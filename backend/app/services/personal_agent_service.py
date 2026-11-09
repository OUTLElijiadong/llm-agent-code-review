"""Private personal Agent definitions; no execution or publication."""

import hashlib
import json
import re

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.personal_agent import PersonalAgent, PersonalAgentVersion
from app.models.user import User
from app.utils.input_validation import normalize_plain_text


def _input(title, prompt, request_key):
    try:
        title = normalize_plain_text(title, field_name="Agent 名称", allow_newlines=False).strip()
        prompt = normalize_plain_text(prompt, field_name="Agent 提示词").strip()
        title.encode("utf-8")
        prompt.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("Agent 名称或提示词包含无效内容") from exc
    if not 1 <= len(title) <= 120 or not 1 <= len(prompt) <= 30000:
        raise ValidationError("Agent 名称或提示词长度无效")
    if not isinstance(request_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,79}", request_key):
        raise ValidationError("请求标识无效")
    return title, prompt


def _digest(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _lock_user(db, user_id):
    user = db.query(User).filter_by(id=user_id, status=1).populate_existing().with_for_update().first()
    if not user:
        raise NotFoundError("账号不可用")


def _agent(db, user_id, agent_id, *, lock=False):
    query = db.query(PersonalAgent).filter_by(id=agent_id, owner_id=user_id)
    row = (query.populate_existing().with_for_update() if lock else query).first()
    if row is None:
        raise NotFoundError("个人 Agent 不存在")
    return row


def current_version(db, row):
    return db.query(PersonalAgentVersion).filter_by(agent_id=row.id, version_number=row.current_version).one()


def create(db, user_id, title, prompt, request_key):
    title, prompt = _input(title, prompt, request_key)
    _lock_user(db, user_id)
    digest = _digest(title, prompt)
    row = db.query(PersonalAgent).filter_by(owner_id=user_id, create_key=request_key).first()
    if row:
        if row.create_hash != digest:
            raise ConflictError("请求标识已用于其他内容")
        return row
    row = PersonalAgent(
        owner_id=user_id, create_key=request_key, create_hash=digest, current_version=1, status="active"
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        original = exc.orig
        args = getattr(original, "args", ())
        duplicate_mysql = bool(args and args[0] == 1062 and "uq_personal_agent_create" in str(original))
        duplicate_sqlite = "UNIQUE constraint failed: personal_agent.owner_id, personal_agent.create_key" in str(
            original
        )
        if not (duplicate_mysql or duplicate_sqlite):
            raise
        # MySQL RR may have opened an old snapshot during authentication before
        # waiting for the owner lock. Recheck in a fresh transaction.
        db.rollback()
        _lock_user(db, user_id)
        row = db.query(PersonalAgent).filter_by(owner_id=user_id, create_key=request_key).first()
        if row is None or row.create_hash != digest:
            raise ConflictError("请求标识已用于其他内容") from exc
        return row
    db.add(
        PersonalAgentVersion(
            agent_id=row.id, version_number=1, title=title, prompt=prompt, request_key=request_key, request_hash=digest
        )
    )
    db.commit()
    return row


def get(db, user_id, agent_id):
    return _agent(db, user_id, agent_id)


def list_agents(db, user_id, *, offset, limit):
    return (
        db.query(PersonalAgent)
        .filter_by(owner_id=user_id)
        .order_by(PersonalAgent.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def versions(db, user_id, agent_id, *, offset, limit):
    _agent(db, user_id, agent_id)
    return (
        db.query(PersonalAgentVersion)
        .filter_by(agent_id=agent_id)
        .order_by(PersonalAgentVersion.version_number.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def revise(db, user_id, agent_id, title, prompt, expected_version, request_key):
    title, prompt = _input(title, prompt, request_key)
    _lock_user(db, user_id)
    row = _agent(db, user_id, agent_id, lock=True)
    digest = _digest(expected_version, title, prompt)
    prior = db.query(PersonalAgentVersion).filter_by(agent_id=agent_id, request_key=request_key).first()
    if prior:
        if prior.request_hash != digest:
            raise ConflictError("请求标识已用于其他内容")
        return row
    if row.status != "active":
        raise ConflictError("Agent 已停用")
    if type(expected_version) is not int or row.current_version != expected_version:
        # A version can have committed while this request waited for the owner
        # lock. Repeat the idempotency lookup in a fresh RR snapshot.
        db.rollback()
        _lock_user(db, user_id)
        row = _agent(db, user_id, agent_id, lock=True)
        prior = db.query(PersonalAgentVersion).filter_by(agent_id=agent_id, request_key=request_key).first()
        if prior:
            if prior.request_hash != digest:
                raise ConflictError("请求标识已用于其他内容")
            return row
        raise ConflictError("Agent 版本已变化，请刷新")
    row.current_version += 1
    db.add(
        PersonalAgentVersion(
            agent_id=row.id,
            version_number=row.current_version,
            title=title,
            prompt=prompt,
            request_key=request_key,
            request_hash=digest,
        )
    )
    db.commit()
    return row


def disable(db, user_id, agent_id, expected_version):
    _lock_user(db, user_id)
    row = _agent(db, user_id, agent_id, lock=True)
    if type(expected_version) is not int or row.current_version != expected_version:
        raise ConflictError("Agent 版本已变化，请刷新")
    if row.status != "disabled":
        row.status = "disabled"
        db.commit()
    return row
