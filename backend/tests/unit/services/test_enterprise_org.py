from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import ConflictError, ForbiddenError
from app.models.enterprise_org import Enterprise, EnterpriseGroupMember, EnterpriseInvitation, EnterpriseMember
from app.models.user import User
from app.services import enterprise_org_service as service


def _user(db, name):
    user = User(username=name, password="x", role="user", status=1)
    db.add(user)
    db.flush()
    return user


def _enterprise(db, owner, *, seats=3, order="paid-1"):
    return service.create_enterprise_from_paid_order(
        db, owner_id=owner.id, name="公司", seat_limit=seats, paid_order_id=order
    )


def test_creation_is_idempotent_and_never_changes_platform_role(db):
    owner = _user(db, "owner")
    company = _enterprise(db, owner)
    assert _enterprise(db, owner).id == company.id
    assert owner.role == "user"
    assert service.require_enterprise_member(db, company.id, owner.id).role == "owner"
    assert db.query(Enterprise).count() == 1


def test_enterprise_isolation_does_not_follow_platform_admin(db):
    owner, outsider = _user(db, "owner"), _user(db, "outside")
    outsider.role = "admin"
    company = _enterprise(db, owner)
    with pytest.raises(ForbiddenError):
        service.require_enterprise_member(db, company.id, outsider.id)
    with pytest.raises(ForbiddenError):
        service.require_enterprise_admin(db, company.id, outsider.id)


def test_invitations_single_use_expiry_and_seat_limit(db):
    owner, first, second = _user(db, "owner"), _user(db, "first"), _user(db, "second")
    company = _enterprise(db, owner, seats=2)
    token = service.create_invitation(db, company.id, owner.id)
    assert token not in db.query(EnterpriseInvitation).one().token_hash
    invite = db.query(EnterpriseInvitation).one()
    invite.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    with pytest.raises(ConflictError):
        service.accept_invitation(db, token, first.id)
    token = service.create_invitation(db, company.id, owner.id)
    assert service.accept_invitation(db, token, first.id).role == "member"
    with pytest.raises(ConflictError):
        service.accept_invitation(db, token, second.id)
    with pytest.raises(ConflictError, match="席位不足"):
        service.create_invitation(db, company.id, owner.id)
    assert db.query(EnterpriseMember).filter_by(enterprise_id=company.id, status="active").count() == 2


def test_group_leader_is_scoped_and_owner_cannot_be_removed(db):
    owner, lead, other = _user(db, "owner"), _user(db, "lead"), _user(db, "other")
    company = _enterprise(db, owner)
    for user in (lead, other):
        service.accept_invitation(db, service.create_invitation(db, company.id, owner.id), user.id)
    group_a = service.create_group(db, company.id, owner.id, "甲组")
    group_b = service.create_group(db, company.id, owner.id, "乙组")
    service.set_group_member(db, company.id, group_a.id, owner.id, lead.id, "leader")
    with pytest.raises(ForbiddenError):
        service.set_group_member(db, company.id, group_a.id, lead.id, other.id, "leader")
    service.set_group_member(db, company.id, group_a.id, lead.id, other.id, "member")
    service.set_group_member(db, company.id, group_a.id, owner.id, other.id, "leader")
    with pytest.raises(ForbiddenError):
        service.remove_group_member(db, company.id, group_a.id, lead.id, other.id)
    with pytest.raises(ForbiddenError):
        service.set_group_member(db, company.id, group_a.id, lead.id, other.id, "member")
    with pytest.raises(ForbiddenError):
        service.set_group_member(db, company.id, group_a.id, lead.id, owner.id, "member")
    assert service.require_group_manager(db, company.id, group_a.id, other.id).role == "leader"
    with pytest.raises(ForbiddenError):
        service.set_group_member(db, company.id, group_b.id, lead.id, other.id, "leader")
    with pytest.raises(ForbiddenError):
        service.set_enterprise_role(db, company.id, lead.id, other.id, "admin")
    with pytest.raises(ConflictError):
        service.remove_enterprise_member(db, company.id, owner.id, owner.id)


def test_member_removal_revokes_group_access_and_seat_reusable(db):
    owner, member, replacement = _user(db, "owner"), _user(db, "member"), _user(db, "replacement")
    company = _enterprise(db, owner, seats=2)
    service.accept_invitation(db, service.create_invitation(db, company.id, owner.id), member.id)
    group = service.create_group(db, company.id, owner.id, "研发")
    service.set_group_member(db, company.id, group.id, owner.id, member.id, "member")
    service.remove_enterprise_member(db, company.id, owner.id, member.id)
    with pytest.raises(ForbiddenError):
        service.require_group_member(db, company.id, group.id, member.id)
    assert db.query(EnterpriseGroupMember).filter_by(group_id=group.id, user_id=member.id).one().status == "removed"
    service.accept_invitation(db, service.create_invitation(db, company.id, owner.id), replacement.id)
    assert db.query(EnterpriseMember).filter_by(enterprise_id=company.id, status="active").count() == 2


def test_disabled_account_can_be_removed_and_releases_seat(db):
    owner, member = _user(db, "owner"), _user(db, "member")
    company = _enterprise(db, owner, seats=2)
    service.accept_invitation(db, service.create_invitation(db, company.id, owner.id), member.id)
    member.status = 0
    db.flush()
    service.remove_enterprise_member(db, company.id, owner.id, member.id)
    assert db.query(EnterpriseMember).filter_by(enterprise_id=company.id, status="active").count() == 1


def test_suspended_company_blocks_join_and_replacement_but_retains_existing_access(db):
    owner, member = _user(db, "owner"), _user(db, "member")
    company = _enterprise(db, owner)
    token = service.create_invitation(db, company.id, owner.id)
    company.status = "suspended"
    db.flush()
    assert service.require_enterprise_member(db, company.id, owner.id)
    with pytest.raises(ConflictError):
        service.accept_invitation(db, token, member.id)
    with pytest.raises(ConflictError):
        service.create_invitation(db, company.id, owner.id)


def test_transfer_owner_is_atomic_and_old_owner_is_admin(db):
    owner, successor = _user(db, "owner"), _user(db, "successor")
    company = _enterprise(db, owner)
    service.accept_invitation(db, service.create_invitation(db, company.id, owner.id), successor.id)
    service.transfer_enterprise_owner(db, company.id, owner.id, successor.id)
    assert company.owner_id == successor.id
    assert service.require_enterprise_member(db, company.id, owner.id).role == "admin"
    assert service.require_enterprise_admin(db, company.id, successor.id, owner_only=True)
    with pytest.raises(ForbiddenError):
        service.transfer_enterprise_owner(db, company.id, owner.id, successor.id)


def test_invite_revocation_invalidates_token_without_exposing_token_hash(db):
    owner, member = _user(db, "owner"), _user(db, "member")
    company = _enterprise(db, owner)
    token = service.create_invitation(db, company.id, owner.id)
    invite = db.query(EnterpriseInvitation).one()
    service.revoke_invitation(db, company.id, owner.id, invite.id)
    with pytest.raises(ConflictError):
        service.accept_invitation(db, token, member.id)
