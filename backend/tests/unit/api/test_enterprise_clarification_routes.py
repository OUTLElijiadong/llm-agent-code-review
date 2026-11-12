"""HTTP room boundaries and handoff contract; execution is tested in runtime tests."""
from time import time

from test_enterprise_assistant_runtime_api import _client

from app.core.dependencies import get_current_user
from app.core.exceptions import ForbiddenError
from app.core.security import create_access_token, decode_token
from app.models.enterprise_org import EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime
from app.services import enterprise_chat_service


def _base(room_id):
    return f"/enterprises/10/conversations/{room_id}/assistant-runs/ear_test/clarification"


def test_clarification_preview_room_scoped_contract(monkeypatch):
    engine, db, client, room, _ = _client()
    calls = []
    expected = {"request_id": 11, "version": 1, "call_id": "call-1", "status": "waiting",
                "action": "create_registered_enterprise_agent_team",
                "target": {}, "arguments": {"title": "设计组"},
                "candidates": [{"source_message_id": 42, "author_user_id": 1,
                                "rank": 4, "content": "@小菱 暂停创建团队", "decision": "pending"}]}
    def preview(_db, **kwargs):
        calls.append(kwargs)
        return expected
    monkeypatch.setattr(runtime, "get_clarification", preview, raising=False)
    try:
        response = client.get(_base(room.id), headers={"x-test-user": "2"})
        assert response.status_code == 200
        assert response.json()["data"] == expected
        assert calls == [{"enterprise_id": 10, "conversation_id": room.id, "run_public_id": "ear_test", "user_id": 2}]
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_clarification_group_admin_and_direct_outsider_cannot_read_or_decide(monkeypatch):
    engine, db, client, _, _ = _client()
    group = enterprise_chat_service.open_conversation(db, 10, 2, "group", group_id=100)
    direct = enterprise_chat_service.open_conversation(db, 10, 1, "direct", other_user_id=2)
    db.add(EnterpriseMember(enterprise_id=10, user_id=3, role="member", status="active"))
    db.commit()
    for user_id in (1, 3):
        user = db.get(User, user_id)
        user._authenticated_token_version = user.token_version
    def never(*_a, **_kw):
        raise AssertionError("must not enter clarification service without room access")
    monkeypatch.setattr(runtime, "get_clarification", never, raising=False)
    monkeypatch.setattr(runtime, "decide_clarification", never, raising=False)
    try:
        for room, who in ((group, 1), (direct, 3)):
            headers = {"x-test-user": str(who)}
            assert client.get(_base(room.id), headers=headers).status_code == 403
            assert client.post(f"{_base(room.id)}/11/decision", headers=headers,
                               json={"source_message_id": 42, "decision": "allow"}).status_code == 403
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_clarification_decision_and_resume_are_explicit_and_version_fenced(monkeypatch):
    engine, db, client, room, _ = _client()
    user = db.get(User, 2)
    user._authenticated_token_version = user.token_version
    user._authenticated_token_expires_at = int(time()) + 300
    decisions, resumptions = [], []
    def decide(_db, **kwargs):
        decisions.append(kwargs)
        return {"request_id": 11, "status": "waiting"}
    async def resume(_db, **kwargs):
        resumptions.append(kwargs)
        return {"run_id": "ear_test", "status": "running", "pending": None}
    monkeypatch.setattr(runtime, "decide_clarification", decide, raising=False)
    monkeypatch.setattr(runtime, "resume_clarification", resume, raising=False)
    try:
        response = client.post(f"{_base(room.id)}/11/decision", headers={"x-test-user": "2"},
                               json={"source_message_id": 42, "decision": "unrelated"})
        assert response.status_code == 200
        assert response.json()["data"] == {"request_id": 11, "status": "waiting"}
        assert decisions == [{"enterprise_id": 10, "conversation_id": room.id, "run_public_id": "ear_test",
                              "request_id": 11, "source_message_id": 42, "user_id": 2,
                              "decision": "unrelated", "expected_token_version": int(user.token_version),
                              "expected_token_expires_at": user._authenticated_token_expires_at}]
        assert client.post(f"{_base(room.id)}/11/decision", headers={"x-test-user": "2"},
                           json={"source_message_id": 42, "decision": "approve"}).status_code == 422
        assert client.post(f"{_base(room.id)}/11/decision", headers={"x-test-user": "2"},
                           json={"source_message_id": True, "decision": "allow"}).status_code == 422
        assert client.post(f"{_base(room.id)}/0/decision", headers={"x-test-user": "2"},
                           json={"source_message_id": 42, "decision": "allow"}).status_code == 422
        assert client.post(f"{_base(room.id)}/0/resume", headers={"x-test-user": "2"}).status_code == 422
        assert client.post(f"{_base(room.id)}/11/resume", headers={"x-test-user": "2"}).status_code == 200
        assert resumptions == [{"enterprise_id": 10, "conversation_id": room.id, "run_public_id": "ear_test",
                                "request_id": 11, "user_id": 2,
                                "expected_token_version": int(user.token_version),
                                "expected_token_expires_at": user._authenticated_token_expires_at}]
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_clarification_decision_requires_authenticated_token_and_propagates_denial(monkeypatch):
    engine, db, client, room, _ = _client()
    calls = []
    def denied(_db, **kwargs):
        calls.append(kwargs)
        raise ForbiddenError("同级不能代决")
    monkeypatch.setattr(runtime, "decide_clarification", denied, raising=False)
    try:
        url = f"{_base(room.id)}/11/decision"
        body = {"source_message_id": 42, "decision": "allow"}
        assert client.post(url, headers={"x-test-user": "2"}, json=body).status_code == 403
        assert calls == []
        user = db.get(User, 2)
        user._authenticated_token_version = user.token_version
        assert client.post(url, headers={"x-test-user": "2"}, json=body).status_code == 403
        assert calls == []
        user._authenticated_token_expires_at = int(time()) + 300
        assert client.post(url, headers={"x-test-user": "2"}, json=body).status_code == 403
        assert len(calls) == 1
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_clarification_route_uses_real_jwt_and_rejects_revoked_token(monkeypatch):
    engine, db, client, room, _ = _client()
    client.app.dependency_overrides.pop(get_current_user)
    calls = []

    def decide(_db, **kwargs):
        calls.append(kwargs)
        return {"request_id": 11, "status": "waiting"}

    async def resume(_db, **kwargs):
        calls.append(kwargs)
        return {"run_id": "ear_test", "status": "running", "pending": None}

    monkeypatch.setattr(runtime, "decide_clarification", decide, raising=False)
    monkeypatch.setattr(runtime, "resume_clarification", resume, raising=False)
    token = create_access_token(2, "user", token_version=0)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"source_message_id": 42, "decision": "deny"}
    url = f"{_base(room.id)}/11/decision"
    try:
        assert client.post(url, headers=headers, json=payload).status_code == 200
        assert calls[0]["expected_token_version"] == 0
        assert calls[0]["expected_token_expires_at"] == decode_token(token)["exp"]
        assert client.post(f"{_base(room.id)}/11/resume", headers=headers).status_code == 200
        assert calls[1]["expected_token_expires_at"] == decode_token(token)["exp"]
        user = db.get(User, 2)
        user.token_version += 1
        db.commit()
        assert client.post(url, headers=headers, json=payload).status_code == 401
        assert len(calls) == 2
    finally:
        client.close()
        db.close()
        engine.dispose()
