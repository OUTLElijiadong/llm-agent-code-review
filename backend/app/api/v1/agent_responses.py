"""普通用户 ChatAgent 与管理员 Agent 共用的 Responses SSE API。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, List, Literal, Mapping, Optional, Sequence

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import Field, field_validator, model_validator
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.agent_mesh import AgentMeshMessage
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import agent_mesh_service, rbac_service
from app.services.agent_responses_service import (
    AgentResponsesService,
    is_paused,
    redact_agent_event_value,
    redact_agent_output_text,
    terminal_event,
)
from app.services.ai_usage_context import model_attribution, usage_context
from app.utils.api_resolver import resolve_api_config
from app.utils.input_validation import normalize_plain_text

router = APIRouter()
_BACKGROUND_RESPONSE_TASKS: set[asyncio.Task[Any]] = set()


@router.get(
    "/runs/{run_id}/assets",
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def list_run_assets(run_id: str, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """列出一次运行留档的多模态资产(输入/输出图片元数据;仅本人)。"""
    from app.models.agent_multimodal import AgentMultimodalAsset

    rows = (
        db.query(AgentMultimodalAsset)
        .filter(
            AgentMultimodalAsset.run_id == run_id,
            AgentMultimodalAsset.user_id == int(user.id),
            AgentMultimodalAsset.surface.in_(("user", "admin") if _is_admin_actor(db, user) else ("user",)),
        )
        .order_by(AgentMultimodalAsset.id)
        .all()
    )
    return Resp(data=[{
        "id": int(row.id),
        "run_id": row.run_id,
        "role": row.role,
        "mime": row.mime,
        "sha256": row.sha256,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    } for row in rows])


@router.get(
    "/assets/{asset_id}/image",
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def get_asset_image(asset_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """查看留档的多模态图片(内联,仅本人)。"""
    from fastapi.responses import Response as FastResponse

    from app.models.agent_multimodal import AgentMultimodalAsset

    row = (
        db.query(AgentMultimodalAsset)
        .filter(
            AgentMultimodalAsset.id == asset_id,
            AgentMultimodalAsset.user_id == int(user.id),
            AgentMultimodalAsset.surface.in_(("user", "admin") if _is_admin_actor(db, user) else ("user",)),
        )
        .first()
    )
    if row is None:
        raise NotFoundError("多模态资产不存在", code=40400)
    return FastResponse(
        content=row.data,
        media_type=row.mime,
        headers={
            "Content-Disposition": "inline",
            # 浏览器缓存按 URL 复用；退出后换账号也必须重新执行所属账号校验。
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _schedule_profile_refresh(user_id: int) -> None:
    """偏好聚合使用独立会话与线程，不能延迟或回滚已经完成的用户任务。"""
    from app.services.profile_service import refresh_background

    task = asyncio.create_task(asyncio.to_thread(refresh_background, user_id))
    _BACKGROUND_RESPONSE_TASKS.add(task)
    task.add_done_callback(_release_background_response_task)


def _release_background_response_task(task: asyncio.Task[Any]) -> None:
    """释放后台任务引用并消费异常，避免断开 SSE 后产生未处理异常。"""
    _BACKGROUND_RESPONSE_TASKS.discard(task)
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.error("小菱后台运行异常: {}", error)

_RAW_TERMINAL_EVENTS = {
    "response.completed",
    "response.incomplete",
    "response.failed",
    "response.cancelled",
}
_ACTIVE_RUN_STATUSES = {
    "running",
    "approving",
    "rejecting",
    "answering",
    "waiting_approval",
    "waiting_input",
}
_TRANSITION_TO_WAITING = {
    "approving": "waiting_approval",
    "rejecting": "waiting_approval",
    "answering": "waiting_input",
}


def _is_admin_actor(db: Session, user: User) -> bool:
    """使用集中 RBAC 管理员判定。"""
    if not isinstance(user, User):  # 轻量协议测试替身；生产请求始终是 ORM User。
        return str(getattr(user, "role", "")) in {"admin", "super_admin"}
    return rbac_service.is_admin_user(db, int(user.id))


class AgentResponseMessage(StrictInputModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=100_000)
    # 多模态:data URL 图片(PNG/JPEG/WebP/GIF,单张≤1.5MB,≤4张);服务端再校验
    images: List[str] = Field(default_factory=list, max_length=4)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalize_plain_text(value, field_name="对话内容", allow_empty=True)
        return value


class AgentResponsesRequest(StrictInputModel):
    action: Literal["start", "approve", "reject", "answer", "retry", "cancel"] = "start"
    surface: Literal["user", "admin"] = "user"
    session_id: str = Field(min_length=8, max_length=128)
    messages: List[AgentResponseMessage] = Field(default_factory=list, max_length=100)
    # 新客户端只提交本轮用户输入；旧对话由同账号服务端账本恢复并由 runtime 压缩。
    use_server_history: bool = False
    run_id: str = Field(default="", max_length=80)
    call_id: str = Field(default="", max_length=160)
    answer: str = Field(default="", max_length=20_000)
    confirmation: str = Field(default="", max_length=20)
    cancel_reason: str = Field(default="", max_length=500)
    mesh_message_id: str = Field(default="", max_length=80)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("session_id 只能包含字母、数字、连字符和下划线")
        return value

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if value and not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("run_id 格式非法")
        return value

    @field_validator("mesh_message_id")
    @classmethod
    def validate_mesh_message_id(cls, value: str) -> str:
        # 系统生成的协作消息既有 msg_<hex>,也有 team-<id>-task-<id>-...-result 等形态;
        # 只校验安全字符集,交给服务层按账本认领,避免团队结果无法自动续跑。
        if value and not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("mesh_message_id 格式非法")
        return value

    @field_validator("answer", "cancel_reason")
    @classmethod
    def validate_free_text(cls, value: str) -> str:
        normalize_plain_text(value, field_name="Agent 操作文本", allow_empty=True)
        return value

    @model_validator(mode="after")
    def validate_action_fields(self) -> "AgentResponsesRequest":
        if self.action == "start":
            if self.use_server_history and not self.mesh_message_id and (
                len(self.messages) != 1 or self.messages[0].role != "user"
            ):
                raise ValueError("服务端历史模式只能提交本轮一条用户消息")
            for index, item in enumerate(self.messages):
                if item.images and (item.role != "user" or index != len(self.messages) - 1):
                    raise ValueError("图片只能附在最后一条用户消息中，请重新选择本次图片")
            has_user_message = any(
                item.role == "user" and (item.content.strip() or item.images)
                for item in self.messages
            )
            if not has_user_message and not self.mesh_message_id:
                raise ValueError("启动 Agent 时必须提供用户消息或 Agent Mesh 消息")
            if has_user_message and self.mesh_message_id:
                raise ValueError("用户消息与 Agent Mesh 消息不能在同一次启动中混用")
        elif not self.run_id:
            raise ValueError("恢复 Agent 运行必须提供 run_id")
        if self.action != "start" and self.mesh_message_id:
            raise ValueError("恢复运行时不得重新指定 mesh_message_id")
        if self.action == "answer" and not self.answer.strip():
            raise ValueError("回答模型追问时 answer 不能为空")
        return self


@router.get(
    "/session",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def get_agent_response_session(
    surface: Literal["user", "admin"] = Query(default="user"),
    session_id: str = Query(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    """恢复当前用户会话最近一次 Responses 运行及待处理动作。"""

    if surface == "admin" and not _is_admin_actor(db, user):
        raise ForbiddenError("仅管理员可使用管理员 Agent", code=40300)
    active_query = (
        db.query(AgentResponseRun)
        .filter(
            AgentResponseRun.user_id == user.id,
            AgentResponseRun.surface == surface,
            AgentResponseRun.session_key == session_id,
            AgentResponseRun.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .order_by(AgentResponseRun.id.desc())
    )
    row = active_query.first()
    if row is None:
        row = (
            db.query(AgentResponseRun)
            .filter(
                AgentResponseRun.user_id == user.id,
                AgentResponseRun.surface == surface,
                AgentResponseRun.session_key == session_id,
            )
            .order_by(AgentResponseRun.id.desc())
            .first()
        )
    if row is None:
        return Resp(
            data={
                "surface": surface,
                "session_id": session_id,
                "run": None,
                "messages": [],
                "history_page": {"has_more": False, "oldest_message_index": 0, "total": 0},
                "pending": None,
                "mesh_messages": agent_mesh_service.list_session_messages(
                    db,
                    user,
                    surface=surface,
                    session_key=session_id,
                ),
            }
        )
    try:
        checkpoint = json.loads(row.checkpoint_json or "{}")
    except (TypeError, json.JSONDecodeError):
        checkpoint = {}
    if not isinstance(checkpoint, Mapping):
        checkpoint = {}
    replay_events = _public_completed_tool_events(db, row, checkpoint)
    history_page = _session_history_page(
        db, user_id=int(user.id), surface=surface, session_id=session_id, limit=100,
    )
    return Resp(
        data={
            "surface": surface,
            "session_id": session_id,
            "run": {
                "run_id": row.run_id,
                "status": row.status,
                "mesh_message_id": row.mesh_message_id or "",
                "model": str(checkpoint.get("model") or ""),
                "rounds": int(checkpoint.get("rounds") or 0),
                "error": _public_text(checkpoint.get("error")),
                "output_text": redact_agent_output_text(str(checkpoint.get("output_text") or "")),
                "cancel_reason": _public_text(checkpoint.get("cancel_reason")),
                "updated_at": _public_utc_time(row.update_time),
            },
            "messages": history_page["messages"],
            "history_page": {
                "has_more": history_page["has_more"],
                "oldest_message_index": history_page["oldest_message_index"],
                "total": history_page["total"],
            },
            "events": replay_events,
            "last_sequence_number": len(replay_events),
            "pending": _public_pending_event(row.run_id, row.status, checkpoint.get("pending")),
            "mesh_messages": agent_mesh_service.list_session_messages(
                db,
                user,
                surface=surface,
                session_key=session_id,
            ),
        }
    )


@router.get(
    "/session/messages",
    response_model=Resp[dict],
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
def get_agent_response_session_messages(
    surface: Literal["user", "admin"] = Query(default="user"),
    session_id: str = Query(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    before_message: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resp[dict]:
    """按会话内稳定的零基序号取更早消息，不传回整段无界历史。"""

    if surface == "admin" and not _is_admin_actor(db, user):
        raise ForbiddenError("仅管理员可使用管理员 Agent", code=40300)
    return Resp(data=_session_history_page(
        db, user_id=int(user.id), surface=surface, session_id=session_id,
        before_message=before_message, limit=limit,
    ))


def sweep_stale_active_runs(db: Session, *, max_age_seconds: int = 0) -> int:
    """批量清扫僵尸活跃运行(Worker 硬重启遗留)。

    部署重启会杀死进行中的续跑,这些 run 停留在 running/approving/rejecting,
    只有用户重新轮询该会话时才会被逐个恢复;页面关闭的用户永远等不到。
    启动时全表清扫一次,让这些 run 进入带恢复标记的 failed 终态，后台调度器随后自动 retry。

    Args:
        db: 数据库会话。
        max_age_seconds: 只清超过该秒数的行;0 表示应用启动阶段强制回收全部执行态。

    Returns:
        int: 实际翻转状态的行数。
    """
    swept = 0
    rows = (
        db.query(AgentResponseRun)
        .filter(AgentResponseRun.status.in_(_ACTIVE_RUN_STATUSES))
        .order_by(AgentResponseRun.id.asc())
        .all()
    )
    for row in rows:
        before = row.status
        if _is_blocked_jarvis_run(db, row):
            _stop_blocked_jarvis_run(db, row)
            if row.status != before:
                swept += 1
            continue
        _recover_stale_active_run(
            db,
            row,
            force=max_age_seconds == 0,
            max_age_seconds=max_age_seconds,
        )
        if row.status != before:
            swept += 1
    return swept


def _is_blocked_jarvis_run(db: Session, row: AgentResponseRun) -> bool:
    """重启清扫时识别旧版自动 JARVIS 运行,避免它被后台恢复再次计费。"""
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


def _stop_blocked_jarvis_run(db: Session, row: AgentResponseRun) -> None:
    """把被成本保护拦截的活跃运行收敛为不可恢复失败。"""
    try:
        checkpoint = json.loads(row.checkpoint_json or "{}")
    except (TypeError, json.JSONDecodeError):
        checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    checkpoint["status"] = "failed"
    checkpoint["pending"] = None
    checkpoint["recovery_requested"] = False
    checkpoint["error"] = "后台成本保护已阻止 JARVIS 自动恢复;请管理员明确发起核验"
    row.status = "failed"
    row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
    row.version = int(row.version or 0) + 1
    db.commit()


def _recover_stale_active_run(
    db: Session,
    row: AgentResponseRun,
    *,
    force: bool = False,
    max_age_seconds: int = 0,
) -> None:
    """恢复 Worker 硬退出留下的过渡态；租约长于单次模型超时。"""
    updated_at = row.update_time
    if updated_at is None:
        return
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    lease_seconds = max_age_seconds or max(int(settings.deepseek_timeout) + 120, 900)
    if not force and (datetime.now(timezone.utc) - updated_at).total_seconds() < lease_seconds:
        return
    try:
        checkpoint = json.loads(row.checkpoint_json or "{}")
    except (TypeError, json.JSONDecodeError):
        checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    if row.status in {"waiting_approval", "waiting_input"}:
        return
    waiting_status = _TRANSITION_TO_WAITING.get(row.status)
    if waiting_status and isinstance(checkpoint.get("pending"), Mapping):
        checkpoint["status"] = waiting_status
        checkpoint["error"] = "Worker 中断后已恢复待处理操作"
        row.status = waiting_status
    else:
        checkpoint["status"] = "failed"
        checkpoint["pending"] = None
        checkpoint["error"] = "Worker 在执行中中断，服务端正在从检查点自动恢复（此前运行已安全终止）"
        checkpoint["recovery_requested"] = True
        row.status = "failed"
    row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
    row.version = int(row.version or 0) + 1
    db.commit()


def _public_utc_time(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat()


def _public_session_messages(db: Session, row: AgentResponseRun, checkpoint: Mapping[str, Any]) -> list[dict[str, Any]]:
    """恢复同账号、同会话的图片元数据；旧轮次只在历史前缀精确一致时关联。"""
    from app.models.agent_multimodal import AgentMultimodalAsset

    runs = (
        db.query(AgentResponseRun)
        .join(AgentMultimodalAsset, AgentMultimodalAsset.run_id == AgentResponseRun.run_id)
        .filter(
            AgentResponseRun.user_id == row.user_id,
            AgentResponseRun.surface == row.surface,
            AgentResponseRun.session_key == row.session_key,
            AgentResponseRun.id <= row.id,
            AgentMultimodalAsset.user_id == row.user_id,
            AgentMultimodalAsset.surface == row.surface,
            AgentMultimodalAsset.role == "input",
        ).distinct().order_by(AgentResponseRun.id.desc()).all()
    )
    assets_by_run: dict[str, dict[str, dict[str, Any]]] = {}
    if runs:
        assets = db.query(
            AgentMultimodalAsset.id, AgentMultimodalAsset.run_id,
            AgentMultimodalAsset.sha256, AgentMultimodalAsset.mime,
        ).filter(
            AgentMultimodalAsset.run_id.in_([run.run_id for run in runs]),
            AgentMultimodalAsset.user_id == row.user_id,
            AgentMultimodalAsset.surface == row.surface,
            AgentMultimodalAsset.role == "input",
        ).all()
        for asset in assets:
            assets_by_run.setdefault(asset.run_id, {})[asset.sha256] = {
                "id": int(asset.id), "mime": asset.mime, "sha256": asset.sha256,
            }
    messages = _public_transcript_messages(
        checkpoint.get("transcript"), image_assets=assets_by_run.get(row.run_id, {}),
    )
    for previous in runs:
        if previous.id == row.id:
            continue
        try:
            old_checkpoint = json.loads(previous.checkpoint_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(old_checkpoint, dict):
            continue
        old_messages = _public_transcript_messages(
            old_checkpoint.get("transcript"), image_assets=assets_by_run.get(previous.run_id, {}),
        )
        if len(old_messages) > len(messages):
            continue
        # 先验证完整旧历史是当前历史前缀，再整体回填；后续分支不同时不能先挂上首问图片。
        if not all(
            current["role"] == old["role"] and current["content"] in {
                old["content"], old["content"] + "\n" + "\n".join(["[图片]"] * len(old.get("image_assets", []))),
            }
            for current, old in zip(messages, old_messages)
        ):
            continue
        for current, old in zip(messages, old_messages):
            if old.get("image_assets") and not current.get("image_assets"):
                current["image_assets"] = old["image_assets"]
                current["content"] = old["content"]
    return messages


def _public_session_history_messages(db: Session, selected: AgentResponseRun) -> list[dict[str, Any]]:
    """从完整运行账本重建同账号会话的可见消息，不截断模型上下文。"""
    from app.models.agent_multimodal import AgentMultimodalAsset

    query = (
        db.query(AgentResponseRun)
        .filter(
            AgentResponseRun.user_id == selected.user_id,
            AgentResponseRun.surface == selected.surface,
            AgentResponseRun.session_key == selected.session_key,
        )
    )
    has_images = db.query(AgentMultimodalAsset.id).join(
        AgentResponseRun, AgentMultimodalAsset.run_id == AgentResponseRun.run_id,
    ).filter(
        AgentResponseRun.user_id == selected.user_id,
        AgentResponseRun.surface == selected.surface,
        AgentResponseRun.session_key == selected.session_key,
        AgentMultimodalAsset.user_id == selected.user_id,
        AgentMultimodalAsset.surface == selected.surface,
        AgentMultimodalAsset.role == "input",
    ).first() is not None

    runs: list[AgentResponseRun] = []
    visible_by_run: dict[int, list[dict[str, Any]]] = {}
    offset = 0
    while True:
        batch = query.order_by(
            AgentResponseRun.create_time.desc(), AgentResponseRun.id.desc(),
        ).offset(offset).limit(100).all()
        if not batch:
            break
        for run in batch:
            try:
                checkpoint = json.loads(run.checkpoint_json or "{}")
            except (TypeError, ValueError):
                raise ConflictError("会话历史检查点损坏，请联系管理员修复后重试", code=40931) from None
            if not isinstance(checkpoint, Mapping):
                checkpoint = {}
            visible_by_run[int(run.id)] = (
                _public_session_messages(db, run, checkpoint)
                if has_images else _public_transcript_messages(checkpoint.get("transcript"))
            )
        runs[:0] = reversed(batch)
        offset += len(batch)
        if len(batch) < 100:
            break
    return _merge_public_history_runs(runs, visible_by_run)


def _session_history_page(
    db: Session, *, user_id: int, surface: str, session_id: str,
    limit: int, before_message: Optional[int] = None,
) -> dict[str, Any]:
    """只传回一个有界页面；游标是完整历史中的零基起点。"""

    selected = db.query(AgentResponseRun).filter(
        AgentResponseRun.user_id == user_id,
        AgentResponseRun.surface == surface,
        AgentResponseRun.session_key == session_id,
    ).order_by(AgentResponseRun.id.desc()).first()
    history = _public_session_history_messages(db, selected) if selected is not None else []
    total = len(history)
    end = total if before_message is None else min(total, max(0, before_message))
    start = max(0, end - limit)
    return {
        "messages": history[start:end],
        "oldest_message_index": start,
        "has_more": start > 0,
        "total": total,
    }


def _server_history_transcript(
    db: Session, *, user_id: int, surface: str, session_id: str,
) -> list[dict[str, Any]]:
    """按运行顺序接续同账号原始 transcript，保留工具调用、结果和图片资产引用。"""

    rows = db.query(AgentResponseRun).filter(
        AgentResponseRun.user_id == user_id,
        AgentResponseRun.surface == surface,
        AgentResponseRun.session_key == session_id,
    ).order_by(AgentResponseRun.create_time.asc(), AgentResponseRun.id.asc()).all()
    combined: list[dict[str, Any]] = []
    for row in rows:
        try:
            checkpoint = json.loads(row.checkpoint_json or "{}")
        except (TypeError, ValueError):
            raise ConflictError("会话历史检查点损坏，请联系管理员修复后重试", code=40931) from None
        transcript = checkpoint.get("transcript") if isinstance(checkpoint, Mapping) else None
        if transcript is not None and not isinstance(transcript, list):
            raise ConflictError("会话历史检查点格式错误，请联系管理员修复后重试", code=40931)
        if not isinstance(transcript, list):
            continue
        source = [dict(item) for item in transcript if isinstance(item, Mapping)]
        if not source:
            continue
        if source == combined and len(source) == 1 and source[0].get("role") == "user":
            # 两次独立运行都只有相同的一句提问时，不可把后一次当成旧前缀吞掉。
            combined.extend(source)
            continue
        if source[:len(combined)] == combined:
            combined.extend(source[len(combined):])
            continue
        # 旧客户端只重传可见对话，下一轮 checkpoint 不含之前的工具证据；
        # 找最长可见消息重叠后追加新后缀，保留已审计工具调用与结果。
        old_visible = [_visible_transcript_key(item) for item in combined]
        old_visible = [item for item in old_visible if item is not None]
        source_visible = [
            (index, key) for index, item in enumerate(source)
            if (key := _visible_transcript_key(item)) is not None
        ]
        overlap = 0
        for size in range(min(len(old_visible), len(source_visible)), 1, -1):
            if old_visible[-size:] == [key for _, key in source_visible[:size]]:
                overlap = size
                break
        start = source_visible[overlap - 1][0] + 1 if overlap else 0
        combined.extend(source[start:])
    return combined


def _visible_transcript_key(item: Mapping[str, Any]) -> Optional[str]:
    if str(item.get("role") or "") not in {"user", "assistant"}:
        return None
    if str(item.get("type") or "message") != "message":
        return None
    return json.dumps({"role": item.get("role"), "content": item.get("content")},
                      ensure_ascii=False, sort_keys=True, default=str)


def _merge_public_history_runs(
    runs: Sequence[AgentResponseRun], visible_by_run: Mapping[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """将全量前缀和异步回复折叠为按运行创建时间排序的可见消息。"""
    history: list[dict[str, Any]] = []
    previous_user_run: list[dict[str, Any]] = []
    background_since_user_run: list[dict[str, Any]] = []
    for run in runs:
        messages = visible_by_run[int(run.id)]
        if run.mesh_message_id:
            # Agent Mesh 的投递提示是内部输入，历史只展示本次回复。
            replies = [message for message in messages if message["role"] == "assistant"]
            history.extend(replies)
            background_since_user_run.extend(replies)
            continue

        # 浏览器发起的新运行可能携带完整的旧历史。只把确实新增的后缀
        # 加入账本；相同的单条用户问题也可能是两次独立提问，不能据此去重。
        prefix = history if len(history) >= 2 else previous_user_run
        if len(prefix) >= 2 and messages[:len(prefix)] == prefix:
            new_messages = messages[len(prefix):]
            background_since_user_run = []
        elif (
            previous_user_run
            and (len(previous_user_run) >= 2 or len(messages) > len(previous_user_run))
            and messages[:len(previous_user_run)] == previous_user_run
        ):
            new_messages = messages[len(previous_user_run):]
            # 新运行可能只吸收了部分异步回复，也可能在别的普通运行后
            # 才吸收；在待回填回复中寻找最长的连续匹配片段。
            for overlap in range(min(len(background_since_user_run), len(new_messages)), 0, -1):
                matched = next((start for start in range(len(background_since_user_run) - overlap + 1)
                                if background_since_user_run[start:start + overlap] == new_messages[:overlap]), None)
                if matched is not None:
                    new_messages = new_messages[overlap:]
                    background_since_user_run = (
                        background_since_user_run[:matched]
                        + background_since_user_run[matched + overlap:]
                    )
                    break
        else:
            new_messages = messages
            # 超过当前窗口时，下一轮可能只携带旧历史的尾部。
            # 至少匹配两条且本轮确有新消息，避免误删独立重复提问。
            for overlap in range(min(len(history), len(new_messages) - 1), 1, -1):
                if history[-overlap:] == new_messages[:overlap]:
                    new_messages = new_messages[overlap:]
                    break
            background_since_user_run = []
        history.extend(new_messages)
        previous_user_run = messages
    return history


def _public_transcript_messages(
    value: Any, *, image_assets: Optional[Mapping[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """只恢复用户可见文本，排除 reasoning、函数参数和工具结果。"""

    if not isinstance(value, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        item_type = str(item.get("type") or "")
        if item_type and item_type != "message":
            continue
        content = item.get("content")
        visible_assets = []
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                part_type = str(part.get("type") or "")
                visible_types = {"input_text", "text"} if role == "user" else {"output_text", "refusal"}
                if part_type == "input_image":
                    url = str(part.get("image_url") or "")
                    asset = (
                        (image_assets or {}).get(url.removeprefix("prism-asset://"))
                        if url.startswith("prism-asset://") else None
                    )
                    if role == "user" and asset:
                        visible_assets.append(asset)
                    else:
                        parts.append("[图片]")
                    continue
                if part_type not in visible_types:
                    continue
                part_value = part.get("refusal") if part_type == "refusal" else part.get("text")
                part_text = str(part_value or "").strip()
                if part_text:
                    parts.append(part_text)
            text = "\n".join(parts)
        else:
            text = ""
        if text and role == "assistant":
            text = redact_agent_output_text(text)
        if text or visible_assets:
            message: dict[str, Any] = {"role": role, "content": text}
            if visible_assets:
                message["image_assets"] = visible_assets
            messages.append(message)
    return messages


def _public_text(value: Any, *, limit: int = 1000) -> str:
    """仅用于工具元数据和错误文本，不改写用户原始输入。"""

    safe_value = redact_agent_event_value(value)
    if safe_value is None:
        return ""
    if isinstance(safe_value, str):
        return safe_value[:limit]
    return str(safe_value)[:limit]


def _transcript_function_calls(checkpoint: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按出现顺序提取 transcript 中的函数调用及对应终态输出（取每个 call_id 最后一次）。

    用于恢复进行中/账本尚未覆盖的工具调用链。参数与输出只做结构化解析，
    脱敏由调用方在生成公开事件时统一处理。
    """

    transcript = checkpoint.get("transcript") if isinstance(checkpoint, Mapping) else None
    if not isinstance(transcript, list):
        return []
    calls: dict[str, dict[str, Any]] = {}
    outputs: dict[str, Mapping[str, Any]] = {}
    order: list[str] = []
    for item in transcript:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "function_call":
            call_id = str(item.get("call_id") or "")
            if not call_id:
                continue
            raw_args = item.get("arguments")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            if call_id not in calls:
                order.append(call_id)
            calls[call_id] = {
                "call_id": call_id,
                "name": str(item.get("name") or ""),
                "arguments": arguments if isinstance(arguments, Mapping) else {},
                "output": None,
                "output_status": "",
            }
        elif item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            if not call_id:
                continue
            raw_output = item.get("output")
            try:
                output = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            except (TypeError, json.JSONDecodeError):
                output = {}
            if isinstance(output, Mapping):
                outputs[call_id] = output
    result: list[dict[str, Any]] = []
    for call_id in order:
        entry = dict(calls[call_id])
        output = outputs.get(call_id)
        if output is not None:
            status = str(output.get("status") or "").casefold()
            entry["output"] = output
            entry["output_status"] = (
                "success"
                if status in {"success", "completed", "ok"}
                else "failed"
                if status in {"error", "failed", "rejected", "denied", "cancelled", "canceled"}
                else ""
            )
        result.append(entry)
    return result


def _public_completed_tool_events(
    db: Session,
    run: AgentResponseRun,
    checkpoint: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """恢复已确认工具执行，并把结论文本排在工具事件之后。

    ``AgentToolExecution.id`` 是实际执行顺序；运行时对同一个 run
    串行执行工具。恢复协议不伪造未确认的 ``executing`` 结果，也不
    从模型 transcript 重放原始参数，只使用已脱敏的幂等账本。
    """

    rows = (
        db.query(AgentToolExecution)
        .filter(
            AgentToolExecution.run_id == run.run_id,
            AgentToolExecution.user_id == run.user_id,
            AgentToolExecution.status.in_(("success", "failed")),
        )
        .order_by(AgentToolExecution.id.asc())
        .all()
    )
    events: list[dict[str, Any]] = []
    sequence = 0
    agent_code = "manager" if run.surface == "admin" else "chat_assistant"
    covered_call_ids: set[str] = set()
    for row in rows:
        try:
            arguments = json.loads(row.arguments_json or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        safe_arguments = redact_agent_event_value(arguments if isinstance(arguments, Mapping) else {})
        sequence += 1
        covered_call_ids.add(str(row.call_id))
        events.append(
            {
                "type": "response.tool.started",
                "run_id": run.run_id,
                "tool_call_id": row.call_id,
                "call_id": row.call_id,
                "tool_name": row.tool_name,
                "agent_code": agent_code,
                "arguments": safe_arguments,
                "cached": True,
                "sequence_number": sequence,
            }
        )

        try:
            result = json.loads(row.result_json or "{}")
        except (TypeError, json.JSONDecodeError):
            result = {}
        if not isinstance(result, Mapping):
            result = {}
        sequence += 1
        if row.status == "success" and str(result.get("status") or "success") == "success":
            events.append(
                {
                    "type": "response.tool.completed",
                    "run_id": run.run_id,
                    "tool_call_id": row.call_id,
                    "call_id": row.call_id,
                    "tool_name": row.tool_name,
                    "agent_code": agent_code,
                    "status": "success",
                    "cached": True,
                    "output_summary": _public_text(result.get("output")),
                    "sequence_number": sequence,
                }
            )
        else:
            events.append(
                {
                    "type": "response.tool.failed",
                    "run_id": run.run_id,
                    "tool_call_id": row.call_id,
                    "call_id": row.call_id,
                    "tool_name": row.tool_name,
                    "agent_code": agent_code,
                    "status": "failed",
                    "cached": True,
                    "error": _public_text(result.get("error") or row.error or "工具执行失败"),
                    "sequence_number": sequence,
                }
            )

    # 从模型 transcript 补齐账本尚未覆盖的调用：进行中/未执行完的
    # function_call 只发 started，让前端恢复时能看到“正在调用”的调用链；
    # 已执行但账本缺失的调用按 transcript 终态补 completed/failed。
    for call in _transcript_function_calls(checkpoint):
        if call["call_id"] in covered_call_ids:
            continue
        safe_args = redact_agent_event_value(call["arguments"] if isinstance(call["arguments"], Mapping) else {})
        sequence += 1
        events.append(
            {
                "type": "response.tool.started",
                "run_id": run.run_id,
                "tool_call_id": call["call_id"],
                "call_id": call["call_id"],
                "tool_name": call["name"],
                "agent_code": agent_code,
                "arguments": safe_args,
                "status": "running",
                "cached": True,
                "sequence_number": sequence,
            }
        )
        if call["output"] is not None:
            sequence += 1
            if call["output_status"] == "success":
                events.append(
                    {
                        "type": "response.tool.completed",
                        "run_id": run.run_id,
                        "tool_call_id": call["call_id"],
                        "call_id": call["call_id"],
                        "tool_name": call["name"],
                        "agent_code": agent_code,
                        "status": "success",
                        "cached": True,
                        "output_summary": _public_text(call["output"].get("output")),
                        "sequence_number": sequence,
                    }
                )
            else:
                events.append(
                    {
                        "type": "response.tool.failed",
                        "run_id": run.run_id,
                        "tool_call_id": call["call_id"],
                        "call_id": call["call_id"],
                        "tool_name": call["name"],
                        "agent_code": agent_code,
                        "status": "failed",
                        "cached": True,
                        "error": _public_text(call["output"].get("error") or "工具执行失败"),
                        "sequence_number": sequence,
                    }
                )

    # 目前前端仍可使用 messages 恢复完整对话；events 是按顺序
    # 重建工具时间线的增量契约。将助手文本统一置于工具后，
    # 保证恢复时不会出现“先结论、后调用”。
    visible_messages = _public_transcript_messages(checkpoint.get("transcript"))
    for index, message in enumerate(visible_messages):
        if message["role"] != "assistant":
            continue
        sequence += 1
        events.append(
            {
                "type": "response.output_text.delta",
                "delta": message["content"],
                "item_id": f"recovered-message-{index}",
                "sequence_number": sequence,
            }
        )
    return events


def _public_response_envelope(value: Any) -> dict[str, Any]:
    """仅保留 Responses 终态中的公开元数据和助手文本。"""

    if not isinstance(value, Mapping):
        return {}
    public: dict[str, Any] = {}
    for key in ("id", "object", "status", "model", "created_at", "completed_at", "rounds"):
        field = value.get(key)
        if field is not None:
            public[key] = field
    output_text = value.get("output_text")
    if isinstance(output_text, str):
        public["output_text"] = redact_agent_output_text(output_text)
    error = value.get("error")
    if error is not None:
        public["error"] = redact_agent_event_value(error)

    output: list[dict[str, Any]] = []
    raw_output = value.get("output")
    if isinstance(raw_output, list):
        for item in raw_output:
            if not isinstance(item, Mapping) or str(item.get("type") or "") != "message":
                continue
            content: list[dict[str, str]] = []
            raw_content = item.get("content")
            if isinstance(raw_content, list):
                for part in raw_content:
                    if not isinstance(part, Mapping):
                        continue
                    part_type = str(part.get("type") or "")
                    if part_type not in {"output_text", "refusal"}:
                        continue
                    source_key = "refusal" if part_type == "refusal" else "text"
                    text = redact_agent_output_text(str(part.get(source_key) or ""))
                    if text:
                        content.append({"type": part_type, source_key: text})
            output.append(
                {
                    "id": str(item.get("id") or ""),
                    "type": "message",
                    "status": str(item.get("status") or "completed"),
                    "role": str(item.get("role") or "assistant"),
                    "content": content,
                }
            )
    if output:
        public["output"] = output
    return public


def _public_pending_event(run_id: str, status: str, value: Any) -> Optional[dict[str, Any]]:
    """把持久化 PendingAction 转回与流事件一致的公开结构。"""

    if status not in {"waiting_approval", "waiting_input"} or not isinstance(value, Mapping):
        return None
    call = value.get("call")
    if not isinstance(call, Mapping):
        return None
    arguments = call.get("arguments")
    safe_arguments = (
        redact_agent_event_value(dict(arguments))
        if isinstance(arguments, Mapping)
        else {}
    )
    if not isinstance(safe_arguments, Mapping):
        safe_arguments = {}
    base = {
        "run_id": run_id,
        "call_id": str(call.get("call_id") or ""),
        "tool_name": str(call.get("name") or ""),
        "arguments": safe_arguments,
    }
    if status == "waiting_input":
        return {
            **base,
            "type": "response.input.required",
            "question": str(safe_arguments.get("question") or ""),
            "options": safe_arguments.get("options") if isinstance(safe_arguments.get("options"), list) else [],
            "allow_free_text": bool(safe_arguments.get("allow_free_text", True)),
        }
    return {
        **base,
        "type": "response.approval.required",
        "operation": _public_text(value.get("operation") or call.get("name") or ""),
        "impact": _public_text(value.get("impact")),
        "danger": bool(value.get("danger")),
        "approval_id": value.get("approval_id"),
        "preview": redact_agent_event_value(value.get("preview")),
    }


def _public_stream_event(
    event: Mapping[str, Any],
    *,
    allow_sensitive: bool = False,
) -> Optional[dict[str, Any]]:
    """内部 Agent 流只暴露 UI 需要的协议事件，不透传 reasoning 或原始工具参数。"""
    event_type = str(event.get("type") or "")
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if not isinstance(delta, str) or not delta:
            return None
        return {
            "type": event_type,
            "delta": delta,
            "item_id": event.get("item_id"),
            "output_index": event.get("output_index"),
            "content_index": event.get("content_index"),
        }
    if event_type == "response.created":
        return {"type": event_type, "response": _public_response_envelope(event.get("response"))}
    if event_type in {"response.tool.started", "response.tool.completed", "response.tool.failed"}:
        error = event.get("error")
        if event_type == "response.tool.failed" and not str(error or "").strip():
            error = f"工具 {str(event.get('tool_name') or '未知工具')} 执行失败"
        return {
            "type": event_type,
            "run_id": event.get("run_id"),
            "tool_call_id": event.get("tool_call_id"),
            "call_id": event.get("call_id"),
            "tool_name": event.get("tool_name"),
            "agent_code": event.get("agent_code"),
            "status": event.get("status"),
            "cached": bool(event.get("cached")),
            "arguments": redact_agent_event_value(event.get("arguments")),
            "output_summary": redact_agent_event_value(event.get("output_summary")),
            "error": redact_agent_event_value(error),
        }
    if event_type == "response.approval.required":
        return {
            "type": event_type,
            "run_id": event.get("run_id"),
            "tool_call_id": event.get("tool_call_id"),
            "call_id": event.get("call_id") or event.get("tool_call_id"),
            "name": event.get("name"),
            "tool_name": event.get("tool_name") or event.get("name"),
            "arguments": redact_agent_event_value(event.get("arguments")),
            "operation": _public_text(event.get("operation")),
            "impact": _public_text(event.get("impact")),
            "danger": bool(event.get("danger")),
            "approval_id": event.get("approval_id"),
            "preview": redact_agent_event_value(event.get("preview")),
        }
    if event_type == "response.input.required":
        return {
            "type": event_type,
            "run_id": event.get("run_id"),
            "tool_call_id": event.get("tool_call_id"),
            "call_id": event.get("call_id") or event.get("tool_call_id"),
            "name": event.get("name"),
            "tool_name": event.get("tool_name") or event.get("name"),
            "arguments": redact_agent_event_value(event.get("arguments")),
            "question": _public_text(event.get("question")),
            "options": redact_agent_event_value(event.get("options")),
            "allow_free_text": bool(event.get("allow_free_text", True)),
        }
    if event_type == "response.sensitive.result":
        if not allow_sensitive:
            return None
        capability = str(event.get("capability") or "")
        if capability not in {"beta_codes.generate", "users.reset_password"}:
            return None
        raw_values = event.get("values")
        if not isinstance(raw_values, list):
            return None
        values = [str(value)[:256] for value in raw_values[:100] if isinstance(value, str) and value]
        if not values:
            return None
        return {
            "type": event_type,
            "run_id": str(event.get("run_id") or ""),
            "call_id": str(event.get("call_id") or ""),
            "capability": capability,
            "title": _public_text(event.get("title")),
            "notice": _public_text(event.get("notice")),
            "values": values,
        }
    if event_type in _RAW_TERMINAL_EVENTS:
        return {"type": event_type, "response": _public_response_envelope(event.get("response"))}
    if event_type == "error":
        return {
            "type": "error",
            "message": _public_text(event.get("message")),
            "error": redact_agent_event_value(event.get("error")),
        }
    return None


@router.post(
    "/stream",
    dependencies=[Depends(require_permission(PermissionCode.AGENT_CHAT))],
)
async def stream_agent_response(
    payload: AgentResponsesRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """启动或恢复工具循环，并以 Responses 语义事件持续输出。"""

    if payload.surface == "admin" and not _is_admin_actor(db, user):
        raise ForbiddenError("仅管理员可使用管理员 Agent", code=40300)
    if payload.action == "start" and hasattr(db, "get_bind"):
        first_text = next(
            (
                item.content.strip()
                for item in payload.messages
                if item.role == "user" and item.content.strip()
            ),
            "",
        )
        try:
            agent_mesh_service.heartbeat(
                db,
                user,
                surface=payload.surface,
                session_key=payload.session_id,
                title=(first_text[:32] + ("…" if len(first_text) > 32 else "")) or "新对话",
            )
        except agent_mesh_service.AgentMeshAccessError as exc:
            raise ForbiddenError(str(exc), code=40321) from exc
        except agent_mesh_service.AgentMeshTargetError as exc:
            raise NotFoundError(str(exc), code=40421) from exc
        except agent_mesh_service.AgentMeshError as exc:
            raise ConflictError(str(exc), code=40921) from exc
    run_id = payload.run_id or f"run_{uuid.uuid4().hex}"

    async def event_source() -> AsyncIterator[str]:
        queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=128)
        sequence = 0
        discard_events = False
        # SSE 元数据只描述当前用户最终使用的模型，不改变服务层的模型选择逻辑。
        # 与 AgentResponsesService._runtime 的分层规则保持一致：user/global 配置
        # 优先，系统默认时才回落到 orchestrator 模型。
        try:
            api_config = resolve_api_config(db, user.id)
        except (AttributeError, TypeError):
            # 轻量测试/非真实 Session 场景不阻断首个 response.created。
            api_config = None
        from app.services.agent_model_service import resolve_agent_model

        response_model = (
            resolve_agent_model(db, surface=payload.surface, config=api_config)
            if api_config is not None else settings.deepseek_orchestrator_model
        )
        if payload.action != "start":
            owned_run = db.query(AgentResponseRun).filter(
                AgentResponseRun.run_id == payload.run_id,
                AgentResponseRun.user_id == user.id,
                AgentResponseRun.surface == payload.surface,
                AgentResponseRun.session_key == payload.session_id,
            ).first()
            if owned_run:
                try:
                    response_model = json.loads(owned_run.checkpoint_json).get("model") or response_model
                except (TypeError, ValueError):
                    pass
        # 多模态:含图消息本运行切到视觉模型,元数据同步(与服务层同口径)
        if payload.action == "start" and any(item.images for item in payload.messages):
            from app.services.multimodal_service import resolve_vision_model

            try:
                response_model = resolve_vision_model(db)
            except Exception:  # noqa: BLE001 - 元数据降级为默认模型名
                pass

        async def sink(event: Mapping[str, Any]) -> None:
            nonlocal discard_events
            if discard_events:
                return
            event_type = str(event.get("type") or "")
            if event_type in _RAW_TERMINAL_EVENTS or event_type == "response.created":
                return
            public_event = _public_stream_event(
                event,
                allow_sensitive=payload.surface == "admin",
            )
            if public_event is not None:
                while not discard_events:
                    try:
                        await asyncio.wait_for(queue.put(public_event), timeout=1.0)
                        return
                    except asyncio.TimeoutError:
                        continue

        async def execute() -> Any:
            owns_run_db = hasattr(db, "get_bind")
            if owns_run_db:
                run_session_factory = sessionmaker(
                    bind=db.get_bind(),
                    autocommit=False,
                    autoflush=False,
                    expire_on_commit=False,
                )
                run_db = run_session_factory()
                run_user = run_db.get(User, int(user.id))
            else:
                # 轻量协议测试可能传入无 SQLAlchemy 能力的替身；生产路径始终
                # 进入上面的独立 Session，避免请求断开时关闭后台任务的数据库。
                run_db = db
                run_user = user
            try:
                if run_user is None:
                    raise RuntimeError("运行所属用户不存在")
                service = AgentResponsesService(
                    run_db,
                    run_user,
                    surface=payload.surface,
                    session_key=payload.session_id,
                )
                mesh_message_id = ""
                source_attribution = {}
                server_history_transcript: Optional[list[dict[str, Any]]] = None
                if payload.action == "start":
                    messages = [item.model_dump() for item in payload.messages if item.content.strip() or item.images]
                    first_user_text = next(
                        (
                            str(item.get("content") or "").strip()
                            for item in messages
                            if item.get("role") == "user" and str(item.get("content") or "").strip()
                        ),
                        "",
                    )
                    if first_user_text and owns_run_db:
                        # 标题由服务端按账号/入口写入，避免只改浏览器本地标题后
                        # 重新登录或跨设备时所有历史仍显示“新对话”。
                        agent_mesh_service.update_title_from_user_message(
                            run_db,
                            run_user,
                            surface=payload.surface,
                            session_key=payload.session_id,
                            message=first_user_text,
                        )
                        run_db.commit()
                    if payload.mesh_message_id:
                        mesh_message_id = payload.mesh_message_id
                        _, system_input = agent_mesh_service.prepare_message_run(
                            run_db,
                            run_user,
                            mesh_message_id,
                            surface=payload.surface,
                            session_key=payload.session_id,
                        )
                        messages = [system_input]
                        source_message = run_db.query(AgentMeshMessage).filter(
                            AgentMeshMessage.message_id == mesh_message_id,
                            AgentMeshMessage.user_id == run_user.id,
                        ).first()
                        source_attribution = model_attribution(source_message)
                    elif payload.use_server_history and owns_run_db:
                        server_history_transcript = _server_history_transcript(
                            run_db, user_id=int(run_user.id), surface=payload.surface,
                            session_id=payload.session_id,
                        )
                else:
                    active_row = (
                        run_db.query(AgentResponseRun)
                        .filter(
                            AgentResponseRun.run_id == run_id,
                            AgentResponseRun.user_id == run_user.id,
                            AgentResponseRun.surface == payload.surface,
                            AgentResponseRun.session_key == payload.session_id,
                        )
                        .first()
                    )
                    mesh_message_id = active_row.mesh_message_id if active_row is not None else ""

                try:
                    # 运行级状态广播: Agent 中心工位卡实时显示助手「正在工作」。
                    # 按 surface 归属身份(管理端=贾维斯/manager, 成员端=小菱)。
                    # 事件走全局 AgentEventBus, 按 user_id 隔离; 广播失败不影响运行。
                    if payload.action in {"start", "resume", "approve"}:
                        try:
                            from app.agents.event_bus import emit_event
                            from app.agents.events import AgentEventType
                            from app.services.agent_responses_service import surface_agent_identity

                            agent_code, agent_display = surface_agent_identity(payload.surface)
                            emit_event(
                                AgentEventType.DISPATCH,
                                agent_code,
                                run_id,
                                message=f"{agent_display}开始处理请求",
                                user_id=int(run_user.id),
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    if payload.action == "start":
                        with usage_context(int(run_user.id), source_attribution):
                            start_kwargs: dict[str, Any] = {"run_id": run_id, "event_sink": sink}
                            if server_history_transcript is not None:
                                start_kwargs["server_history_transcript"] = server_history_transcript
                            result = await service.start(messages, **start_kwargs)
                    elif payload.action == "cancel":
                        result = await service.cancel(run_id=run_id, reason=payload.cancel_reason)
                    else:
                        result = await service.resume(
                            run_id=run_id,
                            action=payload.action,
                            call_id=payload.call_id,
                            answer=payload.answer,
                            confirmation=payload.confirmation,
                            event_sink=sink,
                        )
                    if payload.action in {"start", "resume", "approve"}:
                        try:
                            from app.agents.event_bus import emit_event
                            from app.agents.events import AgentEventType
                            from app.services.agent_responses_service import surface_agent_identity

                            agent_code, agent_display = surface_agent_identity(payload.surface)
                            if result.status == "completed":
                                terminal_event = AgentEventType.COMPLETE
                                terminal_message = f"{agent_display}已完成本轮任务"
                            elif result.status == "failed":
                                terminal_event = AgentEventType.FAILED
                                terminal_message = f"{agent_display}本轮任务失败"
                            else:
                                terminal_event = AgentEventType.PROGRESS
                                terminal_message = f"{agent_display}等待用户确认中"
                            emit_event(
                                terminal_event,
                                agent_code,
                                run_id,
                                message=terminal_message,
                                user_id=int(run_user.id),
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    if mesh_message_id:
                        response_row = (
                            run_db.query(AgentResponseRun)
                            .filter(
                                AgentResponseRun.run_id == run_id,
                                AgentResponseRun.user_id == run_user.id,
                            )
                            .first()
                        )
                        if response_row is not None and response_row.mesh_message_id != mesh_message_id:
                            response_row.mesh_message_id = mesh_message_id
                            run_db.commit()
                        if not is_paused(result):
                            agent_mesh_service.finish_message_run(
                                run_db,
                                run_user,
                                mesh_message_id,
                                surface=payload.surface,
                                session_key=payload.session_id,
                                success=result.status == "completed",
                                summary=result.output_text,
                                error=result.error,
                            )
                    if result.status == "completed" and payload.surface == "user":
                        try:
                            if not _is_admin_actor(run_db, run_user):
                                _schedule_profile_refresh(int(run_user.id))
                        except Exception:  # noqa: BLE001 - 偏好学习失败不影响已完成任务
                            logger.warning("小菱偏好更新暂未排入后台，保留本轮结果")
                    return result
                except Exception as exc:
                    if payload.action in {"start", "resume", "approve"}:
                        try:
                            from app.agents.event_bus import emit_event
                            from app.agents.events import AgentEventType
                            from app.services.agent_responses_service import surface_agent_identity

                            agent_code, agent_display = surface_agent_identity(payload.surface)
                            emit_event(
                                AgentEventType.FAILED,
                                agent_code,
                                run_id,
                                message=f"{agent_display}运行异常: {str(exc)[:120]}",
                                user_id=int(run_user.id),
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    if mesh_message_id:
                        try:
                            agent_mesh_service.finish_message_run(
                                run_db,
                                run_user,
                                mesh_message_id,
                                surface=payload.surface,
                                session_key=payload.session_id,
                                success=False,
                                error=str(exc),
                            )
                        except agent_mesh_service.AgentMeshError:
                            run_db.rollback()
                    raise
            finally:
                if owns_run_db:
                    run_db.close()

        def encode(event: Mapping[str, Any]) -> str:
            nonlocal sequence
            sequence += 1
            public_event = _public_stream_event(
                event,
                allow_sensitive=payload.surface == "admin",
            )
            if public_event is None:
                public_event = {
                    "type": "error",
                    "error": {"message": "Agent 产生了不支持的内部事件"},
                }
            value = {**public_event, "sequence_number": sequence}
            return (
                f"event: {value.get('type', 'message')}\ndata: {json.dumps(value, ensure_ascii=False, default=str)}\n\n"
            )

        yield encode(
            {
                "type": "response.created",
                "response": {
                    "id": run_id,
                    "object": "response",
                    "status": "in_progress",
                    "model": response_model,
                },
            }
        )
        task = asyncio.create_task(execute())
        _BACKGROUND_RESPONSE_TASKS.add(task)
        task.add_done_callback(_release_background_response_task)
        event_task: asyncio.Task[Mapping[str, Any]] | None = None
        try:
            while not task.done() or not queue.empty():
                event_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {task, event_task},
                    timeout=15.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if event_task in done:
                    yield encode(event_task.result())
                    event_task = None
                    continue
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
                event_task = None
                if not done:
                    yield ": keep-alive\n\n"

            result = await task
            for event in result.events:
                event_type = str(event.get("type") or "")
                if event_type == "response.approval.required":
                    yield encode(
                        {
                            **dict(event),
                            "call_id": event.get("tool_call_id"),
                            "tool_name": event.get("name"),
                        }
                    )
                elif event_type == "response.input.required":
                    yield encode(
                        {
                            **dict(event),
                            "call_id": event.get("tool_call_id"),
                        }
                    )
            if not is_paused(result):
                yield encode(terminal_event(result))
        except asyncio.CancelledError:
            discard_events = True
            if event_task is not None:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            # 浏览器关闭、代理断流只结束订阅。独立数据库会话和进程级任务引用
            # 会继续驱动工具链；进程重启时再由持久检查点接管。
            return
        except Exception as exc:  # noqa: BLE001 - 流已开始，只能返回协议错误事件
            error = {
                "type": "error",
                "error": {
                    "message": str(exc),
                    "type": "agent_runtime_error",
                    "code": "agent_runtime_error",
                },
            }
            yield encode(error)
            yield encode(
                {
                    "type": "response.failed",
                    "response": {
                        "id": run_id,
                        "object": "response",
                        "status": "failed",
                        "error": {"message": str(exc)},
                    },
                }
            )
        finally:
            discard_events = True
            if event_task is not None:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
