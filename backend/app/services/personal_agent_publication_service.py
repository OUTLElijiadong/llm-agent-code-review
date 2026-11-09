"""Personal-source immutable global publications with separate platform approval."""

import re

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.audit_log import AuditLog
from app.models.personal_agent import PersonalAgent, PersonalAgentPublication, PersonalAgentVersion
from app.models.user import User
from app.services.billing_c4_reviewer_service import _locked_user_and_role
from app.services.personal_agent_service import _digest


def _verify(row):
    if row.content_hash != _digest(
        row.owner_id, row.agent_id, row.version_id, row.version_number, row.title, row.prompt
    ):
        raise ConflictError("发布快照摘要不匹配")


def _current_publication(db, publication_id):
    # Lock only a known primary key. A locking lookup for a missing request
    # key or version can take an InnoDB gap lock and deadlock other authors.
    return (db.query(PersonalAgentPublication).filter_by(id=publication_id)
            .populate_existing().with_for_update().one_or_none())


def _lock_account(db, user_id, token_version=None):
    user = db.query(User).filter_by(id=user_id).populate_existing().with_for_update().one_or_none()
    if user is None or user.status != 1:
        raise NotFoundError("账号不可用")
    if token_version is not None and int(user.token_version or 0) != token_version:
        raise ForbiddenError("账号会话已更新，请重新登录")
    return user


def _admin(db, actor_id, token_version):
    user = _locked_user_and_role(db, actor_id)
    if user.username != "admin" or user.role != "super_admin" or int(user.token_version or 0) != token_version:
        raise ForbiddenError("需要当前平台最高管理员权限")
    return user


def _audit(db, row, actor_id, action, reason=""):
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_type="personal_agent_publication",
            target_id=str(row.id),
            detail=reason[:500],
            status="success",
        )
    )


def submit(db, *, owner_id, agent_id, expected_version, request_key, token_version=None):
    if (
        type(expected_version) is not int
        or expected_version < 1
        or not isinstance(request_key, str)
        or not re.fullmatch(
            r"[a-z0-9][a-z0-9._:-]{0,79}",
            request_key,
        )
    ):
        raise ValidationError("发布版本或请求标识无效")
    user = _lock_account(db, owner_id, token_version)
    if user.username == "admin" and user.role == "super_admin":
        raise ForbiddenError("平台最高管理员不能提交自己的全局发布申请")
    agent = (
        db.query(PersonalAgent).filter_by(id=agent_id, owner_id=owner_id).populate_existing().with_for_update().first()
    )
    if agent is None:
        raise NotFoundError("个人 Agent 不存在")
    # Missing-key locking reads acquire InnoDB gap locks. Different authors
    # must be able to submit in parallel; the two unique constraints arbitrate
    # concurrent inserts even if this transaction has an older RR snapshot.
    existing = db.query(PersonalAgentPublication).filter_by(owner_id=owner_id, request_key=request_key).first()
    if existing:
        existing = _current_publication(db, existing.id)
    if existing:
        _verify(existing)
        if existing.agent_id != agent_id or existing.version_number != expected_version:
            raise ConflictError("请求标识已用于其他发布申请")
        return existing
    if agent.status != "active" or agent.current_version != expected_version:
        raise ConflictError("Agent 已停用或版本已变化")
    version = (
        db.query(PersonalAgentVersion)
        .filter_by(agent_id=agent_id, version_number=expected_version)
        .with_for_update()
        .one()
    )
    duplicate = (
        db.query(PersonalAgentPublication.id)
        .filter(PersonalAgentPublication.agent_id == agent_id,
                PersonalAgentPublication.version_id == version.id,
                PersonalAgentPublication.status.in_(("pending", "approved")))
        .first()
    )
    if duplicate:
        current = _current_publication(db, duplicate.id)
        if current and current.agent_id == agent_id and current.version_id == version.id and current.status in (
            "pending", "approved"
        ):
            raise ConflictError("该版本已有待审核或已发布申请，请查看我的申请")
    row = PersonalAgentPublication(
        owner_id=owner_id,
        agent_id=agent_id,
        version_id=version.id,
        version_number=version.version_number,
        title=version.title,
        prompt=version.prompt,
        content_hash=_digest(owner_id, agent_id, version.id, expected_version, version.title, version.prompt),
        request_key=request_key,
        status="pending",
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        original = exc.orig
        args = getattr(original, "args", ())
        mysql_conflict = bool(
            args and args[0] == 1062
            and ("uq_pa_publication_request" in str(original) or "uq_pa_publication_active_version" in str(original))
        )
        sqlite_conflict = any(
            text in str(original) for text in (
                "UNIQUE constraint failed: personal_agent_publication.owner_id, personal_agent_publication.request_key",
                "UNIQUE constraint failed: personal_agent_publication.active_version_id",
            )
        )
        if not (mysql_conflict or sqlite_conflict):
            raise
        # Authentication or another read may have established an old RR
        # snapshot before the owner lock. Retry the lookup in a fresh
        # transaction and revalidate the account and its current session.
        db.rollback()
        _lock_account(db, owner_id, token_version)
        agent = (db.query(PersonalAgent).filter_by(id=agent_id, owner_id=owner_id)
                 .populate_existing().with_for_update().first())
        if agent is None:
            raise NotFoundError("个人 Agent 不存在") from exc
        existing = db.query(PersonalAgentPublication).filter_by(owner_id=owner_id, request_key=request_key).first()
        if existing is not None:
            _verify(existing)
            if existing.agent_id != agent_id or existing.version_number != expected_version:
                raise ConflictError("请求标识已用于其他发布申请") from exc
            return existing
        raise ConflictError("该版本已有待审核或已发布申请，请查看我的申请") from exc
    _audit(db, row, owner_id, "personal_agent.publication.submit")
    db.commit()
    return row


def withdraw(db, *, owner_id, publication_id, token_version=None):
    _lock_account(db, owner_id, token_version)
    row = (db.query(PersonalAgentPublication).filter_by(id=publication_id, owner_id=owner_id)
           .populate_existing().with_for_update().first())
    if row is None:
        raise NotFoundError("发布申请不存在")
    if row.status != "pending":
        raise ConflictError("只有待审核申请可以撤回")
    _verify(row)
    row.status = "withdrawn"
    _audit(db, row, owner_id, "personal_agent.publication.withdraw")
    db.commit()
    return row


def decide(db, *, actor_id, token_version, publication_id, approve, reason):
    if type(approve) is not bool or not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError("审核决定或说明无效")
    _admin(db, actor_id, token_version)
    # Lock source in owner -> agent -> publication order, like submit; re-read
    # the publication under lock after obtaining those locks.
    identity = (
        db.query(PersonalAgentPublication.owner_id, PersonalAgentPublication.agent_id)
        .filter_by(id=publication_id)
        .first()
    )
    if identity is None:
        raise NotFoundError("发布申请不存在")
    owner = db.query(User).filter_by(id=identity.owner_id).populate_existing().with_for_update().one_or_none()
    agent = db.query(PersonalAgent).filter_by(id=identity.agent_id).populate_existing().with_for_update().one_or_none()
    row = db.query(PersonalAgentPublication).filter_by(id=publication_id).populate_existing().with_for_update().one()
    if row.owner_id == actor_id:
        raise ForbiddenError("发布申请人不能审批自己的申请")
    if row.status != "pending":
        raise ConflictError("发布申请已处理")
    _verify(row)
    if approve:
        version = (
            db.query(PersonalAgentVersion)
            .filter_by(id=row.version_id, agent_id=row.agent_id, version_number=row.version_number)
            .with_for_update()
            .first()
        )
        if (
            owner is None
            or owner.status != 1
            or agent is None
            or agent.owner_id != row.owner_id
            or agent.status != "active"
            or version is None
            or version.title != row.title
            or version.prompt != row.prompt
        ):
            raise ConflictError("申请来源已不可用或内容发生变化")
    row.status = "approved" if approve else "rejected"
    row.decided_by, row.decision_reason = actor_id, reason.strip()
    _audit(db, row, actor_id, "personal_agent.publication." + row.status, reason)
    db.commit()
    return row


def revoke(db, *, actor_id, token_version, publication_id, reason):
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError("下架说明无效")
    _admin(db, actor_id, token_version)
    row = db.query(PersonalAgentPublication).filter_by(id=publication_id).populate_existing().with_for_update().first()
    if row is None:
        raise NotFoundError("发布申请不存在")
    if row.status != "approved":
        raise ConflictError("只有已发布项目可以下架")
    _verify(row)
    row.status, row.revoked_by, row.revocation_reason = "revoked", actor_id, reason.strip()
    _audit(db, row, actor_id, "personal_agent.publication.revoked", reason)
    db.commit()
    return row


def catalog(db, *, offset=0, limit=100):
    rows = (
        db.query(PersonalAgentPublication)
        .filter_by(status="approved")
        .order_by(PersonalAgentPublication.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    for row in rows:
        _verify(row)
    return [{"id": row.id, "title": row.title, "version_number": row.version_number} for row in rows]
