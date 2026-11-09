"""默认计费归属版本与现有企业组织变更处于同一事务。"""

import pytest

from app.core.exceptions import ConflictError, ForbiddenError
from app.models.enterprise_org import EnterpriseMember
from app.models.user import User
from app.services import enterprise_org_service as org


def setup_members(db):
    owner = User(username="billing_group_owner", password="x", role="user", status=1)
    user = User(username="billing_group_member", password="x", role="user", status=1)
    db.add_all([owner, user])
    db.flush()
    company = org.create_enterprise_from_paid_order(
        db, owner_id=owner.id, name="归属版本测试", seat_limit=3, paid_order_id="billing-group-version"
    )
    org.accept_invitation(db, org.create_invitation(db, company.id, owner.id), user.id)
    member = db.query(EnterpriseMember).filter_by(enterprise_id=company.id, user_id=user.id).one()
    first = org.create_group(db, company.id, owner.id, "甲组")
    second = org.create_group(db, company.id, owner.id, "乙组")
    return company, owner, user, member, first, second


def test_membership_changes_and_default_aba_increment_once(db):
    company, owner, user, member, first, second = setup_members(db)
    assert member.billing_group_version == 0
    org.set_group_member(db, company.id, first.id, owner.id, user.id, "member")
    assert member.billing_group_version == 1
    org.set_group_member(db, company.id, first.id, owner.id, user.id, "leader")
    assert member.billing_group_version == 1  # 仅角色变化，不变更计费归属
    org.set_group_member(db, company.id, second.id, owner.id, user.id, "member")
    assert member.billing_group_version == 2
    org.set_default_group(db, company.id, owner.id, user.id, first.id)
    assert member.billing_group_version == 3
    org.set_default_group(db, company.id, owner.id, user.id, first.id)
    assert member.billing_group_version == 3
    org.set_default_group(db, company.id, owner.id, user.id, second.id)
    org.set_default_group(db, company.id, owner.id, user.id, first.id)
    assert member.billing_group_version == 5
    org.remove_group_member(db, company.id, first.id, owner.id, user.id)
    assert member.default_group_id is None
    assert member.billing_group_version == 6  # 移组和默认组清除为同一操作
    org.set_group_member(db, company.id, first.id, owner.id, user.id, "member")
    assert member.billing_group_version == 7


def test_leave_and_rejoin_company_invalidates_even_no_group(db):
    company, owner, user, member, _, _ = setup_members(db)
    assert member.billing_group_version == 0
    org.remove_enterprise_member(db, company.id, owner.id, user.id)
    assert member.billing_group_version == 1
    token = org.create_invitation(db, company.id, owner.id)
    reinstated = org.accept_invitation(db, token, user.id)
    assert reinstated.id == member.id
    assert reinstated.billing_group_version == 2
    assert reinstated.default_group_id is None


def test_invalid_assignment_never_changes_version(db):
    company, owner, user, member, first, _ = setup_members(db)
    with pytest.raises(ForbiddenError):
        org.set_default_group(db, company.id, owner.id, user.id, first.id)
    assert member.billing_group_version == 0


def test_rolled_back_group_change_restores_version(db):
    company, owner, user, member, first, _ = setup_members(db)
    db.commit()
    org.set_group_member(db, company.id, first.id, owner.id, user.id, "member")
    assert member.billing_group_version == 1
    db.rollback()
    restored = db.get(EnterpriseMember, member.id)
    assert restored.billing_group_version == 0
    assert restored.default_group_id is None


@pytest.mark.parametrize("remove_company", [True, False])
def test_exhausted_version_does_not_partially_change_membership(db, remove_company):
    company, owner, user, member, first, _ = setup_members(db)
    org.set_group_member(db, company.id, first.id, owner.id, user.id, "member")
    org.set_default_group(db, company.id, owner.id, user.id, first.id)
    member.billing_group_version = 2**63 - 1
    db.flush()
    with pytest.raises(ConflictError):
        if remove_company:
            org.remove_enterprise_member(db, company.id, owner.id, user.id)
        else:
            org.remove_group_member(db, company.id, first.id, owner.id, user.id)
    assert member.status == "active"
    assert member.default_group_id == first.id
    row = db.query(org.EnterpriseGroupMember).filter_by(
        enterprise_id=company.id, group_id=first.id, user_id=user.id
    ).one()
    assert row.status == "active"
