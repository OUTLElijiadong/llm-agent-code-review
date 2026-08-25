"""应用重启后的持久任务恢复调度。"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Iterable

from loguru import logger

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.agent_mesh import AgentMeshMessage
from app.models.agent_response_run import AgentResponseRun
from app.models.user import User
from app.services.agent_responses_service import AgentResponsesService


@dataclass(frozen=True)
class AgentRunRecovery:
    """恢复一次 Responses 运行所需的最小持久化引用。"""

    run_id: str
    user_id: int
    surface: str
    session_key: str


def find_interrupted_agent_runs() -> list[AgentRunRecovery]:
    """查找由启动清扫标记、且需要自动续跑的 Responses 运行。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(AgentResponseRun)
            .filter(
                AgentResponseRun.status == "failed",
                AgentResponseRun.checkpoint_json.like('%"recovery_requested": true%'),
            )
            .order_by(AgentResponseRun.id.asc())
            .all()
        )
        recoveries: list[AgentRunRecovery] = []
        for row in rows:
            try:
                checkpoint = json.loads(row.checkpoint_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(checkpoint, dict) or checkpoint.get("recovery_requested") is not True:
                continue
            if _is_blocked_jarvis_recovery(db, row):
                checkpoint["recovery_requested"] = False
                checkpoint["error"] = "后台成本保护已阻止 JARVIS 自动恢复;请管理员明确发起核验"
                row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
                row.version = int(row.version or 0) + 1
                db.commit()
                continue
            recoveries.append(
                AgentRunRecovery(
                    run_id=str(row.run_id),
                    user_id=int(row.user_id),
                    surface=str(row.surface),
                    session_key=str(row.session_key),
                )
            )
        return recoveries
    finally:
        db.close()


def _is_blocked_jarvis_recovery(db, row: AgentResponseRun) -> bool:
    """恢复队列中的旧 JARVIS 运行不得绕过自动派发开关。"""
    if settings.agent_jarvis_auto_dispatch_enabled or not row.mesh_message_id:
        return False
    message = (
        db.query(AgentMeshMessage)
        .filter(AgentMeshMessage.message_id == row.mesh_message_id)
        .first()
    )
    if message is None:
        return False
    try:
        payload = json.loads(message.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return isinstance(payload, dict) and payload.get("patrol_kind") == "jarvis"


def start_agent_run_recovery(recoveries: Iterable[AgentRunRecovery] | None = None) -> int:
    """用独立线程恢复运行，返回已派发数量。"""
    items = list(recoveries if recoveries is not None else find_interrupted_agent_runs())
    for recovery in items:
        threading.Thread(
            target=_resume_agent_run,
            args=(recovery,),
            name=f"agent-run-recovery-{recovery.run_id[:24]}",
            daemon=True,
        ).start()
    if items:
        logger.warning("[recovery] 已派发 {} 个小菱持久运行继续执行", len(items))
    return len(items)


def _resume_agent_run(recovery: AgentRunRecovery) -> None:
    """在线程专属事件循环与数据库会话中重试一个检查点运行。"""
    db = SessionLocal()
    try:
        user = db.get(User, recovery.user_id)
        if user is None:
            logger.error("[recovery] 小菱运行 {} 的用户不存在，无法恢复", recovery.run_id)
            return
        service = AgentResponsesService(
            db,
            user,
            surface=recovery.surface,
            session_key=recovery.session_key,
        )
        result = asyncio.run(service.resume(run_id=recovery.run_id, action="retry"))
        logger.info(
            "[recovery] 小菱运行 {} 已恢复至状态 {}",
            recovery.run_id,
            result.status,
        )
    except Exception as exc:  # noqa: BLE001 - 单任务失败必须隔离且保留检查点
        db.rollback()
        row = db.query(AgentResponseRun).filter(AgentResponseRun.run_id == recovery.run_id).first()
        if row is not None and row.status == "failed":
            try:
                checkpoint = json.loads(row.checkpoint_json or "{}")
            except (TypeError, json.JSONDecodeError):
                checkpoint = {}
            if isinstance(checkpoint, dict):
                checkpoint["recovery_requested"] = False
                checkpoint["error"] = f"服务端自动恢复失败: {exc}"[:1000]
                row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
                row.version = int(row.version or 0) + 1
                db.commit()
        logger.exception("[recovery] 小菱运行 {} 自动恢复失败: {}", recovery.run_id, exc)
    finally:
        db.close()
