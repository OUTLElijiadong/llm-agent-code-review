"""Real JWT contract: billing rollout failures must not masquerade as revoked chat access."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.models.enterprise_chat import EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime


@pytest.fixture
def matrix_env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    users = {
        name: User(id=user_id, username=f"billing-code-{name}", password="fixture", role="user", status=1)
        for name, user_id in (("owner_a", 101), ("member_a", 102), ("owner_b", 103))
    }
    db.add_all(users.values())
    db.commit()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = lambda: db
    tokens = {name: create_access_token(user.id, user.role) for name, user in users.items()}
    client = TestClient(app)
    try:
        yield {"db": db, "client": client, "users": users, "tokens": tokens}
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        db.close()
        engine.dispose()


def _request(env, account, method, path, **kwargs):
    return env["client"].request(method, path, headers={"Authorization": f"Bearer {env['tokens'][account]}"},
                                 **kwargs)


def test_billing_attribution_errors_are_distinct_from_chat_access_revocation(matrix_env, monkeypatch):
    db, users = matrix_env["db"], matrix_env["users"]
    owner, member, outsider = users["owner_a"], users["member_a"], users["owner_b"]
    db.add(Enterprise(id=851, name="计费状态测试", owner_id=owner.id, seat_limit=5,
                      status="active", paid_order_id="billing-status-851"))
    db.add_all([
        EnterpriseMember(enterprise_id=851, user_id=owner.id, role="owner", status="active"),
        EnterpriseMember(enterprise_id=851, user_id=member.id, role="member", status="active"),
        EnterpriseMember(enterprise_id=851, user_id=outsider.id, role="member", status="active"),
        EnterpriseGroup(id=861, enterprise_id=851, name="无默认组", status="active"),
        EnterpriseGroupMember(enterprise_id=851, group_id=861, user_id=member.id,
                              role="member", status="active"),
        EnterpriseConversation(id=871, enterprise_id=851, kind="direct", conversation_key="status-private",
                               participant_low_id=owner.id, participant_high_id=member.id, created_by=owner.id),
        EnterpriseChatMessage(id=881, enterprise_id=851, conversation_id=871, sender_id=member.id,
                              sender_type="human", client_message_id="billing-status", content="@小菱 请检查"),
    ])
    db.commit()
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args: None)
    path = "/api/enterprises/851/conversations/871/assistant-runs"

    response = _request(matrix_env, "member_a", "POST", path, json={"source_message_id": 881})
    assert response.status_code == 403 and response.json()["code"] == 40360
    outsider_response = _request(matrix_env, "owner_b", "POST", path, json={"source_message_id": 881})
    assert outsider_response.status_code == 403 and outsider_response.json()["code"] == 40300

    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    missing_default = _request(matrix_env, "member_a", "POST", path, json={"source_message_id": 881})
    assert missing_default.status_code == 403 and missing_default.json()["code"] == 40361
