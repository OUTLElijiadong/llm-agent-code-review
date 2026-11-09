"""Freeze and revalidate billing attribution without changing chat authorization."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
from app.models.user import User
from app.services import enterprise_chat_service, enterprise_org_service


class BillingAttributionUnavailable(ForbiddenError):
    """The attribution rollout is closed; this is not loss of chat access."""

    code = 40360
    next_action = "请稍后重试或联系企业管理员"


class DefaultBillingGroupRequired(ForbiddenError):
    """The member belongs to groups but has no currently valid default."""

    code = 40361


@dataclass(frozen=True)
class BillingGroupSnapshot:
    group_id: int | None
    membership_version: int | None
    user_token_version: int | None


def frozen(run) -> int | None:
    version = getattr(run, "billing_attribution_version", 0)
    if version == 0:
        return int(run.group_id) if run.group_id is not None else None
    if version != 1:
        raise ForbiddenError("企业小菱计费归属版本无效")
    value = run.billing_group_id
    if value is not None and (type(value) is not int or value <= 0):
        raise ForbiddenError("企业小菱计费小组无效")
    return value


def capture(db: Session, *, enterprise_id: int, user_id: int, conversation_id: int,
            lock_company: bool = True, expected_token_version: int | None = None) -> BillingGroupSnapshot:
    """Take current locked attribution; caller must retain the transaction through use."""
    company = enterprise_org_service._company(db, enterprise_id, lock=lock_company)
    if company.status != "active":
        raise ForbiddenError("企业当前不可执行小菱任务")
    member = enterprise_org_service.require_enterprise_member(db, enterprise_id, user_id, lock=True)
    room = enterprise_chat_service.get_conversation(
        db, enterprise_id, user_id, conversation_id, lock=True, shared_lock=True,
    )
    if room.kind == "group":
        if room.group_id is None:
            raise ForbiddenError("当前小组会话范围无效")
        enterprise_org_service.require_group_member(
            db, enterprise_id, int(room.group_id), user_id, allow_admin=False, lock=True, shared_group_lock=True,
        )
        return BillingGroupSnapshot(group_id=int(room.group_id), membership_version=None, user_token_version=None)
    if room.kind not in {"company", "direct"}:
        raise ForbiddenError("当前会话不能确定计费归属")
    version = member.billing_group_version
    if type(version) is not int or version < 0:
        raise ForbiddenError("成员计费归属版本无效")
    account = db.query(User).filter_by(id=user_id, status=1).populate_existing().with_for_update().first()
    if account is None or type(account.token_version) is not int or account.token_version < 0:
        raise ForbiddenError("账号状态无效，无法确定计费归属")
    if expected_token_version is not None and account.token_version != expected_token_version:
        raise ForbiddenError("登录状态已变化，请重新登录")
    memberships = (db.query(EnterpriseGroupMember)
                   .filter_by(enterprise_id=enterprise_id, user_id=user_id, status="active")
                   .order_by(EnterpriseGroupMember.group_id.asc()).populate_existing().with_for_update().all())
    default_group_id = member.default_group_id
    if default_group_id is None:
        if memberships:
            raise DefaultBillingGroupRequired("请联系企业管理员设置有效默认小组后再使用小菱")
        return BillingGroupSnapshot(group_id=None, membership_version=version, user_token_version=account.token_version)
    if not any(int(row.group_id) == int(default_group_id) for row in memberships):
        raise DefaultBillingGroupRequired("默认小组无效，请联系企业管理员重新设置")
    group = (db.query(EnterpriseGroup).filter_by(
        id=default_group_id, enterprise_id=enterprise_id, status="active",
    ).populate_existing().with_for_update(read=True).first())
    if group is None:
        raise DefaultBillingGroupRequired("默认小组无效，请联系企业管理员重新设置")
    return BillingGroupSnapshot(group_id=int(default_group_id), membership_version=version,
                                user_token_version=account.token_version)


def validate(db: Session, run, *, lock_company: bool = True) -> int | None:
    """Return the frozen group only after checking new-format attribution; legacy is unchanged."""
    version = getattr(run, "billing_attribution_version", 0)
    frozen_group_id = frozen(run)
    if version == 0:
        return frozen_group_id
    # A queued or resumed run can outlive a feature-gate rollback. The gate
    # protects each new action, not only initial creation; historical frozen
    # attribution remains readable for settling already-sent requests.
    from app.services import enterprise_assistant_runtime_service as runtime

    if not runtime._CROSS_SCOPE_ATTRIBUTION_VERIFIED:
        raise BillingAttributionUnavailable("企业会话计费归属正在验证，暂不能继续执行小菱任务")
    live = capture(db, enterprise_id=int(run.enterprise_id), user_id=int(run.initiator_user_id),
                   conversation_id=int(run.conversation_id), lock_company=lock_company)
    room = enterprise_chat_service.get_conversation(
        db, int(run.enterprise_id), int(run.initiator_user_id), int(run.conversation_id),
    )
    expected_scope = int(room.group_id) if room.kind == "group" and room.group_id is not None else None
    if run.group_id != expected_scope:
        raise ForbiddenError("企业小菱会话权限范围已变化")
    if (live.group_id != frozen_group_id or live.membership_version != run.billing_membership_version
            or live.user_token_version != run.billing_user_token_version):
        raise ForbiddenError("企业小菱计费归属已变化，请重新发起任务")
    if expected_scope is not None and expected_scope != live.group_id:
        raise ForbiddenError("企业小菱会话与计费归属不匹配")
    return live.group_id
