"""普通用户 ChatAgent 与管理员 Agent 共用的 Responses SSE API。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, List, Literal, Mapping, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ForbiddenError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.user import User
from app.schemas.common import Resp
from app.services import rbac_service
from app.services.agent_responses_service import (
    AgentResponsesService,
    AgentSessionExpiredError,
    is_paused,
    redact_agent_event_value,
    redact_agent_output_text,
    terminal_event,
)

router = APIRouter()

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
_STREAM_SESSION_CHECK_INTERVAL = 1.0
_STREAM_WAIT_TIMEOUT = 2.0


def _is_admin_actor(db: Session, user: User) -> bool:
    """兼容旧角色字段，并保留新版 RBAC 管理员绑定。"""

    if str(getattr(user, "role", "")) in {"admin", "super_admin"}:
        return True
    return rbac_service.is_admin_user(db, int(user.id))


def _is_session_version_active(user_id: int, token_version: int) -> bool:
    """使用独立短会话检查长流所属登录是否仍是当前版本。"""

    check_db = SessionLocal()
    try:
        current = check_db.get(User, user_id)
        return bool(
            current
            and current.status == 1
            and int(current.token_version or 0) == token_version
        )
    except Exception:
        return False
    finally:
        check_db.close()


class AgentResponseMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=100_000)


class AgentResponsesRequest(BaseModel):
    action: Literal["start", "approve", "reject", "answer", "retry"] = "start"
    surface: Literal["user", "admin"] = "user"
    session_id: str = Field(min_length=8, max_length=128)
    messages: List[AgentResponseMessage] = Field(default_factory=list, max_length=100)
    run_id: str = Field(default="", max_length=80)
    call_id: str = Field(default="", max_length=160)
    answer: str = Field(default="", max_length=20_000)
    confirmation: str = Field(default="", max_length=20)

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

    @model_validator(mode="after")
    def validate_action_fields(self) -> "AgentResponsesRequest":
        if self.action == "start":
            if not self.messages or not any(item.role == "user" and item.content.strip() for item in self.messages):
                raise ValueError("启动 Agent 时至少需要一条非空用户消息")
        elif not self.run_id:
            raise ValueError("恢复 Agent 运行必须提供 run_id")
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
    if row is not None:
        _recover_stale_active_run(db, row)
        if row.status not in _ACTIVE_RUN_STATUSES:
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
                "pending": None,
            }
        )
    try:
        checkpoint = json.loads(row.checkpoint_json or "{}")
    except (TypeError, json.JSONDecodeError):
        checkpoint = {}
    if not isinstance(checkpoint, Mapping):
        checkpoint = {}
    replay_events = _public_completed_tool_events(db, row, checkpoint)
    return Resp(
        data={
            "surface": surface,
            "session_id": session_id,
            "run": {
                "run_id": row.run_id,
                "status": row.status,
                "model": str(checkpoint.get("model") or ""),
                "rounds": int(checkpoint.get("rounds") or 0),
                "error": _public_text(checkpoint.get("error")),
                "updated_at": row.update_time.isoformat() if row.update_time else "",
            },
            "messages": _public_transcript_messages(checkpoint.get("transcript")),
            "events": replay_events,
            "last_sequence_number": len(replay_events),
            "pending": _public_pending_event(row.run_id, row.status, checkpoint.get("pending")),
        }
    )


def _recover_stale_active_run(db: Session, row: AgentResponseRun) -> None:
    """恢复 Worker 硬退出留下的过渡态；租约长于单次模型超时。"""
    updated_at = row.update_time
    if updated_at is None:
        return
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    lease_seconds = max(int(settings.deepseek_timeout) + 120, 900)
    if (datetime.now(timezone.utc) - updated_at).total_seconds() < lease_seconds:
        return
    try:
        checkpoint = json.loads(row.checkpoint_json or "{}")
    except (TypeError, json.JSONDecodeError):
        checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    waiting_status = _TRANSITION_TO_WAITING.get(row.status)
    if waiting_status and isinstance(checkpoint.get("pending"), Mapping):
        checkpoint["status"] = waiting_status
        checkpoint["error"] = "Worker 中断后已恢复待处理操作"
        row.status = waiting_status
    else:
        checkpoint["status"] = "failed"
        checkpoint["pending"] = None
        checkpoint["error"] = "Worker 在执行中中断，该运行已安全终止，请重新发起任务"
        row.status = "failed"
    row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
    row.version = int(row.version or 0) + 1
    db.commit()


def _public_transcript_messages(value: Any) -> list[dict[str, str]]:
    """只恢复用户可见文本，排除 reasoning、函数参数和工具结果。"""

    if not isinstance(value, list):
        return []
    messages: list[dict[str, str]] = []
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
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                part_type = str(part.get("type") or "")
                visible_types = {"input_text", "text"} if role == "user" else {"output_text", "refusal"}
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
        if text:
            messages.append({"role": role, "content": text})
    return messages[-100:]


def _public_text(value: Any, *, limit: int = 1000) -> str:
    """仅用于工具元数据和错误文本，不改写用户原始输入。"""

    safe_value = redact_agent_event_value(value)
    if safe_value is None:
        return ""
    if isinstance(safe_value, str):
        return safe_value[:limit]
    return str(safe_value)[:limit]


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
    for row in rows:
        try:
            arguments = json.loads(row.arguments_json or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        safe_arguments = redact_agent_event_value(arguments if isinstance(arguments, Mapping) else {})
        sequence += 1
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
        for item in raw_output[:100]:
            if not isinstance(item, Mapping) or str(item.get("type") or "") != "message":
                continue
            content: list[dict[str, str]] = []
            raw_content = item.get("content")
            if isinstance(raw_content, list):
                for part in raw_content[:100]:
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
            "error": redact_agent_event_value(event.get("error")),
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
    if event_type == "auth_expired":
        return {
            "type": "auth_expired",
            "code": 40102,
            "message": "账号已在另一台设备登录，当前设备已下线",
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
    session_user_id = int(user.id)
    session_token_version = int(getattr(user, "token_version", 0) or 0)
    session_is_active = lambda: _is_session_version_active(  # noqa: E731
        session_user_id,
        session_token_version,
    )
    run_id = payload.run_id or f"run_{uuid.uuid4().hex}"
    service = AgentResponsesService(
        db,
        user,
        surface=payload.surface,
        session_key=payload.session_id,
        session_validator=session_is_active,
    )

    async def event_source() -> AsyncIterator[str]:
        queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=128)
        sequence = 0
        discard_events = False
        last_session_check = time.monotonic()

        def session_expired() -> bool:
            nonlocal last_session_check
            now = time.monotonic()
            if now - last_session_check < _STREAM_SESSION_CHECK_INTERVAL:
                return False
            last_session_check = now
            return not session_is_active()

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
            if payload.action == "start":
                messages = [item.model_dump() for item in payload.messages if item.content.strip()]
                return await service.start(messages, run_id=run_id, event_sink=sink)
            return await service.resume(
                run_id=run_id,
                action=payload.action,
                call_id=payload.call_id,
                answer=payload.answer,
                confirmation=payload.confirmation,
                event_sink=sink,
            )

        async def cancel_expired_run() -> None:
            """取消旧登录的在途工作，并避免遗留 running 检查点。"""

            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            cancel = getattr(service, "cancel", None)
            if callable(cancel):
                await cancel(run_id)

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
                "response": {"id": run_id, "object": "response", "status": "in_progress"},
            }
        )
        task = asyncio.create_task(execute())
        event_task: asyncio.Task[Mapping[str, Any]] | None = None
        try:
            while not task.done() or not queue.empty():
                event_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {task, event_task},
                    timeout=_STREAM_WAIT_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if event_task in done:
                    if session_expired():
                        discard_events = True
                        yield encode(
                            {
                                "type": "auth_expired",
                                "code": 40102,
                                "message": "账号已在另一台设备登录，当前设备已下线",
                            }
                        )
                        await cancel_expired_run()
                        return
                    yield encode(event_task.result())
                    event_task = None
                    continue
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
                event_task = None
                if not done:
                    if session_expired():
                        discard_events = True
                        yield encode(
                            {
                                "type": "auth_expired",
                                "code": 40102,
                                "message": "账号已在另一台设备登录，当前设备已下线",
                            }
                        )
                        await cancel_expired_run()
                        return
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
            # 浏览器关闭、代理断流不能中止已经开始的工具链。保持请求级数据库
            # 会话存活，直到运行时把完成/暂停/失败检查点可靠落库。
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            return
        except AgentSessionExpiredError:
            discard_events = True
            yield encode(
                {
                    "type": "auth_expired",
                    "code": 40102,
                    "message": "账号已在另一台设备登录，当前设备已下线",
                }
            )
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

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
