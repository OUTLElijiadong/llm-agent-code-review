"""企业成员与小组操作；调用方负责事务提交，授权只认可企业内成员关系。"""
import hashlib
import secrets
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from typing import Iterator

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseAuditEvent,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseInvitation,
    EnterpriseMember,
)
from app.models.enterprise_project import EnterpriseProjectGrant
from app.models.user import User


def _now() -> datetime:
    return datetime.utcnow()


def _event(db: Session, enterprise_id: int, actor_id: int, action: str, *,
           subject_user_id: int | None = None, group_id: int | None = None, detail: dict | None = None) -> None:
    db.add(EnterpriseAuditEvent(enterprise_id=enterprise_id, actor_id=actor_id, action=action,
                                subject_user_id=subject_user_id, group_id=group_id, detail=detail or {}))


def _advance_billing_group_version(member: EnterpriseMember) -> None:
    """Call with the enterprise member row locked, in the membership transaction."""
    version = member.billing_group_version
    if version is None or version < 0 or version >= 2**63 - 1:
        raise ConflictError("成员小组计费归属版本异常，请联系管理员")
    member.billing_group_version = version + 1


def _company(
    db: Session, enterprise_id: int, *, lock: bool = False, shared_lock: bool = False
) -> Enterprise:
    query = db.query(Enterprise).filter(Enterprise.id == enterprise_id)
    # A locking read must refresh an Enterprise already present in the identity
    # map; under MySQL REPEATABLE READ, otherwise a prior consistent read can
    # leave a stale status on the ORM object even though this SELECT is current.
    # Authorization readers may request a shared current read: organization
    # writers still wait on this row, while independent readers can proceed.
    if lock:
        query = query.populate_existing().with_for_update(read=shared_lock)
    company = query.first()
    if not company or company.status not in {"active", "suspended"}:
        raise NotFoundError("企业不存在")
    return company


def _group(
    db: Session,
    enterprise_id: int,
    group_id: int,
    *,
    lock: bool = False,
    shared_lock: bool = False,
) -> EnterpriseGroup:
    query = db.query(EnterpriseGroup).filter_by(
        id=group_id,
        enterprise_id=enterprise_id,
        status="active",
    )
    if lock:
        query = query.populate_existing().with_for_update(read=shared_lock)
    group = query.first()
    if not group:
        raise NotFoundError("小组不存在")
    return group


def _active_member_record(db: Session, enterprise_id: int, user_id: int, *, lock: bool = False) -> EnterpriseMember:
    """管理对象允许账号已禁用，以便企业及时移除并释放席位。"""
    query = db.query(EnterpriseMember).filter_by(
        enterprise_id=enterprise_id, user_id=user_id, status="active"
    )
    if lock:
        query = query.populate_existing().with_for_update()
    member = query.first()
    if not member:
        raise NotFoundError("企业成员不存在")
    return member


def _active_member_count_locked(db: Session, enterprise_id: int) -> int:
    """Count seats with a current read while the caller holds the company lock.

    Invitation acceptance first reads its token, which can establish an InnoDB
    REPEATABLE READ snapshot before it waits on the company row. A plain COUNT
    after that wait would still see the old member count and can overfill seats.
    All callers must lock the company before invoking this helper; FOR UPDATE
    makes the count itself a current read on MySQL while preserving one query.
    """
    count = db.query(func.count(EnterpriseMember.id)).filter_by(
        enterprise_id=enterprise_id, status="active",
    ).with_for_update().scalar()
    return int(count or 0)


@contextmanager
def _billing_authorization_session(db: Session, billing_db: Session | None = None) -> Iterator[Session]:
    """Read current authorization/billing state without taking refund locks."""
    if billing_db is not None:
        yield billing_db
        return
    bind = db.get_bind()
    bind = getattr(bind, "engine", bind)
    if bind.dialect.name != "mysql":
        yield db
        return
    # Authentication often queried User in the request transaction before an
    # enterprise permission check. Under InnoDB REPEATABLE READ that snapshot
    # can predate a refund entering processing. A short, read-only Session sees
    # the committed billing state; it deliberately takes no refund/order locks,
    # preserving the order -> enterprise -> refund writer lock order.
    with Session(bind=bind, autoflush=False, expire_on_commit=False) as fresh:
        yield fresh


def require_enterprise_member(db: Session, enterprise_id: int, user_id: int, *,
                              lock: bool = False, billing_db: Session | None = None,
                              require_billing_access: bool = True, nowait: bool = False) -> EnterpriseMember:
    company = _company(db, enterprise_id)
    query = db.query(EnterpriseMember).join(User, User.id == EnterpriseMember.user_id).filter(
        EnterpriseMember.enterprise_id == enterprise_id, EnterpriseMember.user_id == user_id,
        EnterpriseMember.status == "active", User.status == 1,
    )
    if lock:
        query = query.populate_existing()
        query = query.with_for_update(nowait=True) if nowait else query.with_for_update()
    member = query.first()
    if not member:
        raise ForbiddenError("没有该企业的访问权限")
    # The owner and company admins retain the minimum access needed to inspect
    # the enterprise and renew it. Ordinary members are blocked after the
    # existing seven-day subscription grace period; none preserves legacy/test
    # enterprises that predate paid subscription records.
    from app.services.billing_checkout_service import enterprise_subscription_status

    with _billing_authorization_session(db, billing_db) as billing_session:
        billing_company = billing_session.query(Enterprise).filter_by(id=enterprise_id).first() or company
        live_member_role = (billing_session.query(EnterpriseMember.role)
                            .join(User, User.id == EnterpriseMember.user_id)
                            .filter(
                                EnterpriseMember.enterprise_id == enterprise_id,
                                EnterpriseMember.user_id == user_id,
                                EnterpriseMember.status == "active",
                                User.status == 1,
                            ).scalar())
        # A current locking read above is authoritative for lock=True and may
        # see a member created or changed in this still-uncommitted transaction;
        # the independent billing Session intentionally cannot see that write.
        # Non-locking paths must fail closed when the fresh membership snapshot
        # shows removal or a role change since the request snapshot.
        if not lock and (live_member_role is None or str(live_member_role) != str(member.role)):
            raise ForbiddenError("企业成员权限已变化，请刷新后重试")
        if require_billing_access and member.role not in {"owner", "admin"} and _billing_access_suspended(
            billing_session, billing_company, local_billing_suspended=bool(company.billing_suspended),
        ):
            message = (
                "企业订阅退款处理中，成员访问已暂停"
                if not (company.billing_suspended or billing_company.billing_suspended)
                else "企业订阅已撤销，成员访问已暂停"
            )
            raise ForbiddenError(message)

        if (require_billing_access and enterprise_subscription_status(billing_session, enterprise_id) == "expired"
                and member.role not in {"owner", "admin"}):
            raise ForbiddenError("企业订阅已过期，请联系企业管理员续费")
    return member


def require_enterprise_execution(db: Session, enterprise_id: int) -> None:
    """Keep management access during billing recovery without permitting paid work."""
    status, billing_suspended, access_suspended, subscription_status = _current_billing_state(
        db, enterprise_id,
    )
    if status != "active":
        raise ForbiddenError("企业当前暂停执行任务")
    if billing_suspended or access_suspended:
        raise ForbiddenError("企业计费已暂停，不能执行项目任务")
    if subscription_status == "expired":
        raise ForbiddenError("企业订阅已过期，续费后才能执行项目任务")


def require_enterprise_admin(db: Session, enterprise_id: int, user_id: int, *,
                             owner_only: bool = False, lock: bool = False,
                             require_billing_access: bool = True) -> EnterpriseMember:
    member = require_enterprise_member(
        db, enterprise_id, user_id, lock=lock, require_billing_access=require_billing_access,
    )
    roles = {"owner"} if owner_only else {"owner", "admin"}
    if member.role not in roles:
        raise ForbiddenError("需要企业管理权限")
    return member


def require_group_member(
    db: Session,
    enterprise_id: int,
    group_id: int,
    user_id: int,
    *,
    allow_admin: bool = True,
    lock: bool = False,
    shared_group_lock: bool = False,
    require_billing_access: bool = True,
):
    member = require_enterprise_member(
        db, enterprise_id, user_id, lock=lock, require_billing_access=require_billing_access,
    )
    session_context = nullcontext(db) if lock else _billing_authorization_session(db)
    with session_context as authorization_db:
        _group(authorization_db, enterprise_id, group_id, lock=lock, shared_lock=shared_group_lock)
        if allow_admin and member.role in {"owner", "admin"}:
            return member
        query = authorization_db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=enterprise_id, group_id=group_id, user_id=user_id, status="active",
        )
        group_member = (query.populate_existing().with_for_update() if lock else query).first()
        if not group_member:
            raise ForbiddenError("没有该小组的访问权限")
        return group_member


def require_group_manager(
    db: Session,
    enterprise_id: int,
    group_id: int,
    user_id: int,
    *,
    allow_admin: bool = True,
    lock: bool = False,
    shared_group_lock: bool = False,
    require_billing_access: bool = True,
):
    member = require_group_member(
        db,
        enterprise_id,
        group_id,
        user_id,
        allow_admin=allow_admin,
        lock=lock,
        shared_group_lock=shared_group_lock,
        require_billing_access=require_billing_access,
    )
    if member.role not in {"owner", "admin", "leader"}:
        raise ForbiddenError("需要该小组的管理权限")
    return member


def create_enterprise_from_paid_order(db: Session, *, owner_id: int, name: str,
                                      seat_limit: int, paid_order_id: str) -> Enterprise:
    """仅供已验证支付成功的服务层调用；调用方必须在同一事务核验真实订单。"""
    name = name.strip()
    order_id = str(paid_order_id).strip()
    if not name or len(name) > 120 or not order_id or len(order_id) > 64 or not 1 <= seat_limit <= 10000:
        raise ValidationError("企业名称、席位数或订单无效")
    owner = db.get(User, owner_id)
    if not owner or owner.status != 1:
        raise ForbiddenError("企业所有者账号不可用")
    company = db.query(Enterprise).filter_by(paid_order_id=order_id).first()
    if company:
        if company.owner_id != owner_id or company.name != name or company.seat_limit != seat_limit:
            raise ConflictError("订单已绑定不同企业")
        return company
    company = Enterprise(name=name, owner_id=owner_id, seat_limit=seat_limit,
                         paid_order_id=order_id, status="active")
    db.add(company)
    db.flush()
    db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner_id, role="owner", status="active"))
    _event(db, company.id, owner_id, "enterprise_created")
    db.flush()
    return company


def create_invitation(db: Session, enterprise_id: int, actor_id: int, *, ttl_hours: int = 24) -> str:
    company = _company(db, enterprise_id, lock=True)
    require_enterprise_admin(db, enterprise_id, actor_id, lock=True)
    if company.status != "active" or company.billing_suspended:
        raise ConflictError("企业当前不能邀请或替换成员")
    _, _, access_suspended, subscription_status = _current_billing_state(
        db, enterprise_id, local_company=company,
    )
    if access_suspended:
        raise ConflictError("企业订阅退款处理中，暂不能邀请成员")
    if subscription_status == "expired":
        raise ConflictError("企业订阅已过期，续费后才能邀请成员")
    active_count = _active_member_count_locked(db, enterprise_id)
    if active_count >= company.seat_limit:
        raise ConflictError("企业席位不足，无法发出新邀请")
    if not 1 <= ttl_hours <= 168:
        raise ValidationError("邀请有效期必须为 1 至 168 小时")
    token = secrets.token_urlsafe(32)
    db.add(EnterpriseInvitation(enterprise_id=enterprise_id, created_by=actor_id,
                                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                                expires_at=_now() + timedelta(hours=ttl_hours), max_uses=1, use_count=0))
    _event(db, enterprise_id, actor_id, "invitation_created")
    db.flush()
    return token


def accept_invitation(db: Session, token: str, user_id: int) -> EnterpriseMember:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    invitation = db.query(EnterpriseInvitation).filter_by(token_hash=token_hash).first()
    if not invitation:
        raise NotFoundError("邀请不存在或已失效")
    company = _company(db, invitation.enterprise_id, lock=True)
    # Preserve changes the caller intentionally made in this transaction before
    # refreshing the row; keep the company -> invitation lock order intact.
    if db.is_modified(invitation, include_collections=False):
        db.flush([invitation])
    # The first lookup may already have put a pre-revocation invitation in
    # the identity map. A current locking SELECT alone does not overwrite it.
    invitation = (db.query(EnterpriseInvitation).filter_by(id=invitation.id)
                  .populate_existing().with_for_update().first())
    if invitation is None or company.status != "active" or company.billing_suspended \
            or invitation.revoked_at or invitation.expires_at <= _now() \
            or invitation.use_count >= invitation.max_uses:
        raise ConflictError("邀请已失效或企业不可加入")
    _, _, access_suspended, subscription_status = _current_billing_state(
        db, int(company.id), local_company=company,
    )
    if access_suspended:
        raise ConflictError("企业订阅退款处理中，暂不能加入")
    if subscription_status == "expired":
        raise ConflictError("企业订阅已过期，暂不能加入")
    user = db.query(User).filter_by(id=user_id).populate_existing().with_for_update().first()
    if not user or user.status != 1:
        raise ForbiddenError("账号不可用")
    existing = (db.query(EnterpriseMember).filter_by(enterprise_id=company.id, user_id=user_id)
                .populate_existing().with_for_update().first())
    if existing and existing.status == "active":
        raise ConflictError("已是该企业成员")
    count = _active_member_count_locked(db, int(company.id))
    if count >= company.seat_limit:
        raise ConflictError("企业席位不足")
    if existing:
        _advance_billing_group_version(existing)
        existing.status = "active"
        existing.role = "member"
        existing.default_group_id = None
        member = existing
    else:
        member = EnterpriseMember(enterprise_id=company.id, user_id=user_id, role="member", status="active")
        db.add(member)
    invitation.use_count += 1
    _event(db, company.id, user_id, "invitation_accepted", subject_user_id=user_id)
    db.flush()
    return member


def _subscription_status(db: Session, enterprise_id: int) -> str:
    # Import lazily because billing_checkout_service depends on this module for
    # post-payment enterprise creation and period activation.
    from app.services.billing_checkout_service import enterprise_subscription_status

    return enterprise_subscription_status(db, enterprise_id)


def _billing_access_suspended(db: Session, company: Enterprise, *, local_billing_suspended: bool = False) -> bool:
    if company.billing_suspended or local_billing_suspended:
        return True
    from app.services.billing_checkout_service import enterprise_subscription_refund_pending

    return (enterprise_subscription_refund_pending(db, int(company.id))
            and _subscription_status(db, int(company.id)) not in {"active", "grace"})


def _current_billing_state(
    db: Session, enterprise_id: int, *, local_company: Enterprise | None = None,
) -> tuple[str, bool, bool, str]:
    """Read committed enterprise billing state outside a possibly stale RR request snapshot.

    Management and execution requests commonly read the authenticated User before reaching
    organization services. Under MySQL REPEATABLE READ, subsequent plain reads in that request
    can keep seeing a refund as `requested` after another transaction commits `processing`.
    Do not lock refund/order rows here: subscription-refund writers lock in order -> enterprise
    -> refund order, and taking those locks while holding an enterprise lock would invert it.
    """
    with _billing_authorization_session(db) as billing_db:
        company = billing_db.query(Enterprise).filter_by(id=enterprise_id).first()
        # Some service flows create the enterprise and its first invitation inside one
        # transaction. A fresh Session cannot see that transaction's uncommitted row.
        if company is None and local_company is not None and int(local_company.id) == enterprise_id:
            company = local_company
        if company is None:
            raise NotFoundError("企业不存在")
        status = str(company.status or "")
        company_suspended = bool(company.billing_suspended)
        access_suspended = _billing_access_suspended(billing_db, company)
        subscription_status = _subscription_status(billing_db, enterprise_id)
        return status, company_suspended, access_suspended, subscription_status


def apply_subscription_seats(db: Session, *, enterprise_id: int, seat_limit: int, actor_id: int) -> Enterprise:
    """Apply a paid period's total seat count at period start; never remove members."""
    if type(seat_limit) is not int or not 1 <= seat_limit <= 10000:
        raise ValidationError("订阅席位数无效")
    company = _company(db, enterprise_id, lock=True)
    old_limit = company.seat_limit
    if old_limit != seat_limit:
        active_count = _active_member_count_locked(db, enterprise_id)
        company.seat_limit = seat_limit
        _event(db, enterprise_id, actor_id, "subscription_seats_changed", detail={
            "previous_seat_limit": old_limit,
            "seat_limit": seat_limit,
            "active_member_count": active_count,
            "over_capacity": active_count > seat_limit,
        })
        db.flush()
    return company


def revoke_invitation(db: Session, enterprise_id: int, actor_id: int, invitation_id: int) -> None:
    _company(db, enterprise_id, lock=True)
    require_enterprise_admin(db, enterprise_id, actor_id, lock=True)
    invite = db.query(EnterpriseInvitation).filter_by(
        id=invitation_id, enterprise_id=enterprise_id
    ).with_for_update().first()
    if not invite:
        raise NotFoundError("邀请不存在")
    if not invite.revoked_at:
        invite.revoked_at = _now()
        _event(db, enterprise_id, actor_id, "invitation_revoked", detail={"invitation_id": invitation_id})
    db.flush()


def transfer_enterprise_owner(db: Session, enterprise_id: int, actor_id: int, user_id: int) -> Enterprise:
    company = _company(db, enterprise_id, lock=True)
    old_owner = require_enterprise_admin(db, enterprise_id, actor_id, owner_only=True, lock=True)
    if user_id == actor_id:
        raise ConflictError("不能向自己转移企业所有权")
    new_owner = require_enterprise_member(db, enterprise_id, user_id, lock=True)
    old_owner.role = "admin"
    new_owner.role = "owner"
    company.owner_id = user_id
    _event(db, enterprise_id, actor_id, "owner_transferred", subject_user_id=user_id)
    db.flush()
    return company


def set_enterprise_role(db: Session, enterprise_id: int, actor_id: int, user_id: int, role: str) -> EnterpriseMember:
    _company(db, enterprise_id, lock=True)
    require_enterprise_admin(db, enterprise_id, actor_id, owner_only=True, lock=True)
    if role not in {"admin", "member"}:
        raise ValidationError("企业角色无效")
    target = require_enterprise_member(db, enterprise_id, user_id, lock=True)
    if target.role == "owner":
        raise ConflictError("不能修改企业所有者角色")
    target.role = role
    _event(db, enterprise_id, actor_id, "member_role_changed", subject_user_id=user_id, detail={"role": role})
    db.flush()
    return target


def remove_enterprise_member(db: Session, enterprise_id: int, actor_id: int, user_id: int) -> None:
    _company(db, enterprise_id, lock=True)
    actor = require_enterprise_admin(db, enterprise_id, actor_id, lock=True)
    target = _active_member_record(db, enterprise_id, user_id, lock=True)
    if target.role == "owner":
        raise ConflictError("不能移除企业所有者")
    if target.role == "admin" and actor.role != "owner":
        raise ForbiddenError("只有企业所有者能移除企业管理员")
    _advance_billing_group_version(target)
    target.status = "removed"
    target.default_group_id = None
    rows = db.query(EnterpriseGroupMember).filter_by(
        enterprise_id=enterprise_id, user_id=user_id, status="active"
    ).populate_existing().with_for_update().all()
    for row in rows:
        row.status = "removed"
    grants = (db.query(EnterpriseProjectGrant).filter_by(
        enterprise_id=enterprise_id, user_id=user_id, status="active",
    ).populate_existing().with_for_update().all())
    for grant in grants:
        grant.status = "inactive"
    _event(db, enterprise_id, actor_id, "member_removed", subject_user_id=user_id,
           detail={"revoked_project_grants": len(grants)})
    db.flush()


def create_group(db: Session, enterprise_id: int, actor_id: int, name: str) -> EnterpriseGroup:
    _company(db, enterprise_id, lock=True)
    require_enterprise_admin(db, enterprise_id, actor_id, lock=True)
    name = name.strip()
    if not name or len(name) > 120:
        raise ValidationError("小组名称无效")
    if (db.query(EnterpriseGroup).filter_by(enterprise_id=enterprise_id, name=name)
            .populate_existing().with_for_update().first()):
        raise ConflictError("小组名称已存在")
    group = EnterpriseGroup(enterprise_id=enterprise_id, name=name, status="active")
    db.add(group)
    db.flush()
    _event(db, enterprise_id, actor_id, "group_created", group_id=group.id)
    return group


def set_group_member(db: Session, enterprise_id: int, group_id: int, actor_id: int,
                     user_id: int, role: str) -> EnterpriseGroupMember:
    _company(db, enterprise_id, lock=True)
    actor = require_group_manager(db, enterprise_id, group_id, actor_id, lock=True)
    target = require_enterprise_member(db, enterprise_id, user_id, lock=True)
    if role not in {"leader", "member"}:
        raise ValidationError("小组角色无效")
    row = db.query(EnterpriseGroupMember).filter_by(
        enterprise_id=enterprise_id, group_id=group_id, user_id=user_id
    ).populate_existing().with_for_update().first()
    if actor.role == "leader" and (role == "leader" or target.role in {"owner", "admin"}
                                    or (row and row.status == "active" and row.role == "leader")):
        raise ForbiddenError("小组长只能管理本组普通成员")
    if row:
        if row.status != "active":
            _advance_billing_group_version(target)
        row.status, row.role = "active", role
    else:
        _advance_billing_group_version(target)
        row = EnterpriseGroupMember(enterprise_id=enterprise_id, group_id=group_id,
                                    user_id=user_id, role=role, status="active")
        db.add(row)
    _event(db, enterprise_id, actor_id, "group_member_changed", subject_user_id=user_id,
           group_id=group_id, detail={"role": role})
    db.flush()
    return row


def remove_group_member(db: Session, enterprise_id: int, group_id: int, actor_id: int, user_id: int) -> None:
    _company(db, enterprise_id, lock=True)
    # The company lock serializes role changes, so validate the manager before
    # touching the target. Keep this check non-locking to avoid holding the
    # group's exclusive row lock while waiting for an active target request.
    actor = require_group_manager(db, enterprise_id, group_id, actor_id, lock=False)
    # Match the authorization order used by an in-flight group task: first the
    # enterprise membership, then the group membership. A provider request
    # holds those rows until its current request returns, so revocation waits
    # for that request without creating an inverted lock cycle.
    member = _active_member_record(db, enterprise_id, user_id, lock=True)
    row = (
        db.query(EnterpriseGroupMember)
        .filter_by(enterprise_id=enterprise_id, group_id=group_id, user_id=user_id, status="active")
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not row:
        raise NotFoundError("小组成员不存在")
    if actor.role == "leader" and (row.role == "leader" or member.role in {"owner", "admin"}):
        raise ForbiddenError("小组长只能管理本组普通成员")
    _advance_billing_group_version(member)
    row.status = "removed"
    if member.default_group_id == group_id:
        member.default_group_id = None
    _event(db, enterprise_id, actor_id, "group_member_removed", subject_user_id=user_id, group_id=group_id)
    db.flush()


def set_default_group(db: Session, enterprise_id: int, actor_id: int, user_id: int,
                      group_id: int | None) -> EnterpriseMember:
    _company(db, enterprise_id, lock=True)
    require_enterprise_admin(db, enterprise_id, actor_id, lock=True)
    member = require_enterprise_member(db, enterprise_id, user_id, lock=True)
    if group_id is not None:
        require_group_member(db, enterprise_id, group_id, user_id, allow_admin=False, lock=True)
    if member.default_group_id != group_id:
        _advance_billing_group_version(member)
        member.default_group_id = group_id
    _event(db, enterprise_id, actor_id, "default_group_changed", subject_user_id=user_id, group_id=group_id)
    db.flush()
    return member
