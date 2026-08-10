"""小菱 Agent Mesh 的发现、寻址、投递、回执与追踪。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.contracts import CONTRACTS, collaboration_allowed
from app.agents.registry import AgentRegistry
from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage, AgentMeshMessageEvent
from app.models.custom_agent import CustomAgent
from app.models.user import User
from app.schemas.agent_mesh import AgentMeshAckIn, AgentMeshMessageIn

ONLINE_WINDOW = timedelta(seconds=90)
_SESSION_TERMINAL = {"completed", "failed", "expired", "dead_letter"}


class AgentMeshError(ValueError):
    """Agent Mesh 领域错误。"""


class AgentMeshAccessError(AgentMeshError):
    """消息所有权或协作边界错误。"""


class AgentMeshTargetError(AgentMeshError):
    """目标地址不存在或不可见。"""


class AgentMeshStateError(AgentMeshError):
    """消息状态流转不合法。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> str:
    aware = _aware(value)
    return aware.isoformat() if aware is not None else ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _load(value: Optional[str], fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _is_admin(user: User) -> bool:
    return str(getattr(user, "role", "")) in {"admin", "super_admin"}


def _session_address(surface: str, session_key: str) -> str:
    return f"session:{surface}:{session_key}"


def _assert_surface(user: User, surface: str) -> None:
    if surface not in {"user", "admin"}:
        raise AgentMeshAccessError("surface 必须是 user 或 admin")
    if surface == "admin" and not _is_admin(user):
        raise AgentMeshAccessError("仅管理员账户可注册或访问 admin 会话")


def _conversation(db: Session, user_id: int, surface: str, session_key: str) -> Optional[AgentMeshConversation]:
    return (
        db.query(AgentMeshConversation)
        .filter(
            AgentMeshConversation.user_id == user_id,
            AgentMeshConversation.surface == surface,
            AgentMeshConversation.session_key == session_key,
            AgentMeshConversation.status == "active",
        )
        .first()
    )


def heartbeat(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    title: str,
    active_run_id: str = "",
    active_run_status: str = "",
) -> dict[str, Any]:
    """注册或刷新当前账户的小菱会话。"""
    _assert_surface(user, surface)
    row = _conversation(db, int(user.id), surface, session_key)
    now = _now()
    if row is None:
        row = AgentMeshConversation(
            user_id=int(user.id),
            surface=surface,
            session_key=session_key,
            title=title.strip() or "新对话",
            status="active",
            last_seen_at=now,
        )
        db.add(row)
    row.title = title.strip() or row.title or "新对话"
    row.active_run_id = active_run_id or None
    row.active_run_status = active_run_status or None
    row.last_seen_at = now
    db.commit()
    db.refresh(row)
    return _conversation_out(row, now=now)


def _conversation_out(row: AgentMeshConversation, *, now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or _now()
    last_seen = _aware(row.last_seen_at)
    online = bool(last_seen and current - last_seen <= ONLINE_WINDOW)
    return {
        "address": _session_address(row.surface, row.session_key),
        "name": row.title,
        "kind": "session",
        "status": "online" if online else "offline",
        "capabilities": ["receive_message", "resume_context", "acknowledge"],
        "session_id": row.session_key,
        "surface": row.surface,
        "active_run_id": row.active_run_id or "",
        "active_run_status": row.active_run_status or "",
        "last_seen_at": _iso(row.last_seen_at),
    }


def _runtime_codes() -> set[str]:
    return {str(item.get("code") or "") for item in AgentRegistry.instance().list_runtime()}


def _custom_agents(db: Session) -> list[dict[str, Any]]:
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(CustomAgent.__tablename__):
        return []
    rows = (
        db.query(CustomAgent)
        .filter(CustomAgent.is_enabled == 1, CustomAgent.status == "published")
        .order_by(CustomAgent.code.asc())
        .all()
    )
    return [
        {
            "address": f"custom:{row.code}",
            "name": row.name,
            "kind": "custom",
            "status": "available",
            "capabilities": ["invoke_published_agent"],
            "session_id": "",
            "surface": "",
            "last_seen_at": "",
            "description": row.description or "",
        }
        for row in rows
    ]


def list_agents(db: Session, user: User) -> dict[str, Any]:
    """列出全部内置契约、可调用自定义 Agent 和同账户会话。"""
    runtime_codes = _runtime_codes()
    items: list[dict[str, Any]] = []
    for code, contract in CONTRACTS.items():
        kind = "runtime" if code in runtime_codes or contract.execution_mode in {
            "runtime", "runtime_service", "protected_runtime",
        } else "service"
        items.append({
            "address": f"agent:{code}",
            "name": contract.name,
            "kind": kind,
            "status": "registered" if code in runtime_codes else "available",
            "capabilities": [skill.code for skill in contract.skills],
            "session_id": "",
            "surface": "",
            "last_seen_at": "",
            "description": contract.mission,
        })
    items.extend(_custom_agents(db))
    conversations = (
        db.query(AgentMeshConversation)
        .filter(
            AgentMeshConversation.user_id == int(user.id),
            AgentMeshConversation.status == "active",
        )
        .order_by(AgentMeshConversation.last_seen_at.desc(), AgentMeshConversation.id.desc())
        .all()
    )
    now = _now()
    items.extend(_conversation_out(row, now=now) for row in conversations)
    by_kind: dict[str, int] = {}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {"items": items, "total": len(items), "by_kind": by_kind}


def _validate_target(db: Session, user: User, address: str) -> str:
    if address.startswith("agent:"):
        code = address.split(":", 1)[1]
        if code not in CONTRACTS:
            raise AgentMeshTargetError("目标 Agent 不存在或未登记")
        return "agent"
    if address.startswith("custom:"):
        code = address.split(":", 1)[1]
        if not any(item["address"] == address for item in _custom_agents(db)):
            raise AgentMeshTargetError(f"已发布 Agent {code} 不存在或不可调用")
        return "custom"
    parts = address.split(":", 2)
    if len(parts) == 3 and parts[0] == "session" and parts[1] in {"user", "admin"}:
        if _conversation(db, int(user.id), parts[1], parts[2]) is None:
            raise AgentMeshTargetError("目标会话不存在、已归档或不属于当前账户")
        return "session"
    raise AgentMeshTargetError("目标地址格式非法")


def _event(
    db: Session,
    row: AgentMeshMessage,
    status: str,
    actor: str,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    db.add(AgentMeshMessageEvent(
        message_id=row.message_id,
        user_id=row.user_id,
        trace_id=row.trace_id,
        status=status,
        actor_address=actor,
        detail_json=_json(dict(detail or {})),
    ))


def _message_out(row: AgentMeshMessage, *, include_body: bool = True) -> dict[str, Any]:
    data = {
        "schema_version": row.schema_version,
        "message_id": row.message_id,
        "idempotency_key": row.idempotency_key,
        "trace_id": row.trace_id,
        "correlation_id": row.correlation_id,
        "causation_id": row.causation_id,
        "sent_from": row.sent_from,
        "send_to": row.send_to,
        "message_type": row.message_type,
        "priority": row.priority,
        "subject": row.subject,
        "status": row.status,
        "requires_ack": bool(row.requires_ack),
        "max_attempts": row.max_attempts,
        "attempt_count": row.attempt_count,
        "expires_at": _iso(row.expires_at),
        "create_time": _iso(row.create_time),
        "update_time": _iso(row.update_time),
    }
    if include_body:
        data.update({
            "payload": _load(row.payload_json, {}),
            "context": _load(row.context_json, {}),
            "artifacts": _load(row.artifacts_json, []),
            "errors": _load(row.errors_json, []),
        })
    return data


def send_message(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    message: AgentMeshMessageIn,
    trusted_source: bool = False,
) -> dict[str, Any]:
    """校验并持久化标准消息；相同幂等键返回原记录。"""
    _assert_surface(user, surface)
    if _conversation(db, int(user.id), surface, session_key) is None:
        raise AgentMeshAccessError("发送会话尚未注册或不属于当前账户")
    derived_source = _session_address(surface, session_key)
    source = message.sent_from or derived_source
    if not trusted_source and source != derived_source:
        raise AgentMeshAccessError("sent_from 与当前认证会话不一致")
    target_kind = _validate_target(db, user, message.send_to)
    if trusted_source and source.startswith("agent:") and message.send_to.startswith("agent:"):
        source_code = source.split(":", 1)[1]
        target_code = message.send_to.split(":", 1)[1]
        if not collaboration_allowed(source_code, target_code):
            raise AgentMeshAccessError(f"Agent 协作越界: {source_code} -> {target_code}")

    existing = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.idempotency_key == message.idempotency_key,
        )
        .first()
    )
    if existing is not None:
        if existing.sent_from != source or existing.send_to != message.send_to:
            raise AgentMeshStateError("幂等键已用于不同的发送方或目标")
        return _message_out(existing)

    now = _now()
    row = AgentMeshMessage(
        message_id=f"msg_{uuid.uuid4().hex}",
        user_id=int(user.id),
        schema_version=message.schema_version,
        idempotency_key=message.idempotency_key,
        trace_id=message.trace_id or f"trc_{uuid.uuid4().hex}",
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        sent_from=source,
        send_to=message.send_to,
        message_type=message.message_type,
        priority=message.priority,
        subject=message.subject,
        payload_json=_json(message.payload),
        context_json=_json(message.context.model_dump()),
        artifacts_json=_json(message.artifacts),
        errors_json=_json(message.errors),
        status="queued",
        requires_ack=1 if message.delivery.requires_ack else 0,
        max_attempts=message.delivery.max_attempts,
        attempt_count=0,
        expires_at=message.delivery.expires_at,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(AgentMeshMessage)
            .filter(
                AgentMeshMessage.user_id == int(user.id),
                AgentMeshMessage.idempotency_key == message.idempotency_key,
            )
            .first()
        )
        if existing is None:
            raise
        return _message_out(existing)
    _event(db, row, "queued", source)
    if target_kind in {"agent", "custom"}:
        row.status = "delivered"
        row.delivered_at = now
        row.attempt_count = 1
        _event(db, row, "delivered", message.send_to, {"delivery_kind": target_kind})
    source_parts = derived_source.split(":", 2)
    source_conversation = _conversation(db, int(user.id), source_parts[1], source_parts[2])
    if source_conversation is not None:
        source_conversation.last_message_at = now
    if target_kind == "session":
        target_parts = message.send_to.split(":", 2)
        target_conversation = _conversation(db, int(user.id), target_parts[1], target_parts[2])
        if target_conversation is not None:
            target_conversation.last_message_at = now
    db.commit()
    db.refresh(row)
    return _message_out(row)


def _expire_pending(db: Session, rows: list[AgentMeshMessage], now: datetime) -> None:
    for row in rows:
        expires_at = _aware(row.expires_at)
        if expires_at is None or expires_at > now or row.status in _SESSION_TERMINAL:
            continue
        row.status = "expired"
        row.completed_at = now
        _event(db, row, "expired", "system", {"reason": "delivery deadline exceeded"})


def pull_inbox(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """拉取当前会话的待处理消息，并原子标记首次送达。"""
    _assert_surface(user, surface)
    if _conversation(db, int(user.id), surface, session_key) is None:
        raise AgentMeshAccessError("目标会话尚未注册或不属于当前账户")
    target = _session_address(surface, session_key)
    rows = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.send_to == target,
            AgentMeshMessage.status.in_(("queued", "delivered")),
        )
        .order_by(AgentMeshMessage.id.asc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    now = _now()
    _expire_pending(db, rows, now)
    delivered: list[AgentMeshMessage] = []
    for row in rows:
        if row.status == "expired":
            continue
        if row.status == "queued":
            row.status = "delivered"
            row.delivered_at = now
            row.attempt_count = int(row.attempt_count or 0) + 1
            _event(db, row, "delivered", target)
        delivered.append(row)
    db.commit()
    return [_message_out(row) for row in delivered]


def _owned_target_message(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
) -> AgentMeshMessage:
    _assert_surface(user, surface)
    target = _session_address(surface, session_key)
    row = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.message_id == message_id,
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.send_to == target,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        raise AgentMeshTargetError("消息不存在或不属于当前会话")
    return row


def ack_message(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
    acknowledgement: AgentMeshAckIn,
) -> dict[str, Any]:
    """推进消息状态；失败在预算内重新排队，超限进入死信。"""
    row = _owned_target_message(db, user, message_id, surface=surface, session_key=session_key)
    now = _now()
    requested = acknowledgement.status
    allowed = {
        "queued": {"acknowledged", "processing"},
        "delivered": {"acknowledged", "processing", "completed", "failed"},
        "acknowledged": {"processing", "completed", "failed"},
        "processing": {"completed", "failed"},
    }
    if requested == row.status:
        return _message_out(row)
    if requested not in allowed.get(row.status, set()):
        raise AgentMeshStateError(f"消息不能从 {row.status} 转为 {requested}")

    actor = _session_address(surface, session_key)
    if requested == "acknowledged":
        row.status = requested
        row.acknowledged_at = now
        _event(db, row, requested, actor, {"summary": acknowledgement.summary})
    elif requested == "processing":
        row.status = requested
        row.acknowledged_at = row.acknowledged_at or now
        row.processing_at = now
        _event(db, row, requested, actor, {"summary": acknowledgement.summary})
    elif requested == "completed":
        row.status = requested
        row.completed_at = now
        _event(db, row, requested, actor, {"summary": acknowledgement.summary})
    else:
        row.last_error = acknowledgement.error or acknowledgement.summary or "消息处理失败"
        _event(db, row, "failed", actor, {"error": row.last_error})
        if int(row.attempt_count or 0) < int(row.max_attempts or 1):
            row.status = "queued"
            _event(db, row, "queued", "system", {"reason": "retry scheduled"})
        else:
            row.status = "dead_letter"
            row.completed_at = now
            _event(db, row, "dead_letter", "system", {"error": row.last_error})
    db.commit()
    db.refresh(row)
    return _message_out(row)


def get_trace(db: Session, user: User, trace_id: str) -> dict[str, Any]:
    """按账户恢复一条 trace 的完整消息与状态时间线。"""
    rows = (
        db.query(AgentMeshMessage)
        .filter(AgentMeshMessage.user_id == int(user.id), AgentMeshMessage.trace_id == trace_id)
        .order_by(AgentMeshMessage.id.asc())
        .all()
    )
    if not rows:
        raise AgentMeshTargetError("追踪链不存在")
    message_ids = [row.message_id for row in rows]
    events = (
        db.query(AgentMeshMessageEvent)
        .filter(
            AgentMeshMessageEvent.user_id == int(user.id),
            AgentMeshMessageEvent.message_id.in_(message_ids),
        )
        .order_by(AgentMeshMessageEvent.id.asc())
        .all()
    )
    by_message: dict[str, list[dict[str, Any]]] = {message_id: [] for message_id in message_ids}
    for event in events:
        by_message[event.message_id].append({
            "status": event.status,
            "actor_address": event.actor_address,
            "detail": _load(event.detail_json, {}),
            "create_time": _iso(event.create_time),
        })
    output = []
    for row in rows:
        item = _message_out(row)
        item["events"] = by_message[row.message_id]
        output.append(item)
    return {"trace_id": trace_id, "messages": output, "total": len(output)}


def list_session_messages(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """返回当前账户会话收发的最近消息，供折叠时间线恢复。"""
    _assert_surface(user, surface)
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(AgentMeshMessage.__tablename__):
        return []
    address = _session_address(surface, session_key)
    rows = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.user_id == int(user.id),
            (AgentMeshMessage.sent_from == address) | (AgentMeshMessage.send_to == address),
        )
        .order_by(AgentMeshMessage.id.desc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    return [_message_out(row) for row in reversed(rows)]


def prepare_message_run(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
) -> tuple[AgentMeshMessage, dict[str, Any]]:
    """为自动续跑认领消息，并返回不会伪装成用户文本的 system input。"""
    row = _owned_target_message(db, user, message_id, surface=surface, session_key=session_key)
    if row.status == "queued":
        pull_inbox(db, user, surface=surface, session_key=session_key, limit=100)
        db.refresh(row)
    if row.status not in {"delivered", "acknowledged"}:
        raise AgentMeshStateError(f"消息当前状态 {row.status} 不能启动新回合")
    ack_message(
        db,
        user,
        message_id,
        surface=surface,
        session_key=session_key,
        acknowledgement=AgentMeshAckIn(status="processing"),
    )
    db.refresh(row)
    envelope = _message_out(row)
    system_input = {
        "role": "system",
        "content": (
            "你收到一条来自同一账户其他会话或专业 Agent 的结构化协作消息。"
            "只依据消息事实处理；需要执行工具时继续遵守权限和审批；不要把该消息伪装成用户发言。\n"
            + _json(envelope)
        ),
    }
    return row, system_input


def finish_message_run(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
    success: bool,
    summary: str = "",
    error: str = "",
) -> dict[str, Any]:
    """把自动 Responses 回合结果回写消息状态。"""
    return ack_message(
        db,
        user,
        message_id,
        surface=surface,
        session_key=session_key,
        acknowledgement=AgentMeshAckIn(
            status="completed" if success else "failed",
            summary=summary[:2000],
            error=error[:2000],
        ),
    )
