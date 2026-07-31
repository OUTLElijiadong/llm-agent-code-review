"""管理员副驾驶会话与消息持久化。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.admin_chat import AdminChatMessage, AdminChatSession
from app.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_session(db: Session, admin: User, session_key: str) -> AdminChatSession:
    row = (
        db.query(AdminChatSession)
        .filter(AdminChatSession.user_id == admin.id, AdminChatSession.session_key == session_key)
        .first()
    )
    if row:
        return row
    row = AdminChatSession(user_id=admin.id, session_key=session_key, last_message_at=_utcnow())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def append_message(
    db: Session,
    session: AdminChatSession,
    *,
    role: str,
    payload: dict[str, Any],
    agent_code: str = "",
    trace_id: str = "",
) -> AdminChatMessage:
    action_token = str(payload.get("action_token") or "")
    content = str(payload.get("content") or payload.get("summary") or payload.get("operation") or "")
    row = AdminChatMessage(
        session_id=session.id,
        role=role,
        message_type=str(payload.get("type") or "text"),
        content=content or None,
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        action_token_hash=_token_hash(action_token) if action_token else None,
        action_status=str(payload.get("status") or "pending") if action_token else None,
        agent_code=agent_code or None,
        trace_id=trace_id or None,
    )
    session.last_message_at = _utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def append_user_text(db: Session, session: AdminChatSession, content: str) -> AdminChatMessage:
    return append_message(db, session, role="user", payload={"type": "text", "content": content})


def mark_action(
    db: Session,
    session: AdminChatSession,
    action_token: str,
    status: str,
) -> Optional[AdminChatMessage]:
    token_hash = _token_hash(action_token)
    row = (
        db.query(AdminChatMessage)
        .filter(
            AdminChatMessage.session_id == session.id,
            AdminChatMessage.action_token_hash == token_hash,
            AdminChatMessage.role == "assistant",
        )
        .order_by(AdminChatMessage.id.desc())
        .first()
    )
    if not row:
        return None
    payload = _payload(row)
    payload["status"] = status
    row.action_status = status
    row.payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    db.commit()
    db.refresh(row)
    return row


def list_history(
    db: Session,
    admin: User,
    session_key: str,
    *,
    after_id: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    session = get_or_create_session(db, admin, session_key)
    query = db.query(AdminChatMessage).filter(AdminChatMessage.session_id == session.id)
    if after_id > 0:
        query = query.filter(AdminChatMessage.id > after_id)
    rows = query.order_by(AdminChatMessage.id.asc()).limit(max(1, min(limit, 500))).all()
    return {
        "session_id": session.session_key,
        "messages": [to_dict(row) for row in rows],
        "last_message_id": rows[-1].id if rows else after_id,
    }


def recent_context(db: Session, session: AdminChatSession, limit: int = 12) -> list[dict[str, str]]:
    rows = (
        db.query(AdminChatMessage)
        .filter(AdminChatMessage.session_id == session.id)
        .order_by(AdminChatMessage.id.desc())
        .limit(max(1, min(limit, 30)))
        .all()
    )
    result: list[dict[str, str]] = []
    for row in reversed(rows):
        payload = _payload(row)
        content = str(payload.get("content") or payload.get("summary") or payload.get("operation") or "")
        if content:
            result.append({"role": row.role, "content": content[:2000]})
    return result


def to_dict(row: AdminChatMessage) -> dict[str, Any]:
    return {
        "id": row.id,
        "role": row.role,
        "time": row.create_time.isoformat() if row.create_time else "",
        "payload": _payload(row),
        "agent_code": row.agent_code,
        "trace_id": row.trace_id,
    }


def _payload(row: AdminChatMessage) -> dict[str, Any]:
    try:
        value = json.loads(row.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _token_hash(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()
