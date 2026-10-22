"""真实持久账本与 ASGI 鉴权，观察、认领及失联都不伪造业务成功。"""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import agent_browser as router_module
from app.api.v1 import agent_responses
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.exceptions import ConflictError, ForbiddenError, ValidationError
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models.agent_browser_action import AgentBrowserAction
from app.models.agent_response_run import AgentResponseRun
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_browser import BrowserAckIn
from app.services import agent_browser_service as service
from app.services.agent_supervisor_service import review_response_tool


@pytest.mark.parametrize("route", ["/admin/embedding", "/admin/mcp-workers", "/admin/llm",
    "/admin/platform?section=embedding", "/admin/platform?section=mcp-workers", "/admin/platform?section=llm"])
def test_super_admin_routes_reject_regular_admin(db, owner, monkeypatch, route):
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: True)
    monkeypatch.setattr(service.rbac_service, "is_super_admin_user", lambda *_args: False)

    with pytest.raises(ForbiddenError):
        service.authorize_route(db, owner, route)


@pytest.mark.parametrize("route", ["/admin/embedding", "/admin/mcp-workers", "/admin/llm",
    "/admin/platform?section=embedding", "/admin/platform?section=mcp-workers", "/admin/platform?section=llm"])
def test_super_admin_routes_allow_verified_super_admin(db, owner, monkeypatch, route):
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: True)
    monkeypatch.setattr(service.rbac_service, "is_super_admin_user", lambda *_args: True)

    assert service.authorize_route(db, owner, route) == route


def test_audit_route_requires_reviewer_and_audit_permission(db, owner, monkeypatch):
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: False)
    monkeypatch.setattr(service.rbac_service, "has_effective_role", lambda *_args: False)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: True)

    with pytest.raises(ForbiddenError):
        service.authorize_route(db, owner, "/audit")

    monkeypatch.setattr(service.rbac_service, "has_effective_role", lambda *_args: True)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: False)

    with pytest.raises(ForbiddenError):
        service.authorize_route(db, owner, "/audit")

    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: True)
    assert service.authorize_route(db, owner, "/audit") == "/audit"


def test_legacy_report_template_route_normalizes_to_admin_path(db, owner, monkeypatch):
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: True)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: True)

    assert service.authorize_route(db, owner, "/report/templates") == "/admin/report-templates"


def test_legacy_report_template_route_does_not_grant_users_admin_access(db, owner, monkeypatch):
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: False)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: True)

    with pytest.raises(ForbiddenError):
        service.authorize_route(db, owner, "/report/templates")


@pytest.fixture
def db():
    # ASGI 同步鉴权在线程池执行；所有线程必须读到同一份隔离测试数据库。
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def owner(db):
    actor = User(username="browser-owner", password="unused", role="user", status=1)
    role = Role(name="用户", code="user", status="active")
    permission = Permission(code=PermissionCode.AGENT_CHAT, name="使用小菱", module="agent")
    db.add_all([actor, role, permission])
    db.flush()
    db.add_all(
        [UserRole(user_id=actor.id, role_id=role.id), RolePermission(role_id=role.id, permission_id=permission.id)]
    )
    db.add(
        AgentResponseRun(
            run_id="browser-run",
            user_id=actor.id,
            surface="user",
            session_key="browser-session",
            status="running",
            checkpoint_json="{}",
        )
    )
    db.commit()
    return actor


def ack(event, claim, *, controls=None, route="/forum", outcome="completed"):
    return BrowserAckIn.model_validate(
        {
            "session_id": "browser-session",
            "surface": "user",
            "client_nonce": event["client_nonce"],
            "claim_token": claim["claim_token"],
            "outcome": outcome,
            "observation": {
                "snapshot_id": event["observation_snapshot_id"],
                "route": route,
                "title": "论坛",
                "controls": controls or [],
                "messages": ["帖子列表已读取"],
            },
        }
    )


def perform(
    db, owner, *, action="inspect", args=None, call_id="inspect-call", controls=None, approved=False, route="/forum"
):
    events = []

    def emit(event):
        events.append(event)
        claim = service.claim_action(
            db, owner, event["action_id"], session_key="browser-session", surface="user", nonce=event["client_nonce"]
        )
        service.acknowledge_action(db, owner, event["action_id"], ack(event, claim, controls=controls, route=route))

    result = asyncio.run(
        service.request_action(
            db,
            owner,
            run_id="browser-run",
            session_key="browser-session",
            surface="user",
            call_id=call_id,
            action=action,
            args=args or {},
            event_sink=emit,
            approved=approved,
        )
    )
    return result, events


def control(label="发布帖子", *, role="button", typ="button", handle="a" * 32):
    return {"client_handle": handle, "label": label, "role": role, "type": typ, "readonly": False}


def test_inspection_preserves_only_observation_and_hashed_execution_credentials(db, owner):
    result, events = perform(db, owner, controls=[control()])
    assert result["status"] == "completed" and result["verification_status"] == "client_observed"
    assert result["server_verified"] is False
    item = result["observation"]["controls"][0]
    assert item["control_handle"].startswith("ctrl_") and "client_handle" not in item
    ledger = db.query(AgentBrowserAction).one()
    assert ledger.nonce_sha256 != events[0]["client_nonce"]
    assert ledger.claim_sha256 and "claim_token" not in json.dumps(result)
    assert "client_nonce" not in json.dumps(result)
    assert agent_responses._public_stream_event(events[0])["client_nonce"] == events[0]["client_nonce"]
    assert agent_responses._public_stream_event({**events[0], "args": {"route": "https://attacker.invalid"}}) is None
    assert agent_responses._public_stream_event({"type": "response.browser.required", "client_nonce": "forged"}) is None


def test_latest_control_maps_exactly_and_old_snapshot_cannot_be_reused(db, owner):
    inspected, _ = perform(db, owner, controls=[control()])
    handle = inspected["observation"]["controls"][0]["control_handle"]
    clicked, events = perform(
        db, owner, action="click", args={"control_handle": handle}, call_id="click-call", approved=True
    )
    assert events[0]["args"] == {
        "client_handle": "a" * 32,
        "route": "/forum",
        "snapshot_id": inspected["observation"]["snapshot_id"],
    }
    assert clicked["server_verified"] is False
    with pytest.raises(ConflictError, match="控件"):
        perform(db, owner, action="click", args={"control_handle": handle}, call_id="stale-call", approved=True)


@pytest.mark.parametrize(
    "action,args",
    [
        ("click", {"control_handle": "fake"}),
        ("fill", {"control_handle": "fake", "value": "text"}),
        ("select", {"control_handle": "fake", "value": "x"}),
        ("check", {"control_handle": "fake", "checked": True}),
    ],
)
def test_unknown_effect_requires_current_user_confirmation_before_dispatch(db, owner, action, args):
    assert review_response_tool("operate_current_page", {"action": action})["needs_confirmation"] is True
    with pytest.raises(ForbiddenError, match="确认"):
        perform(db, owner, action=action, args=args)
    assert db.query(AgentBrowserAction).count() == 0


@pytest.mark.parametrize(
    "route",
    [
        "https://example.invalid",
        "//example.invalid",
        "/api/reset",
        "/forum/%2e%2e",
        "/forum#javascript",
        "/admin/platform",
        "/projects/123",
    ],
)
def test_external_unregistered_and_permission_denied_routes_never_dispatch(db, owner, route):
    with pytest.raises((ForbiddenError, ValidationError)):
        perform(db, owner, action="navigate", args={"route": route})
    assert db.query(AgentBrowserAction).count() == 0


def test_secret_and_file_inputs_are_not_model_accessible(db, owner):
    observed, events = perform(
        db,
        owner,
        controls=[
            control("密码", role="textbox", typ="password"),
            control("API Key", role="textbox", typ="text", handle="b" * 32),
            control("选择源码", role="textbox", typ="file", handle="c" * 32),
        ],
    )
    assert len(observed["observation"]["controls"]) == 1
    handle = observed["observation"]["controls"][0]["control_handle"]
    with pytest.raises(ConflictError, match="用户"):
        perform(
            db,
            owner,
            action="fill",
            args={"control_handle": handle, "value": "/etc/passwd"},
            call_id="file-input",
            approved=True,
        )
    with pytest.raises(ValueError):
        BrowserAckIn.model_validate(
            {
                **ack(events[0], {"claim_token": "z" * 40}).model_dump(),
                "observation": {"snapshot_id": "a" * 32, "route": "/forum", "values": {"password": "anything"}},
            }
        )


def test_same_call_returns_persisted_result_without_new_click_and_arguments_cannot_change(db, owner):
    first, _ = perform(db, owner)
    second, events = perform(db, owner)
    assert second == first and events == [] and db.query(AgentBrowserAction).count() == 1
    with pytest.raises(ConflictError):
        perform(db, owner, action="navigate", args={"route": "/forum"})


def test_two_claims_cannot_execute_the_same_action_and_recovery_rotates_queued_nonce(db, owner):
    async def scenario():
        events = []
        task = asyncio.create_task(
            service.request_action(
                db,
                owner,
                run_id="browser-run",
                session_key="browser-session",
                surface="user",
                call_id="claim-once",
                action="inspect",
                args={},
                event_sink=events.append,
            )
        )
        await asyncio.sleep(0)
        initial = events[0]
        recovered = service.pending_actions(
            db, owner, run_id="browser-run", session_key="browser-session", surface="user"
        )["actions"][0]
        with pytest.raises(ConflictError):
            service.claim_action(
                db,
                owner,
                initial["action_id"],
                session_key="browser-session",
                surface="user",
                nonce=initial["client_nonce"],
            )
        claim = service.claim_action(
            db,
            owner,
            recovered["action_id"],
            session_key="browser-session",
            surface="user",
            nonce=recovered["client_nonce"],
        )
        with pytest.raises(ConflictError):
            service.claim_action(
                db,
                owner,
                recovered["action_id"],
                session_key="browser-session",
                surface="user",
                nonce=recovered["client_nonce"],
            )
        pending = service.pending_actions(
            db, owner, run_id="browser-run", session_key="browser-session", surface="user"
        )["actions"][0]
        assert pending["status"] == "executing" and "client_nonce" not in pending and "args" not in pending
        service.acknowledge_action(db, owner, recovered["action_id"], ack(recovered, claim))
        with pytest.raises(ConflictError):
            service.acknowledge_action(db, owner, recovered["action_id"], ack(recovered, claim))
        return await task

    assert asyncio.run(scenario())["status"] == "completed"


def test_lost_receipt_after_claim_is_indeterminate_and_never_requeued(db, owner):
    async def scenario():
        def emit(event):
            service.claim_action(
                db,
                owner,
                event["action_id"],
                session_key="browser-session",
                surface="user",
                nonce=event["client_nonce"],
            )
            row = db.get(AgentBrowserAction, event["action_id"])
            row.expires_at = service._now() - timedelta(seconds=1)
            db.commit()

        return await service.request_action(
            db,
            owner,
            run_id="browser-run",
            session_key="browser-session",
            surface="user",
            call_id="lost-ack",
            action="inspect",
            args={},
            event_sink=emit,
        )

    result = asyncio.run(scenario())
    assert result["status"] == "indeterminate" and result["completed"] is False and result["retryable"] is False
    assert (
        service.pending_actions(db, owner, run_id="browser-run", session_key="browser-session", surface="user")[
            "actions"
        ]
        == []
    )


def test_login_version_change_stops_waiting_old_account_action(db, owner):
    async def scenario():
        def emit(_event):
            owner.token_version += 1
            db.commit()

        with pytest.raises(ForbiddenError, match="凭据"):
            await service.request_action(
                db,
                owner,
                run_id="browser-run",
                session_key="browser-session",
                surface="user",
                call_id="old-login",
                action="inspect",
                args={},
                event_sink=emit,
            )

    asyncio.run(scenario())
    assert db.query(AgentBrowserAction).one().status == "cancelled"


@pytest.mark.asyncio
async def test_actual_asgi_auth_owner_session_atomic_claim_ack_and_strict_observation(db, owner):
    application = FastAPI()
    register_handlers(application)
    application.include_router(router_module.router, prefix="/api/agent-browser")
    application.dependency_overrides[get_db] = lambda: db
    other = User(username="browser-other", password="unused", role="user", status=1)
    db.add(other)
    db.flush()
    role = db.query(Role).filter_by(code="user").one()
    db.add(UserRole(user_id=other.id, role_id=role.id))
    db.commit()
    headers = {"Authorization": "Bearer " + create_access_token(owner.id, "user")}
    other_headers = {"Authorization": "Bearer " + create_access_token(other.id, "user")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:

        async def emit(event):
            base = {"session_id": "browser-session", "surface": "user", "client_nonce": event["client_nonce"]}
            path = f"/api/agent-browser/actions/{event['action_id']}"
            assert (await client.post(path + "/claim", json=base)).status_code == 401
            assert (await client.post(path + "/claim", json=base, headers=other_headers)).status_code == 404
            assert (
                await client.post(path + "/claim", json={**base, "session_id": "other-session"}, headers=headers)
            ).status_code == 404
            claimed = await client.post(path + "/claim", json=base, headers=headers)
            assert claimed.status_code == 200
            assert (await client.post(path + "/claim", json=base, headers=headers)).status_code == 409
            receipt = ack(event, claimed.json()["data"]).model_dump()
            assert (
                await client.post(path + "/ack", json={**receipt, "script": "fetch('/users')"}, headers=headers)
            ).status_code == 400
            invalid = {**receipt, "claim_token": "z" * 40}
            assert (await client.post(path + "/ack", json=invalid, headers=headers)).status_code == 409
            delivered = await client.post(path + "/ack", json=receipt, headers=headers)
            assert delivered.status_code == 200 and delivered.json()["data"]["server_verified"] is False

        result = await service.request_action(
            db,
            owner,
            run_id="browser-run",
            session_key="browser-session",
            surface="user",
            call_id="http-contract",
            action="inspect",
            args={},
            event_sink=emit,
        )
    assert result["status"] == "completed"


def test_live_tool_metadata_survives_public_filter_without_unregistered_page_injection():
    event = {
        "type": "response.tool.completed",
        "tool_name": "user_execute_capability",
        "team_id": 71,
        "page_action": {
            "capability": "forum.posts.create",
            "account_id": 9,
            "phase": "submitted",
            "page": "https://attacker.invalid",
            "result_route": "/forum/42",
            "mutation": False,
        },
    }
    public = agent_responses._public_stream_event(event)
    assert public["team_id"] == 71 and public["page_action"]["page"] == "/forum"
    assert public["page_action"]["mutation"] is True and public["page_action"]["phase"] == "submitted"
    assert public["page_action"]["result_route"] == "/forum/42"
    public = agent_responses._public_stream_event({**event, "tool_name": "unregistered"})
    assert "page_action" not in public


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["user", "admin"])
@pytest.mark.parametrize("terminal_status", ["completed", "cancelled", "failed"])
async def test_pending_run_creation_window_uses_real_checkpoint_id_and_keeps_owner_binding(db, owner, surface, terminal_status):
    """客户端 run/created 早于 create 提交；404 不应通过伪造 run 或放宽归属修复。"""
    from app.models.agent_mesh import AgentMeshConversation
    from app.services.agent_responses_service import DatabaseCheckpointStore
    from app.services.deepseek_responses_runtime import RunCheckpoint

    if surface == "admin":
        admin_role = Role(name="管理员", code="admin", status="active")
        db.add(admin_role)
        db.flush()
        db.query(UserRole).filter_by(user_id=owner.id).delete(synchronize_session=False)
        owner.role = "admin"
        db.add(UserRole(user_id=owner.id, role_id=admin_role.id))
    session_key = "browser-start-session"
    run_id = "run_client_before_checkpoint"
    db.add(
        AgentMeshConversation(
            user_id=owner.id,
            surface=surface,
            session_key=session_key,
            status="active",
            last_seen_at=service._now(),
        )
    )
    other = User(username="browser-unrelated-owner", password="unused", role="user", status=1)
    db.add(other)
    db.flush()
    role = db.query(Role).filter_by(code="user").one()
    db.add(UserRole(user_id=other.id, role_id=role.id))
    db.commit()
    application = FastAPI()
    register_handlers(application)
    application.include_router(router_module.router, prefix="/api/agent-browser")
    application.include_router(agent_responses.router, prefix="/api/agent-responses")
    application.dependency_overrides[get_db] = lambda: db
    headers = {"Authorization": "Bearer " + create_access_token(owner.id, owner.role)}
    params = {"run_id": run_id, "session_id": session_key, "surface": surface}
    store = DatabaseCheckpointStore(db, user_id=owner.id, surface=surface, session_key=session_key)
    checkpoint = RunCheckpoint(
        run_id=run_id,
        model="test-model",
        transcript=[],
        tools=[],
        last_response={"id": "resp_upstream_response"},
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        confirmation = await client.get("/api/agent-responses/session", params=params, headers=headers)
        assert confirmation.status_code == 200
        assert confirmation.json()["data"]["run"] is None
        # 首次恢复探测发生在真正的运行创建之前；不生成占位检查点或页面动作。
        missing = await client.get("/api/agent-browser/pending", params=params, headers=headers)
        assert missing.status_code == 404
        assert missing.json()["code"] == 40400
        assert missing.json()["message"] == "页面操作运行不存在"
        assert db.query(AgentResponseRun).filter_by(run_id=run_id).count() == 0
        assert db.query(AgentBrowserAction).count() == 0
        assert await store.create(checkpoint) is True
        restored = await store.load(run_id)
        assert restored.run_id == run_id
        assert restored.last_response["id"] == "resp_upstream_response"
        confirmed = (await client.get("/api/agent-responses/session", params=params, headers=headers)).json()["data"]
        assert (confirmed["surface"], confirmed["session_id"], confirmed["run"]["run_id"]) == (surface, session_key, run_id)
        available = await client.get("/api/agent-browser/pending", params=params, headers=headers)
        assert available.status_code == 200 and available.json()["data"] == {"actions": []}
        for invalid in [
            {**params, "run_id": "resp_upstream_response"},
            {**params, "session_id": "other-session"},
            {**params, "surface": "admin" if surface == "user" else "user"},
        ]:
            rejected = await client.get("/api/agent-browser/pending", params=invalid, headers=headers)
            assert rejected.status_code in {403, 404}
        foreign = await client.get(
            "/api/agent-browser/pending",
            params=params,
            headers={"Authorization": "Bearer " + create_access_token(other.id, "user")},
        )
        assert foreign.status_code in {403, 404}
        ack_delivered = asyncio.Event()

        async def wait_for_browser_receipt(_event):
            # 此 fixture 用同一 Session 供 ASGI 线程和工具调用读取。回执提交前
            # 暂停工具的数据库轮询，避免测试自身并发使用非线程安全的 Session。
            await ack_delivered.wait()

        execution = asyncio.create_task(service.request_action(
            db, owner, run_id=run_id, session_key=session_key, surface=surface,
            call_id="confirmed-browser-action", action="inspect", args={}, event_sink=wait_for_browser_receipt,
        ))
        try:
            await asyncio.sleep(0)
            recovered = (await client.get("/api/agent-browser/pending", params=params, headers=headers)).json()["data"]["actions"][0]
            action_path = f"/api/agent-browser/actions/{recovered['action_id']}"
            context = {"session_id": session_key, "surface": surface, "client_nonce": recovered["client_nonce"]}
            claimed = await client.post(action_path + "/claim", json=context, headers=headers)
            assert claimed.status_code == 200
            receipt = {**context, **claimed.json()["data"], "outcome": "completed", "observation": {
                "snapshot_id": recovered["observation_snapshot_id"], "route": "/forum", "title": "论坛", "controls": [], "messages": [],
            }}
            receipt.pop("action_id")
            delivered = await client.post(action_path + "/ack", json=receipt, headers=headers)
            assert delivered.status_code == 200 and delivered.json()["data"]["server_verified"] is False
            ack_delivered.set()
            assert (await execution)["status"] == "completed"
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
        run = db.query(AgentResponseRun).filter_by(run_id=run_id).one()
        run.status = terminal_status
        db.commit()
        stopped = await client.get("/api/agent-browser/pending", params=params, headers=headers)
        assert stopped.status_code == 409
        assert (await client.post(action_path + "/claim", json=context, headers=headers)).status_code == 409
        assert (await client.post(action_path + "/ack", json=receipt, headers=headers)).status_code == 409
        assert db.query(AgentBrowserAction).count() == 1


def test_audit_reviewer_still_requires_permission_and_admin_keeps_access(db, owner, monkeypatch):
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: False)
    monkeypatch.setattr(service.rbac_service, "has_effective_role", lambda *_args: True)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: False)
    with pytest.raises(ForbiddenError):
        service.authorize_route(db, owner, "/audit")
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: True)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: True)
    assert service.authorize_route(db, owner, "/audit") == "/audit"


def test_agent_studio_legacy_route_uses_real_redirect_page_permissions(db, owner, monkeypatch):
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda _db, _id, permission: permission == "agent:view")
    assert service.authorize_route(db, owner, "/agent-studio") == "/agent-studio"


def test_report_template_permission_does_not_bypass_admin_surface(db, owner, monkeypatch):
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: False)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda _db, _id, permission: permission == "report:template_manage")
    with pytest.raises(ForbiddenError):
        service.authorize_route(db, owner, "/report/templates")
