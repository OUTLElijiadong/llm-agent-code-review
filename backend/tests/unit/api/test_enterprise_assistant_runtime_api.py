"""企业小菱运行 API 契约与路由层当前会话授权。"""

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.enterprise_chat import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingRefund,
    BillingSubscription,
    BillingSubscriptionPeriod,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantApproval, EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseAuditEvent,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseMember,
)
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime
from app.services import enterprise_chat_service


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Enterprise.__table__, EnterpriseMember.__table__,
        EnterpriseGroup.__table__, EnterpriseGroupMember.__table__,
        EnterpriseAuditEvent.__table__,
        EnterpriseConversation.__table__, EnterpriseChatMessage.__table__,
        EnterpriseAssistantRun.__table__, EnterpriseAssistantRuntimeState.__table__,
        EnterpriseAssistantApproval.__table__,
        BillingOrder.__table__, BillingPaymentEvent.__table__,
        BillingRefund.__table__, BillingSubscription.__table__, BillingSubscriptionPeriod.__table__,
    ])
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, username="owner", password="x", role="user", status=1),
        User(id=2, username="employee", password="x", role="user", status=1),
        User(id=3, username="outsider", password="x", role="user", status=1),
    ])
    db.flush()
    db.add_all([
        Enterprise(id=10, name="甲公司", owner_id=1, seat_limit=5, status="active", paid_order_id="p1"),
        EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
        EnterpriseGroup(id=100, enterprise_id=10, name="研发组", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=100, user_id=2, role="member", status="active"),
    ])
    db.commit()
    room = enterprise_chat_service.open_conversation(db, 10, 1, "company")
    message = enterprise_chat_service.send_message(db, 10, 2, room.id, "assistant-result", "小菱结果")
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppError)
    async def handle_app_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.dependency_overrides[get_db] = lambda: db
    def current_user(x_test_user: int = Header()):
        return db.get(User, x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    return engine, db, TestClient(app), room, message


def test_run_status_response_contract_and_runtime_authorization(monkeypatch):
    engine, db, client, room, message = _client()
    calls = []

    def status(_db, **kwargs):
        calls.append(kwargs)
        return {
            "run_id": "ear_test", "status": "waiting_input", "message": message,
            "pending": {"kind": "input", "call_id": "call-1", "operation": "ask_user", "approval_id": None,
                        "question": "需要补充哪一项？"},
            "error": None, "recoverable": False, "initiator_user_id": 2,
        }

    monkeypatch.setattr(runtime, "get_run_status", status)
    url = f"/enterprises/10/conversations/{room.id}/assistant-runs/ear_test"
    response = client.get(url, headers={"x-test-user": "2"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["assistant_mode"] == "enterprise_agent_runtime"
    assert data["run_id"] == "ear_test"
    assert data["status"] == "waiting_input"
    assert data["pending"] == {
        "kind": "input", "call_id": "call-1", "operation": "ask_user", "approval_id": None,
        "question": "需要补充哪一项？",
    }
    assert data["message"]["content"] == "小菱结果"
    assert calls[0]["user_id"] == 2 and calls[0]["conversation_id"] == room.id
    client.close()
    db.close()
    engine.dispose()


def test_active_run_list_returns_call_id_for_refresh_and_scopes_to_room(monkeypatch):
    engine, db, client, room, message = _client()
    monkeypatch.setattr(runtime, "list_active_runs", lambda _db, **_: [{
        "run_id": "ear_test", "status": "waiting_input", "message": message,
        "pending": {"kind": "input", "call_id": "call-2", "operation": "ask_user",
                    "approval_id": None, "question": "补充信息"},
        "error": None, "recoverable": False, "initiator_user_id": 2, "source_message_id": 1,
    }])
    response = client.get(
        f"/enterprises/10/conversations/{room.id}/assistant-runs", headers={"x-test-user": "2"},
    )
    assert response.status_code == 200
    item = response.json()["data"]["items"][0]
    assert item["pending"]["call_id"] == "call-2"
    assert item["pending"]["kind"] == "input"
    assert item["assistant_mode"] == "enterprise_agent_runtime"
    client.close()
    db.close()
    engine.dispose()


def test_start_route_returns_queued_contract_and_schedules_background_runner(monkeypatch):
    engine, db, client, room, message = _client()
    scheduled = []

    async def enqueue(_db, **kwargs):
        return {"run_id": "ear_queued", "status": "queued", "message": message, "pending": None,
                "recoverable": True, "initiator_user_id": 2}

    async def background(received_engine, **kwargs):
        scheduled.append((received_engine, kwargs))

    monkeypatch.setattr(runtime, "run_for_message", enqueue)
    monkeypatch.setattr(runtime, "execute_queued_run", background)
    response = client.post(
        f"/enterprises/10/conversations/{room.id}/assistant-runs",
        headers={"x-test-user": "2"}, json={"source_message_id": message.id},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["run_id"], data["status"], data["assistant_mode"]) == (
        "ear_queued", "queued", "enterprise_agent_runtime",
    )
    assert len(scheduled) == 1
    received_engine, kwargs = scheduled[0]
    assert received_engine is engine
    assert kwargs == {"enterprise_id": 10, "conversation_id": room.id, "run_public_id": "ear_queued"}
    client.close()
    db.close()
    engine.dispose()


def test_approval_routes_require_enterprise_admin_and_return_decision_contract(monkeypatch):
    engine, db, client, room, message = _client()

    async def decision(_db, **_kwargs):
        return {"run_id": "ear_test", "status": "completed", "message": message, "pending": None}

    monkeypatch.setattr(runtime, "list_pending_approvals", lambda _db, **_: [{
        "id": 7, "run_id": "ear_test", "conversation_id": room.id, "group_id": None,
        "initiator_user_id": 2, "required_role": "enterprise_admin", "tool": "propose_enterprise_announcement",
        "title": "维护公告", "content": "今晚维护", "status": "pending", "create_time": None,
    }])
    monkeypatch.setattr(runtime, "decide_approval", decision)

    denied = client.get("/enterprises/10/conversations/assistant-approvals", headers={"x-test-user": "2"})
    assert denied.status_code == 403
    allowed = client.get("/enterprises/10/conversations/assistant-approvals", headers={"x-test-user": "1"})
    assert allowed.status_code == 200
    approval = allowed.json()["data"]["items"][0]
    assert approval["required_role"] == "enterprise_admin"
    assert approval["title"] == "维护公告"

    decision = client.post(
        "/enterprises/10/conversations/assistant-approvals/7/decision",
        headers={"x-test-user": "1"}, json={"decision": "approve", "reason": "已核对"},
    )
    assert decision.status_code == 200
    data = decision.json()["data"]
    assert (data["approval_id"], data["decision"], data["run_id"], data["status"]) == (
        7, "approve", "ear_test", "completed",
    )
    assert data["message"]["content"] == "小菱结果"
    client.close()
    db.close()
    engine.dispose()


def test_company_admin_can_stop_group_run_without_reading_or_retrying_group_content():
    engine, db, client, _company_room, _company_message = _client()
    group_room = enterprise_chat_service.open_conversation(db, 10, 2, "group", group_id=100)
    source = enterprise_chat_service.send_message(db, 10, 2, group_room.id, "source-group", "@小菱 执行任务")
    run = EnterpriseAssistantRun(
        public_id="ear_group_admin", request_key="enterprise-assistant:ear_group_admin",
        enterprise_id=10, conversation_id=group_room.id, group_id=100, initiator_user_id=2,
        source_message_id=source.id, status="queued", prompt_sha256="0" * 64,
    )
    db.add(run)
    db.flush()
    db.add(EnterpriseAssistantRuntimeState(
        assistant_run_id=run.id, enterprise_id=10, conversation_id=group_room.id, group_id=100,
        initiator_user_id=2, source_message_id=source.id, status="queued",
        checkpoint_json='{"run_id":"ear_group_admin","model":"test","transcript":[],"tools":[],"status":"queued"}',
        permission_snapshot_json="{}", permission_snapshot_sha256="0" * 64,
        lease_generation=0, billing_invocation_count=0, version=1,
    ))
    db.commit()
    base = f"/enterprises/10/conversations/{group_room.id}"

    assert client.get(f"{base}/messages", headers={"x-test-user": "1"}).status_code == 403
    listed = client.get(f"{base}/assistant-runs", headers={"x-test-user": "1"})
    assert listed.status_code == 200
    item = listed.json()["data"]["items"][0]
    assert item["run_id"] == run.public_id and item["status"] == "queued"
    assert not {"message", "pending", "source_message_id", "initiator_user_id"}.intersection(item)

    status = client.get(f"{base}/assistant-runs/{run.public_id}", headers={"x-test-user": "1"})
    assert status.status_code == 200
    assert status.json()["data"]["status"] == "queued"
    assert "message" not in status.json()["data"]

    retry = client.post(f"{base}/assistant-runs/{run.public_id}/retry", headers={"x-test-user": "1"})
    assert retry.status_code == 403
    cancelled = client.post(
        f"{base}/assistant-runs/{run.public_id}/cancel", headers={"x-test-user": "1"},
        json={"reason": "停止小组中的异常任务"},
    )
    assert cancelled.status_code == 200
    payload = cancelled.json()["data"]
    assert payload["status"] == "cancelled" and payload["can_cancel"] is False
    assert "message" not in payload
    assert db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one().status == "cancelled"
    audit = db.query(EnterpriseAuditEvent).filter_by(
        enterprise_id=10, actor_id=1, action="assistant_run_cancelled", subject_user_id=2, group_id=100,
    ).one()
    assert audit.detail == {"run_id": run.public_id, "reason": "停止小组中的异常任务"}

    outsider = client.get(f"{base}/assistant-runs/{run.public_id}", headers={"x-test-user": "3"})
    assert outsider.status_code == 403
    client.close()
    db.close()
    engine.dispose()


def test_billing_context_previews_current_user_group_and_closed_gate_without_authorizing_execution():
    engine, db, client, room, _ = _client()
    member = db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.default_group_id = 100
    actor = db.get(User, 2)
    actor._authenticated_token_version = int(actor.token_version)
    db.commit()
    url = f"/enterprises/10/conversations/{room.id}/assistant-billing-context"
    response = client.get(url, headers={"x-test-user": "2"})
    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "group", "group_id": 100, "group_name": "研发组", "enabled": False,
    }
    member.default_group_id = None
    db.commit()
    response = client.get(url, headers={"x-test-user": "2"})
    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "default_group_required", "group_id": None, "group_name": None, "enabled": False,
    }
    db.query(EnterpriseGroupMember).filter_by(enterprise_id=10, user_id=2, group_id=100).one().status = "removed"
    db.commit()
    response = client.get(url, headers={"x-test-user": "2"})
    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "unassigned", "group_id": None, "group_name": None, "enabled": False,
    }
    client.close()
    db.close()
    engine.dispose()


def test_billing_context_enforces_direct_membership_and_current_account_token():
    engine, db, client, _, _ = _client()
    db.add(EnterpriseMember(enterprise_id=10, user_id=3, role="member", status="active"))
    db.commit()
    direct = enterprise_chat_service.open_conversation(db, 10, 1, "direct", other_user_id=2)
    url = f"/enterprises/10/conversations/{direct.id}/assistant-billing-context"
    actor = db.get(User, 2)
    outsider = db.get(User, 3)
    actor._authenticated_token_version = int(actor.token_version)
    outsider._authenticated_token_version = int(outsider.token_version)
    assert client.get(url, headers={"x-test-user": "3"}).status_code == 403
    actor._authenticated_token_version += 1
    assert client.get(url, headers={"x-test-user": "2"}).status_code == 403
    actor._authenticated_token_version -= 1
    db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().status = "removed"
    db.commit()
    assert client.get(url, headers={"x-test-user": "2"}).status_code == 403
    assert client.get(f"/enterprises/11/conversations/{direct.id}/assistant-billing-context",
                      headers={"x-test-user": "2"}).status_code in {403, 404}
    client.close()
    db.close()
    engine.dispose()
