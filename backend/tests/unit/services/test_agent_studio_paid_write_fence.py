"""Each Studio mutation must check its precise live permission before touching assets."""

import pytest
from studio_rbac_test_support import grant_studio_actor

from app.core.exceptions import ForbiddenError
from app.core.permission_codes import PermissionCode
from app.models.user import User
from app.services import agent_studio_service as studio
from app.services import rbac_service


@pytest.mark.parametrize(("operation", "permission", "args", "kwargs"), [
    (studio.create_agent, PermissionCode.AGENT_ASSET_CREATE, (), {
        "code": "test", "name": "test", "description": "", "prompt": "test", "review_focus": "test",
        "model_config": {},
    }),
    (studio.revise_agent, PermissionCode.AGENT_ASSET_UPDATE_OWN, (1,), {
        "prompt": "test", "review_focus": "test", "model_config": {},
    }),
    (studio.bind_skill, PermissionCode.AGENT_ASSET_UPDATE_OWN, (1,), {
        "skill_version_id": 2, "position": 0, "config": {},
    }),
    (studio.unbind_skill, PermissionCode.AGENT_ASSET_UPDATE_OWN, (1,), {}),
    (studio.test_agent_version, PermissionCode.AGENT_ASSET_TEST, (1,), {}),
    (studio.submit_agent_version, PermissionCode.AGENT_ASSET_SUBMIT, (1,), {}),
    (studio.withdraw_agent_version, PermissionCode.AGENT_ASSET_SUBMIT, (1,), {}),
    (studio.create_skill, PermissionCode.SKILL_ASSET_CREATE, (), {
        "code": "test", "name": "test", "description": "", "skill_type": "llm_transform",
        "definition": {"prompt": "test"}, "requested_capabilities": [],
    }),
    (studio.revise_skill, PermissionCode.SKILL_ASSET_UPDATE_OWN, (1,), {
        "skill_type": "llm_transform", "definition": {"prompt": "test"}, "requested_capabilities": [],
    }),
])
def test_mutation_requires_exact_permission_before_reading_assets(db, monkeypatch, operation, permission, args, kwargs):
    actor = User(username="studio_fence_actor", password="x", role="reviewer", status=1)
    db.add(actor)
    db.commit()
    calls = []

    actor._authenticated_token_version = 7

    def deny(session, user_id, code, *, expected_token_version=None):
        assert session is db
        calls.append((user_id, code, expected_token_version))
        return False

    monkeypatch.setattr(rbac_service, "check_paid_permission_fenced", deny)
    with pytest.raises(ForbiddenError):
        operation(db, actor, *args, **kwargs)
    assert calls == [(actor.id, permission, 7)]


def test_misgranted_user_still_cannot_create_studio_asset(db):
    actor = User(username="studio_misgranted_user", password="x", role="user", status=1)
    db.add(actor)
    db.flush()
    grant_studio_actor(db, actor)
    db.commit()
    with pytest.raises(ForbiddenError):
        studio.create_agent(
            db, actor, code="studio_misgrant", name="test", description="", prompt="test",
            review_focus="test", model_config={},
        )
