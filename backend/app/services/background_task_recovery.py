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
from app.services import agent_mesh_service
from app.services.agent_responses_service import AgentResponsesService, is_paused


@dataclass(frozen=True)
class AgentRunRecovery:
    """恢复一次 Responses 运行所需的最小持久化引用。"""

    run_id: str
    user_id: int
    surface: str
    session_key: str


def find_interrupted_agent_runs() -> list[AgentRunRecovery]:
    """查找中断运行，以及终态已落库但 Mesh 回执仍 processing 的运行。"""
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
            if checkpoint.get("stop_requested") is True:
                reason = str(checkpoint.get("stop_reason") or checkpoint.get("cancel_reason") or "").strip()[:500]
                checkpoint.update({
                    "status": "cancelled",
                    "pending": None,
                    "recovery_requested": False,
                    "cancel_reason": reason,
                    "error": "",
                    "output_text": (
                        f"用户取消了本次运行（原因：{reason}）。已停止任务，未执行剩余操作；如需继续可重新发起。"
                        if reason else "用户取消了本次运行。已停止任务，未执行剩余操作；如需继续可重新发起。"
                    ),
                })
                row.status = "cancelled"
                row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
                row.version = int(row.version or 0) + 1
                db.commit()
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
        known = {
            (item.run_id, item.user_id, item.surface, item.session_key)
            for item in recoveries
        }
        pending_receipts = _query_pending_terminal_receipts(db, limit=500)
        for row, message in pending_receipts:
            identity = (
                str(row.run_id), int(row.user_id), str(row.surface), str(row.session_key),
            )
            if identity in known or message.send_to != f"session:{row.surface}:{row.session_key}":
                continue
            try:
                checkpoint = json.loads(row.checkpoint_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(checkpoint, dict)
                or checkpoint.get("recovery_requested") is True
                or checkpoint.get("stop_requested") is True
            ):
                continue
            recoveries.append(AgentRunRecovery(
                run_id=identity[0],
                user_id=identity[1],
                surface=identity[2],
                session_key=identity[3],
            ))
            known.add(identity)
        return recoveries
    finally:
        db.close()


def _query_pending_terminal_receipts(db, *, limit: int) -> list[tuple[AgentResponseRun, AgentMeshMessage]]:
    return (
        db.query(AgentResponseRun, AgentMeshMessage)
        .join(AgentMeshMessage, AgentMeshMessage.message_id == AgentResponseRun.mesh_message_id)
        .filter(
            AgentResponseRun.status.in_(("completed", "failed")),
            AgentMeshMessage.status == "processing",
            AgentMeshMessage.user_id == AgentResponseRun.user_id,
        )
        .order_by(AgentResponseRun.id.asc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )


def recover_pending_terminal_receipts_once(*, limit: int = 100) -> int:
    """周期性收敛已终态 Responses 运行的未完成会话回执，不重新调用模型。"""
    db = SessionLocal()
    settled = 0
    try:
        for run, message in _query_pending_terminal_receipts(db, limit=limit):
            expected_target = f"session:{run.surface}:{run.session_key}"
            if message.send_to != expected_target:
                continue
            checkpoint = _load_checkpoint(run)
            if checkpoint is None or checkpoint.get("recovery_requested") is True:
                continue
            user = db.get(User, int(run.user_id))
            if user is None:
                continue
            recovery = AgentRunRecovery(
                run_id=str(run.run_id),
                user_id=int(run.user_id),
                surface=str(run.surface),
                session_key=str(run.session_key),
            )
            _finish_terminal_run_receipt(db, user, run, recovery, checkpoint)
            db.expire_all()
            status = (
                db.query(AgentMeshMessage.status)
                .filter(AgentMeshMessage.id == int(message.id))
                .scalar()
            )
            if status != "processing":
                settled += 1
        return settled
    except Exception:  # noqa: BLE001 - 下一周期继续处理剩余终态回执
        db.rollback()
        logger.exception("[recovery] 周期性 Mesh 终态回执扫描失败")
        return settled
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


_terminal_receipt_scheduler = None


def start_terminal_receipt_recovery_scheduler() -> None:
    """启动低成本周期扫描，避免单次回执故障一直等到服务重启。"""
    global _terminal_receipt_scheduler
    if _terminal_receipt_scheduler and getattr(_terminal_receipt_scheduler, "running", False):
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        recover_pending_terminal_receipts_once,
        "interval",
        id="agent-terminal-receipt-recovery",
        seconds=max(5, int(settings.agent_run_receipt_recovery_interval_seconds)),
        kwargs={"limit": 100},
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _terminal_receipt_scheduler = scheduler
    logger.info("[recovery] terminal Mesh receipt reconciler started")


def stop_terminal_receipt_recovery_scheduler() -> None:
    global _terminal_receipt_scheduler
    if not _terminal_receipt_scheduler:
        return
    try:
        if getattr(_terminal_receipt_scheduler, "running", False):
            _terminal_receipt_scheduler.shutdown(wait=False)
    finally:
        _terminal_receipt_scheduler = None
        logger.info("[recovery] terminal Mesh receipt reconciler stopped")


def _resume_agent_run(recovery: AgentRunRecovery) -> None:
    """在线程专属事件循环与数据库会话中重试一个检查点运行。"""
    db = SessionLocal()
    try:
        user = db.get(User, recovery.user_id)
        if user is None:
            logger.error("[recovery] 小菱运行 {} 的用户不存在，无法恢复", recovery.run_id)
            return
        row = _load_recovery_run(db, recovery)
        if row is not None:
            checkpoint = _load_checkpoint(row)
            if checkpoint is not None and checkpoint.get("stop_requested") is True:
                reason = str(checkpoint.get("stop_reason") or checkpoint.get("cancel_reason") or "").strip()[:500]
                checkpoint.update({
                    "status": "cancelled",
                    "pending": None,
                    "recovery_requested": False,
                    "cancel_reason": reason,
                    "error": "",
                    "output_text": (
                        f"用户取消了本次运行（原因：{reason}）。已停止任务，未执行剩余操作；如需继续可重新发起。"
                        if reason else "用户取消了本次运行。已停止任务，未执行剩余操作；如需继续可重新发起。"
                    ),
                })
                row.status = "cancelled"
                row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
                row.version = int(row.version or 0) + 1
                db.commit()
                logger.info("[recovery] 小菱运行 {} 已收到停止意图，跳过自动恢复", recovery.run_id)
                return
        if row is not None and row.status in {"completed", "failed"}:
            checkpoint = _load_checkpoint(row)
            if checkpoint is not None and checkpoint.get("recovery_requested") is not True:
                _finish_terminal_run_receipt(db, user, row, recovery, checkpoint)
                return
        service = AgentResponsesService(
            db,
            user,
            surface=recovery.surface,
            session_key=recovery.session_key,
        )
        result = asyncio.run(service.resume(run_id=recovery.run_id, action="retry"))
        if not is_paused(result):
            row = (
                db.query(AgentResponseRun)
                .filter(
                    AgentResponseRun.run_id == recovery.run_id,
                    AgentResponseRun.user_id == recovery.user_id,
                    AgentResponseRun.surface == recovery.surface,
                    AgentResponseRun.session_key == recovery.session_key,
                )
                .first()
            )
            if row is not None:
                _finish_recovered_session_message(
                    db,
                    user,
                    row,
                    recovery,
                    success=result.status == "completed",
                    summary=str(getattr(result, "output_text", "") or ""),
                    error=str(getattr(result, "error", "") or ""),
                )
        logger.info(
            "[recovery] 小菱运行 {} 已恢复至状态 {}",
            recovery.run_id,
            result.status,
        )
    except Exception as exc:  # noqa: BLE001 - 单任务失败必须隔离且保留检查点
        db.rollback()
        row = _load_recovery_run(db, recovery)
        if row is not None:
            checkpoint = _load_checkpoint(row) or {}
            if row.status == "failed" and checkpoint.get("recovery_requested") is True:
                checkpoint["recovery_requested"] = False
                checkpoint["error"] = f"服务端自动恢复失败: {exc}"[:1000]
                row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
                row.version = int(row.version or 0) + 1
                db.commit()
                user = db.get(User, recovery.user_id)
                if user is not None:
                    _finish_recovered_session_message_with_retry(
                        db,
                        user,
                        row,
                        recovery,
                        success=False,
                        error=f"服务端自动恢复失败: {exc}"[:1000],
                    )
            elif row.status in {"completed", "failed"} and checkpoint:
                user = db.get(User, recovery.user_id)
                if user is not None:
                    _finish_terminal_run_receipt(db, user, row, recovery, checkpoint)
        logger.exception("[recovery] 小菱运行 {} 自动恢复失败: {}", recovery.run_id, exc)
    finally:
        db.close()


def _finish_recovered_session_message(
    db,
    user: User,
    run: AgentResponseRun,
    recovery: AgentRunRecovery,
    *,
    success: bool,
    summary: str = "",
    error: str = "",
) -> None:
    """仅收敛属于同账号、同 surface 与会话，且仍在处理中的 Mesh 回执。"""
    message_id = str(run.mesh_message_id or "")
    if not message_id:
        return
    expected_target = f"session:{recovery.surface}:{recovery.session_key}"
    message = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.message_id == message_id,
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.send_to == expected_target,
            AgentMeshMessage.status == "processing",
        )
        .first()
    )
    if message is None:
        return
    agent_mesh_service.finish_message_run(
        db,
        user,
        message_id,
        surface=recovery.surface,
        session_key=recovery.session_key,
        success=success,
        summary=summary,
        error=error,
    )


def _load_recovery_run(db, recovery: AgentRunRecovery) -> AgentResponseRun | None:
    return (
        db.query(AgentResponseRun)
        .filter(
            AgentResponseRun.run_id == recovery.run_id,
            AgentResponseRun.user_id == recovery.user_id,
            AgentResponseRun.surface == recovery.surface,
            AgentResponseRun.session_key == recovery.session_key,
        )
        .first()
    )


def _load_checkpoint(run: AgentResponseRun) -> dict | None:
    try:
        checkpoint = json.loads(run.checkpoint_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return checkpoint if isinstance(checkpoint, dict) else None


def _finish_terminal_run_receipt(
    db,
    user: User,
    run: AgentResponseRun,
    recovery: AgentRunRecovery,
    checkpoint: dict,
) -> None:
    _finish_recovered_session_message_with_retry(
        db,
        user,
        run,
        recovery,
        success=run.status == "completed",
        summary=str(checkpoint.get("output_text") or "")[:2000],
        error=str(checkpoint.get("error") or "")[:2000],
    )


def _finish_recovered_session_message_with_retry(
    db,
    user: User,
    run: AgentResponseRun,
    recovery: AgentRunRecovery,
    *,
    success: bool,
    summary: str = "",
    error: str = "",
) -> None:
    """一次数据库瞬断后回滚并重试；失败仍留作启动时的终态回执扫描。"""
    for attempt in range(2):
        try:
            _finish_recovered_session_message(
                db,
                user,
                run,
                recovery,
                success=success,
                summary=summary,
                error=error,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 下一轮可在新事务中重试
            db.rollback()
            if attempt == 1:
                logger.warning(
                    "[recovery] 小菱运行 {} 的 Mesh 回执暂未收敛，将在下次启动继续: {}",
                    recovery.run_id,
                    exc,
                )
