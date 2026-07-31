"""内测码与注册原子事务测试。"""

from datetime import timedelta

import pytest

from app.core.config import settings
from app.core.exceptions import ConflictError, ValidationError
from app.models.beta_invite_code import BetaInviteCode
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.schemas.auth import RegisterIn
from app.services import auth_service, beta_invite_service


@pytest.fixture(autouse=True)
def beta_settings(monkeypatch):
    monkeypatch.setattr(settings, "beta_registration_enabled", True)
    monkeypatch.setattr(settings, "beta_code_pepper", "test-beta-code-pepper-32-characters-minimum")


@pytest.fixture
def default_role(db):
    role = Role(name="普通成员", code="user", status="active", sort=100, is_builtin=1)
    db.add(role)
    db.commit()
    return role


def _generate_one(db, admin_user, *, days=7) -> tuple[str, BetaInviteCode]:
    codes, rows = beta_invite_service.generate_codes(
        db,
        creator_id=admin_user.id,
        count=1,
        expiry_days=days,
        label="测试批次",
    )
    return codes[0], rows[0]


def _payload(username: str, code: str) -> RegisterIn:
    return RegisterIn(username=username, password="secret12", beta_code=code)


def test_generated_code_is_hmac_only_and_has_fixed_format(db, admin_user):
    plain, row = _generate_one(db, admin_user)

    assert plain.startswith("PRISM-")
    assert len(plain) == 29
    assert row.code_hash == beta_invite_service.digest_code(plain)
    assert plain not in row.code_hash
    assert plain not in row.display_prefix
    assert len(row.display_prefix) == 29
    assert BetaInviteCode.__table__.c.display_prefix.type.length >= len(row.display_prefix)
    assert row.display_prefix.endswith("-*****-*****-*****")


def test_registration_atomically_consumes_code_and_assigns_default_role(db, admin_user, default_role):
    plain, invite = _generate_one(db, admin_user)

    user = auth_service.register(db, _payload("beta-user", plain))
    db.refresh(invite)

    assert invite.status == "used"
    assert invite.used_by == user.id
    assert invite.used_at is not None
    binding = db.query(UserRole).filter(UserRole.user_id == user.id).one()
    assert binding.role_id == default_role.id


def test_username_conflict_rolls_back_without_consuming_code(db, admin_user, default_role):
    existing = User(username="taken-name", password="x", role="user", status=1)
    db.add(existing)
    db.commit()
    plain, invite = _generate_one(db, admin_user)

    with pytest.raises(ConflictError, match="用户名已存在"):
        auth_service.register(db, _payload("taken-name", plain))

    db.refresh(invite)
    assert invite.status == "active"
    assert invite.used_by is None


def test_code_cannot_be_reused(db, admin_user, default_role):
    plain, _ = _generate_one(db, admin_user)
    auth_service.register(db, _payload("first-user", plain))

    with pytest.raises(ValidationError, match="内测码无效、已使用或已过期"):
        auth_service.register(db, _payload("second-user", plain))

    assert db.query(User).filter(User.username == "second-user").count() == 0


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_revoked_and_expired_codes_share_nondisclosing_error(db, admin_user, default_role, state):
    plain, invite = _generate_one(db, admin_user)
    if state == "revoked":
        beta_invite_service.revoke_code(db, invite.id)
    else:
        invite.expires_at = beta_invite_service._utcnow_naive() - timedelta(seconds=1)
        db.commit()

    with pytest.raises(ValidationError) as exc_info:
        auth_service.register(db, _payload(f"{state}-user", plain))

    assert exc_info.value.message == "内测码无效、已使用或已过期"


def test_disabled_beta_registration_preserves_normal_registration(db, default_role, monkeypatch):
    monkeypatch.setattr(settings, "beta_registration_enabled", False)

    user = auth_service.register(db, RegisterIn(username="open-user", password="secret12"))

    assert user.username == "open-user"
    assert db.query(UserRole).filter(UserRole.user_id == user.id).count() == 1


def test_revoke_rejects_used_code(db, admin_user, default_role):
    plain, invite = _generate_one(db, admin_user)
    auth_service.register(db, _payload("used-user", plain))

    with pytest.raises(ConflictError, match="仅可撤销"):
        beta_invite_service.revoke_code(db, invite.id)
