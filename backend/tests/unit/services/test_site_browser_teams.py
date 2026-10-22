"""正式站点成员的页面桥合同、真实浏览器回执和持久运行归属。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.permission_codes import PermissionCode
from app.models.agent_browser_action import AgentBrowserAction
from app.models.agent_response_run import AgentResponseRun
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamTask
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_browser import BrowserAckIn
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_browser_service as browser
from app.services import agent_mesh_service, agent_supervisor_service, agent_team_service, site_agent_service
from app.services.ai_usage_context import run_attribution, usage_context


@pytest.fixture
def owner(db):
    actor = User(username="page-team-owner", password="unused", role="user", status=1)
    role = Role(name="普通用户", code="user", status="active")
    permission = Permission(code=PermissionCode.AGENT_CHAT, name="使用小菱", module="agent")
    db.add_all([actor, role, permission])
    db.flush()
    db.add_all([UserRole(user_id=actor.id, role_id=role.id),
                RolePermission(role_id=role.id, permission_id=permission.id)])
    db.add(AgentResponseRun(run_id="page-team-run", user_id=actor.id, surface="user",
                            session_key="page-team-session", status="running", checkpoint_json="{}"))
    db.commit()
    return actor


def payload(*, action="inspect", args=None):
    return AgentTeamCreateIn.model_validate({
        "surface": "user", "session_id": "page-team-session", "title": "观察当前页面",
        "objective": "只报告浏览器实际观察到的标题和控件",
        "members": [
            {"member_key": "operator", "display_name": "页面观察", "address": "agent:site_operator", "role": "worker"},
            {"member_key": "verifier", "display_name": "观察回执核对", "address": "agent:site_operator",
             "role": "verifier"},
        ],
        "tasks": [
            {"task_key": "observe", "member_key": "operator", "title": "观察页面", "instructions": "按合同操作",
             "input": {"operation": "operate_current_page", "action": action, "args": args or {}}, "max_attempts": 1},
            {"task_key": "verify", "member_key": "verifier", "title": "核对观察回执",
             "instructions": "不能宣称业务成功",
             "input": {"operation": "verify_results"}, "depends_on": ["observe"], "max_attempts": 1},
        ], "max_attempts": 1,
    })


def create(db, owner, *, plan=None, confirmed=False):
    plan = plan or payload()
    review = agent_supervisor_service.review_agent_team_plan(plan.model_dump(mode="json"))
    with usage_context(owner.id, run_attribution(db, owner.id, "page-team-run")):
        return agent_team_service.create_team_from_xiaoling(
            db, owner, plan, supervisor_plan_sha256=review["plan_sha256"] if confirmed else "",
            supervisor_confirmed_by=owner.id if confirmed else None,
        )


async def observe(db, owner, *, controls=None):
    # 使用与真实客户端相同的 pending -> claim -> ack，不 mock 页面服务。
    for _ in range(100):
        pending = browser.pending_actions(db, owner, run_id="page-team-run",
                                         session_key="page-team-session", surface="user")["actions"]
        if pending:
            event = pending[0]
            assert event["account_id"] == owner.id
            assert event["args"] == {}
            assert db.query(AgentResponseRun).filter_by(run_id=event["run_id"]).one().id
            claim = browser.claim_action(db, owner, event["action_id"], session_key=event["session_id"],
                                         surface=event["surface"], nonce=event["client_nonce"])
            browser.acknowledge_action(db, owner, event["action_id"], BrowserAckIn.model_validate({
                "session_id": event["session_id"], "surface": event["surface"],
                "client_nonce": event["client_nonce"], "claim_token": claim["claim_token"],
                "outcome": "completed", "observation": {
                    "snapshot_id": event["observation_snapshot_id"], "route": "/forum", "title": "真实论坛",
                    "controls": controls or [], "messages": ["实际页面观察"],
                },
            }))
            return event
        await asyncio.sleep(.001)
    pytest.fail("正式团队未产生可领取的页面动作")


async def execute(db, owner, created, claim):
    return await site_agent_service.execute_site_task(db, owner, team_id=created["team_id"],
            task_id=claim["task_id"], lease_token=claim["lease_token"])


def test_page_contract_is_discoverable_and_coordinator_stays_coordination_only(db, owner):
    from app.services.agent_responses_service import PrismToolExecutor
    items = agent_mesh_service.list_agents(db, owner, surface="user")["items"]
    operator = next(item for item in items if item["address"] == "agent:site_operator")
    contract = operator["team_input_contract"]
    assert "operate_current_page" in contract["operation"]
    assert contract["operate_current_page"]["required"] == ["action", "args"]
    assert "inspect" in contract["operate_current_page"]["actions"]
    executor = PrismToolExecutor(db, owner, surface="user", session_key="page-team-session", run_id="page-team-run",
                                 mcp_provider=SimpleNamespace(), tool_scope="xiaoling_coordinator")
    names = {item["name"] for item in asyncio.run(executor.tool_schemas())}
    assert "list_agents" in names and "create_agent_team" in names
    assert "operate_current_page" not in names


@pytest.mark.asyncio
async def test_real_member_inspect_and_verifier_keep_client_observed_boundary(db, owner, monkeypatch):
    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    created = create(db, owner)
    claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    result, event = await asyncio.gather(execute(db, owner, created, claim), observe(db, owner, controls=[{
        "client_handle": "a" * 32, "label": "新建帖子", "role": "button", "type": "button", "readonly": False,
    }]))
    assert result["status"] == "completed"
    receipt = result["result_receipt"]
    assert receipt["output"]["observation"]["title"] == "真实论坛"
    assert receipt["output"]["observation"]["controls"][0]["label"] == "新建帖子"
    assert receipt["output"]["verification_status"] == "client_observed"
    assert receipt["output"]["server_verified"] is False
    team = db.get(AgentTeam, created["team_id"])
    assert db.get(AgentResponseRun, team.root_agent_run_id).run_id == event["run_id"]
    assert (await execute(db, owner, created, claim)) == result
    assert db.query(AgentBrowserAction).count() == 1
    agent_team_service.complete_task(db, created["team_id"], claim["task_id"],
                                    lease_token=claim["lease_token"], result=result, success=True)
    verifier = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    verified = await execute(db, owner, created, verifier)
    assert verified["status"] == "completed"
    assert verified["result_receipt"]["output"]["verification_status"] == "client_observed"
    assert verified["result_receipt"]["output"]["server_verified"] is False
    assert "page_action" not in verified["result_receipt"]


def test_page_team_requires_persisted_matching_parent_run(db, owner):
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="运行|会话"):
        agent_team_service.create_team_from_xiaoling(db, owner, payload())
    plan = payload().model_copy(update={"session_id": "other-session"})
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="运行|会话"):
        create(db, owner, plan=plan)


def test_page_input_rejects_identity_or_approval_override():
    original = {"operation": "operate_current_page", "action": "inspect", "args": {}}
    site_agent_service.resolve_site_input(original)
    for key in ("parent_run_id", "user_id", "surface", "session_id", "approved", "team_id", "lease_token",
                "token_version", "_token_version", "browser_token_version"):
        with pytest.raises(ValueError):
            site_agent_service.resolve_site_input({**original, key: "untrusted"})


async def pending_event(db, owner):
    for _ in range(100):
        found = browser.pending_actions(db, owner, run_id="page-team-run",
                                        session_key="page-team-session", surface="user")["actions"]
        if found:
            return found[0]
        await asyncio.sleep(.001)
    pytest.fail("成员未创建页面动作")


def ack_payload(event, claim):
    return BrowserAckIn.model_validate({
        "session_id": event["session_id"], "surface": event["surface"],
        "client_nonce": event["client_nonce"], "claim_token": claim["claim_token"],
        "outcome": "completed", "observation": {
            "snapshot_id": event["observation_snapshot_id"], "route": "/forum", "title": "实际页面",
            "controls": [], "messages": [],
        },
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("invalidated", ["cancel", "lease_expired", "lease_replaced", "stop", "login_changed"])
async def test_waiting_page_member_rejects_invalidated_lease_stop_and_late_ack(
    db, owner, monkeypatch, claimed, invalidated,
):
    from datetime import timedelta

    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    created = create(db, owner)
    lease = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    waiting = asyncio.create_task(execute(db, owner, created, lease))
    try:
        event = await pending_event(db, owner)
        claim = None
        if claimed:
            claim = browser.claim_action(db, owner, event["action_id"], session_key=event["session_id"],
                                         surface=event["surface"], nonce=event["client_nonce"])
        task = db.get(AgentTeamTask, lease["task_id"])
        if invalidated == "cancel":
            agent_team_service.cancel_team(db, owner, created["team_id"], reason="测试人工停止")
        elif invalidated == "lease_expired":
            task.lease_expires_at = browser._now() - timedelta(seconds=1)
        elif invalidated == "lease_replaced":
            task.lease_token = "replacement-lease"
        elif invalidated == "stop":
            run = db.query(AgentResponseRun).filter_by(run_id="page-team-run").one()
            # 状态仍 running，持久停止意图必须已生效。
            run.checkpoint_json = json.dumps({"stop_requested": True})
        else:
            owner.token_version = int(owner.token_version or 0) + 1
        db.commit()
        with pytest.raises((ConflictError, ForbiddenError)):
            if claimed:
                browser.acknowledge_action(db, owner, event["action_id"], ack_payload(event, claim))
            else:
                browser.claim_action(db, owner, event["action_id"], session_key=event["session_id"],
                                     surface=event["surface"], nonce=event["client_nonce"])
        result = await asyncio.wait_for(waiting, timeout=2)
        assert result["status"] == "blocked"
        row = db.get(AgentBrowserAction, event["action_id"], populate_existing=True)
        assert row.status == ("indeterminate" if claimed else "cancelled")
        assert not row.observation_json
        again = await execute(db, owner, created, lease)
        assert again["status"] == "blocked"
        assert db.query(AgentBrowserAction).count() == 1
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_team_page_action_preserves_account_surface_session_and_route_rbac(db, owner, monkeypatch):
    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    created = create(db, owner)
    lease = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    waiting = asyncio.create_task(execute(db, owner, created, lease))
    try:
        event = await pending_event(db, owner)
        other = User(username="other-page-owner", password="unused", role="user", status=1)
        db.add(other)
        db.commit()
        for actor, session, surface in ((other, event["session_id"], "user"),
                                        (owner, "other-page-session", "user"),
                                        (owner, event["session_id"], "admin")):
            with pytest.raises(NotFoundError):
                browser.claim_action(db, actor, event["action_id"], session_key=session,
                                     surface=surface, nonce=event["client_nonce"])
        claim = browser.claim_action(db, owner, event["action_id"], session_key=event["session_id"],
                                     surface=event["surface"], nonce=event["client_nonce"])
        ack = ack_payload(event, claim)
        with pytest.raises(ForbiddenError):
            browser.acknowledge_action(db, owner, event["action_id"], ack.model_copy(update={
                "observation": ack.observation.model_copy(update={"route": "/admin/platform"}),
            }))
        browser.acknowledge_action(db, owner, event["action_id"], ack)
        assert (await waiting)["status"] == "completed"
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    navigation = create(db, owner, plan=payload(action="navigate", args={"route": "/admin/platform"}))
    lease = agent_team_service.claim_next_task(db, navigation["team_id"], lease_seconds=60)
    result = await execute(db, owner, navigation, lease)
    assert result["status"] == "blocked"
    assert db.query(AgentBrowserAction).count() == 1


@pytest.mark.asyncio
async def test_control_confirmation_is_persisted_and_observation_never_proves_business_success(db, owner, monkeypatch):
    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    inspected = create(db, owner)
    lease = agent_team_service.claim_next_task(db, inspected["team_id"], lease_seconds=60)
    result, _ = await asyncio.gather(execute(db, owner, inspected, lease), observe(db, owner, controls=[{
        "client_handle": "b" * 32, "label": "普通按钮", "role": "button", "type": "button", "readonly": False,
    }]))
    handle = result["result_receipt"]["output"]["observation"]["controls"][0]["control_handle"]
    plan = payload(action="click", args={"control_handle": handle})
    with pytest.raises(agent_team_service.AgentTeamAccessError, match="当前账号确认"):
        create(db, owner, plan=plan)
    assert db.query(AgentBrowserAction).count() == 1
    approved = create(db, owner, plan=plan, confirmed=True)
    lease = agent_team_service.claim_next_task(db, approved["team_id"], lease_seconds=60)
    waiting = asyncio.create_task(execute(db, owner, approved, lease))
    try:
        event = await pending_event(db, owner)
        assert event["action"] == "click"
        assert "_team_binding" not in event["args"]
        assert event["args"]["client_handle"] == "b" * 32
        claim = browser.claim_action(db, owner, event["action_id"], session_key=event["session_id"],
                                     surface=event["surface"], nonce=event["client_nonce"])
        browser.acknowledge_action(db, owner, event["action_id"], ack_payload(event, claim))
        clicked = await waiting
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    assert clicked["status"] == "completed"
    assert clicked["result_receipt"]["output"]["server_verified"] is False
    agent_team_service.complete_task(db, approved["team_id"], lease["task_id"],
                                    lease_token=lease["lease_token"], result=clicked, success=True)
    verifier = agent_team_service.claim_next_task(db, approved["team_id"], lease_seconds=60)
    verified = await execute(db, owner, approved, verifier)
    assert verified["status"] == "blocked"
    assert "业务接口独立核验" in verified["summary"]
    assert "page_action" not in verified["result_receipt"]


@pytest.mark.parametrize("action,args", [(None, {}), ([], {}), ("inspect", []), ("inspect", {"approved": True})])
def test_invalid_page_contract_fails_closed(action, args):
    with pytest.raises(ValueError, match="页面动作"):
        site_agent_service.resolve_site_input({"operation": "operate_current_page", "action": action, "args": args})


@pytest.mark.asyncio
async def test_coordinator_waits_for_real_page_member_and_verifier_before_returning(db, owner, monkeypatch):
    from app.services.agent_responses_service import PrismToolExecutor
    from app.services.deepseek_responses_runtime import ToolCall

    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    executor = PrismToolExecutor(db, owner, surface="user", session_key="page-team-session", run_id="page-team-run",
                                mcp_provider=SimpleNamespace(has_tool=lambda _name: False),
                                tool_scope="xiaoling_coordinator")
    discovery = await executor.execute(ToolCall("discover-page-team", "list_agents", {}, "{}"))
    assert discovery.status == "success"
    plan = payload().model_dump(mode="json", exclude={"surface", "session_id", "trace_id"})
    call = ToolCall("create-page-team", "create_agent_team", plan, json.dumps(plan, ensure_ascii=False))
    waiting = asyncio.create_task(executor.execute(call))
    try:
        for _ in range(100):
            team = db.query(AgentTeam).first()
            if team:
                break
            await asyncio.sleep(.001)
        assert team is not None, (waiting.result() if waiting.done() else "组队尚未完成")
        assert not waiting.done()
        created = {"team_id": team.id}
        worker = agent_team_service.claim_next_task(db, team.id, lease_seconds=60)
        result, _ = await asyncio.gather(execute(db, owner, created, worker), observe(db, owner))
        assert not waiting.done()
        agent_team_service.complete_task(db, team.id, worker["task_id"],
                                        lease_token=worker["lease_token"], result=result, success=True)
        verifier = agent_team_service.claim_next_task(db, team.id, lease_seconds=60)
        verified = await execute(db, owner, created, verifier)
        assert verified["status"] == "completed" and "业务结果已独立核验" not in verified["summary"]
        agent_team_service.complete_task(db, team.id, verifier["task_id"],
                                        lease_token=verifier["lease_token"], result=verified, success=True)
        returned = await asyncio.wait_for(waiting, timeout=2)
        assert returned.status == "success"
        assert db.get(AgentTeam, team.id).status == "completed"
        assert db.query(AgentBrowserAction).count() == 1
        # 相同 call_id 恢复时只读取账本，不再组队或创建页面动作。
        assert (await executor.execute(call)).status == "success"
        assert db.query(AgentTeam).count() == 1
        assert db.query(AgentBrowserAction).count() == 1
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "session_expired"])
async def test_coordinator_wait_cancels_team_when_parent_stops_or_login_expires(db, owner, monkeypatch, reason):
    from app.services.agent_responses_service import AgentSessionExpiredError, PrismToolExecutor
    from app.services.deepseek_responses_runtime import ToolCall

    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    created = create(db, owner)
    active = {"session": True}
    executor = PrismToolExecutor(db, owner, surface="user", session_key="page-team-session", run_id="page-team-run",
                                mcp_provider=SimpleNamespace(), session_validator=lambda: active["session"],
                                tool_scope="xiaoling_coordinator")
    call = ToolCall("wait-page-team", "create_agent_team", payload().model_dump(mode="json"), "{}")
    waiting = asyncio.create_task(executor._wait_for_browser_team(call, created))
    try:
        await asyncio.sleep(.005)
        assert not waiting.done()
        if reason == "stop":
            run = db.query(AgentResponseRun).filter_by(run_id="page-team-run").one()
            run.checkpoint_json = json.dumps({"stop_requested": True})
            db.commit()
        else:
            active["session"] = False
        with pytest.raises((ConflictError, AgentSessionExpiredError)):
            await asyncio.wait_for(waiting, timeout=2)
        assert db.get(AgentTeam, created["team_id"], populate_existing=True).status == "cancelled"
        assert agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60) is None
        assert db.query(AgentBrowserAction).count() == 0
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_team_page_policy_deny_never_creates_action(db, owner, monkeypatch):
    from app.services import policy_engine, tool_gateway

    created = create(db, owner)
    lease = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    monkeypatch.setattr(tool_gateway, "authorize", lambda *_a, **_k: SimpleNamespace(
        decision=policy_engine.DENY, risk_level="low", reason="禁止页面操作"))
    result = await execute(db, owner, created, lease)
    assert result["status"] == "blocked" and "策略阻断" in result["summary"]
    assert db.query(AgentBrowserAction).count() == 0


def test_page_task_cannot_use_non_site_operator_member_or_retry(db, owner):
    from app.schemas.agent_team import AgentTeamTaskIn

    task = AgentTeamTaskIn.model_validate(payload().tasks[0].model_dump())
    for address in ("temporary:page-worker", "agent:chat_assistant"):
        with pytest.raises(agent_team_service.AgentTeamValidationError, match="正式站点"):
            agent_team_service._validate_task_scope(db, owner, task, address)
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="只能尝试一次"):
        agent_team_service._validate_task_scope(db, owner, task.model_copy(update={"max_attempts": 2}),
                                              "agent:site_operator")


@pytest.mark.asyncio
async def test_stopped_parent_cannot_return_cached_member_success_or_rewrite_completed_observation(
    db, owner, monkeypatch,
):
    from app.models.agent_response_run import AgentToolExecution

    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    created = create(db, owner)
    lease = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    result, event = await asyncio.gather(execute(db, owner, created, lease), observe(db, owner))
    assert result["status"] == "completed"
    run = db.query(AgentResponseRun).filter_by(run_id="page-team-run").one()
    run.checkpoint_json = json.dumps({"stop_requested": True})
    db.commit()
    stopped = await execute(db, owner, created, lease)
    assert stopped["status"] == "blocked" and "已停止" in stopped["summary"]
    assert db.get(AgentBrowserAction, event["action_id"]).status == "completed"
    ledger = db.query(AgentToolExecution).filter_by(tool_name="site_operate_current_page").one()
    assert ledger.status == "success"  # 原始已发生事实不被新拒绝改写。
    assert db.query(AgentBrowserAction).count() == 1


@pytest.mark.asyncio
async def test_page_team_without_initial_login_binding_fails_closed(db, owner):
    created = create(db, owner)
    event = db.query(AgentTeamEvent).filter_by(
        team_id=created["team_id"], event_type="supervisor.plan_reviewed",
    ).one()
    detail = json.loads(event.detail_json)
    detail.pop("browser_token_version", None)
    event.detail_json = json.dumps(detail)
    db.commit()
    lease = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    result = await asyncio.wait_for(execute(db, owner, created, lease), timeout=2)
    assert result["status"] == "blocked"
    assert "绑定" in result["summary"]
    assert db.query(AgentBrowserAction).count() == 0


@pytest.mark.asyncio
async def test_page_team_captures_login_before_creation_refresh(db, owner, monkeypatch):
    original_version = int(owner.token_version or 0)
    original_create = agent_team_service._create_team

    def create_then_refresh(*args, **kwargs):
        result = original_create(*args, **kwargs)
        owner.token_version = original_version + 1
        db.commit()
        db.refresh(owner)
        return result

    monkeypatch.setattr(agent_team_service, "_create_team", create_then_refresh)
    created = create(db, owner)
    event = db.query(AgentTeamEvent).filter_by(
        team_id=created["team_id"], event_type="supervisor.plan_reviewed",
    ).one()
    assert json.loads(event.detail_json)["browser_token_version"] == original_version
    public_event = next(item for item in created["events"] if item["event_type"] == "supervisor.plan_reviewed")
    assert public_event["detail"]["browser_token_version"] == "[REDACTED]"
    lease = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    result = await asyncio.wait_for(execute(db, owner, created, lease), timeout=2)
    assert result["status"] == "blocked"
    assert db.query(AgentBrowserAction).count() == 0
