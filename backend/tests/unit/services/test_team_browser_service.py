"""真实团队租约、父运行、页面账本及客户端 ACK 的集成边界。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.core.exceptions import ConflictError
from app.core.permission_codes import PermissionCode
from app.models.agent_browser_action import AgentBrowserAction
from app.models.agent_response_run import AgentResponseRun
from app.models.agent_team import AgentTeam
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_browser import BrowserAckIn
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_browser_service as browser
from app.services import agent_supervisor_service, agent_team_service, site_agent_service, team_browser_service


@pytest.fixture
def actor(db):
    user = User(username="site-agent-owner", password="unused", role="user", status=1)
    role = Role(name="普通用户", code="user", status="active")
    permission = Permission(code=PermissionCode.AGENT_CHAT, name="使用小菱", module="agent")
    db.add_all([user, role, permission])
    db.flush()
    db.add_all(
        [UserRole(user_id=user.id, role_id=role.id), RolePermission(role_id=role.id, permission_id=permission.id)]
    )
    db.commit()
    return user


def payload(action="inspect", args=None, attempts=1):
    return AgentTeamCreateIn.model_validate(
        {
            "surface": "user",
            "session_id": "browser-team-session",
            "title": "实际页面验收",
            "objective": "读取页面",
            "members": [
                {"member_key": "page", "display_name": "页面执行员", "address": "agent:site_operator", "role": "worker"}
            ],
            "tasks": [
                {
                    "task_key": "page",
                    "member_key": "page",
                    "title": "页面动作",
                    "instructions": "只操作当前页",
                    "input": {"operation": "operate_current_page", "action": action, "args": args or {}},
                    "max_attempts": attempts,
                }
            ],
        }
    )


def claimed(db, actor, *, action="inspect", args=None, confirmed=False, bind=True):
    parent = AgentResponseRun(
        run_id="browser-team-run",
        user_id=actor.id,
        surface="user",
        session_key="browser-team-session",
        status="running",
        checkpoint_json="{}",
    )
    db.add(parent)
    db.commit()
    plan = payload(action, args)
    review = agent_supervisor_service.review_agent_team_plan(plan.model_dump(mode="json"))
    created = agent_team_service.create_team_from_xiaoling(
        db,
        actor,
        plan,
        supervisor_plan_sha256=review["plan_sha256"] if confirmed else "",
        supervisor_confirmed_by=actor.id if confirmed else None,
    )
    team = db.get(AgentTeam, created["team_id"])
    if bind:
        team.root_agent_run_id = parent.id
        db.commit()
    claim = agent_team_service.claim_next_task(db, team.id, lease_seconds=60)
    return team.id, claim


def execute(db, actor, team_id, claim):
    return site_agent_service.execute_site_task(
        db, actor, team_id=team_id, task_id=claim["task_id"], lease_token=claim["lease_token"]
    )


@pytest.mark.parametrize(
    "action,args", [("inspect", {}), ("navigate", {"route": "/forum"}), ("scroll", {"direction": "down"})]
)
def test_team_contract_accepts_registered_page_reads(action, args):
    spec, parsed = site_agent_service.resolve_site_input(payload(action, args).tasks[0].input)
    assert spec.risk == "read" and parsed == args
    review = agent_supervisor_service.review_agent_team_plan(payload(action, args).model_dump(mode="json"))
    assert review["needs_confirmation"] is False


@pytest.mark.parametrize(
    "raw",
    [
        {"operation": "operate_current_page", "action": "inspect", "args": {}, "run_id": "foreign"},
        {"operation": "operate_current_page", "action": [], "args": {}},
        {"operation": "operate_current_page", "action": "click", "args": {"selector": "button"}},
        {"operation": "operate_current_page", "action": "check", "args": {"control_handle": "a", "checked": "true"}},
        {
            "operation": "operate_current_page",
            "action": "fill",
            "args": {"control_handle": "a", "value": "sk-12345678901234567890"},
        },
    ],
)
def test_team_contract_rejects_identity_overrides_and_unregistered_args(raw):
    with pytest.raises(ValueError):
        site_agent_service.resolve_site_input(raw)


def test_unbound_team_cannot_create_browser_action(db, actor):
    team_id, claim = claimed(db, actor, bind=False)
    result = asyncio.run(execute(db, actor, team_id, claim))
    assert result["status"] == "blocked"
    assert db.query(AgentBrowserAction).count() == 0


def test_page_action_cannot_be_configured_for_blind_retry(db, actor):
    with pytest.raises(agent_team_service.AgentTeamValidationError, match="只能尝试一次"):
        agent_team_service.create_team_from_xiaoling(db, actor, payload(attempts=2))


def test_actual_team_worker_uses_parent_run_and_returns_ack_evidence(db, actor, monkeypatch):
    team_id, claim = claimed(db, actor)
    monkeypatch.setattr(browser, "POLL_SECONDS", 0.001)

    async def client():
        for _ in range(100):
            pending = browser.pending_actions(
                db, actor, run_id="browser-team-run", session_key="browser-team-session", surface="user"
            )["actions"]
            if pending:
                event = pending[0]
                identity = browser.claim_action(
                    db,
                    actor,
                    event["action_id"],
                    session_key="browser-team-session",
                    surface="user",
                    nonce=event["client_nonce"],
                )
                browser.acknowledge_action(
                    db,
                    actor,
                    event["action_id"],
                    BrowserAckIn.model_validate(
                        {
                            "session_id": "browser-team-session",
                            "surface": "user",
                            "client_nonce": event["client_nonce"],
                            "claim_token": identity["claim_token"],
                            "outcome": "completed",
                            "observation": {
                                "snapshot_id": event["observation_snapshot_id"],
                                "route": "/forum",
                                "title": "论坛",
                                "controls": [],
                                "messages": ["列表已显示"],
                            },
                        }
                    ),
                )
                return
            await asyncio.sleep(0.001)
        raise AssertionError("no actual queued browser action")

    async def scenario():
        result, _ = await asyncio.gather(execute(db, actor, team_id, claim), client())
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "completed"
    observation = result["evidence"][0]["data"]
    assert observation["verification_status"] == "client_observed"
    assert observation["server_verified"] is False
    row = db.query(AgentBrowserAction).one()
    assert row.run_id == "browser-team-run" and row.call_id.startswith(f"team-browser:{team_id}:")
    again = asyncio.run(execute(db, actor, team_id, claim))
    assert again == result and db.query(AgentBrowserAction).count() == 1
    # The same old receipt is not a license for a stopped task to claim again.
    task = db.get(agent_team_service.AgentTeamTask, claim["task_id"])
    task.lease_expires_at = browser._now() - timedelta(seconds=1)
    db.commit()
    with pytest.raises(ConflictError):
        browser._owner_action(db, actor, row.id, "browser-team-session", "user")


def action_row(db, actor, team_id, claim, *, status="queued"):
    row = AgentBrowserAction(
        request_id="lease-action",
        input_sha256="a" * 64,
        user_id=actor.id,
        token_version=int(actor.token_version or 0),
        run_id="browser-team-run",
        session_key="browser-team-session",
        surface="user",
        call_id=f"team-browser:{team_id}:{claim['task_id']}:1:{browser._hash(claim['lease_token'])}",
        action="inspect",
        status=status,
        arguments_json="{}",
        observation_snapshot_id="a" * 32,
        nonce_sha256=browser._hash("n" * 32),
        claim_sha256=browser._hash("c" * 32),
        expires_at=browser._now() + timedelta(seconds=60),
    )
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("replacement", ["cleanup_in_progress", "cleanup_retry", "another_generation", "parent_stop"])
@pytest.mark.parametrize("operation", ["claim", "ack", "pending"])
def test_changed_lease_rejects_old_browser_side_effects(db, actor, replacement, operation):
    team_id, claim = claimed(db, actor)
    row = action_row(db, actor, team_id, claim, status="executing" if operation == "ack" else "queued")
    task = db.get(agent_team_service.AgentTeamTask, claim["task_id"])
    if replacement == "parent_stop":
        db.query(AgentResponseRun).one().checkpoint_json = '{"stop_requested":true}'
    else:
        task.lease_token = replacement
    task.lease_expires_at = browser._now() + timedelta(seconds=60)
    db.commit()
    if operation == "pending" and replacement == "parent_stop":
        with pytest.raises(ConflictError):
            browser.pending_actions(db, actor, run_id=row.run_id, session_key=row.session_key, surface="user")
    elif operation == "pending":
        assert browser.pending_actions(db, actor, run_id=row.run_id, session_key=row.session_key, surface="user") == {
            "actions": []
        }
    else:
        with pytest.raises(ConflictError):
            if operation == "claim":
                browser.claim_action(db, actor, row.id, session_key=row.session_key, surface="user", nonce="n" * 32)
            else:
                browser.acknowledge_action(
                    db,
                    actor,
                    row.id,
                    BrowserAckIn.model_validate(
                        {
                            "session_id": row.session_key,
                            "surface": "user",
                            "client_nonce": "n" * 32,
                            "claim_token": "c" * 32,
                            "outcome": "completed",
                            "observation": {
                                "snapshot_id": "a" * 32,
                                "route": "/dashboard",
                                "title": "首页",
                                "controls": [],
                                "messages": [],
                            },
                        }
                    ),
                )
    db.refresh(row)
    assert row.status == ("indeterminate" if operation == "ack" else "cancelled")
    assert row.observation_json is None


def test_browser_wait_timeout_expires_team_instead_of_returning_created_success(db, actor, monkeypatch):
    team_id, _ = claimed(db, actor)
    ticks = iter([0, 10000])
    monkeypatch.setattr(team_browser_service, "monotonic", lambda: next(ticks))
    result = asyncio.run(team_browser_service.wait_for_browser_team(db, actor, team_id))
    assert result["status"] == "expired"
    assert all(task["status"] in {"expired", "completed"} for task in result["tasks"])
    assert result["error"]["reason"] == "browser_team_wait_timeout"


@pytest.mark.parametrize("terminal", ["completed", "failed", "blocked", "cancelled", "expired"])
def test_browser_wait_returns_latest_terminal_snapshot(db, actor, monkeypatch, terminal):
    team_id, _ = claimed(db, actor)
    ticks = iter([0, 200])  # 队列延迟超过旧150秒上限仍能等到真实终态。
    monkeypatch.setattr(team_browser_service, "monotonic", lambda: next(ticks))
    states = iter([{"status": "running"}, {"team_id": team_id, "status": terminal, "summary": "最终账本"}])
    monkeypatch.setattr(agent_team_service, "get_team", lambda *a: next(states))

    async def tick(_):
        pass

    monkeypatch.setattr(team_browser_service.asyncio, "sleep", tick)
    result = asyncio.run(team_browser_service.wait_for_browser_team(db, actor, team_id))
    assert result == {"team_id": team_id, "status": terminal, "summary": "最终账本"}


def test_browser_wait_parent_stop_cancels_live_team(db, actor):
    team_id, _ = claimed(db, actor)
    parent = db.query(AgentResponseRun).one()
    parent.checkpoint_json = '{"stop_requested":true}'
    db.commit()
    result = asyncio.run(team_browser_service.wait_for_browser_team(db, actor, team_id))
    assert result["status"] == "cancelled"
    assert all(task["status"] == "cancelled" for task in result["tasks"])


def test_browser_wait_poll_database_failure_is_not_success(db, actor, monkeypatch):
    from sqlalchemy.exc import OperationalError

    team_id, _ = claimed(db, actor)
    original = agent_team_service.get_team
    calls = 0

    def flaky(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("read", {}, Exception("unavailable"))
        return original(*args)

    monkeypatch.setattr(agent_team_service, "get_team", flaky)
    with pytest.raises(ConflictError, match="状态读取失败"):
        asyncio.run(team_browser_service.wait_for_browser_team(db, actor, team_id))
    assert db.get(AgentTeam, team_id).status == "expired"


def test_browser_wait_coroutine_cancel_closes_team(db, actor, monkeypatch):
    team_id, _ = claimed(db, actor)

    async def interrupt(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(team_browser_service.asyncio, "sleep", interrupt)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(team_browser_service.wait_for_browser_team(db, actor, team_id))
    assert db.get(AgentTeam, team_id).status == "cancelled"


@pytest.mark.parametrize("parent_stop", ["cancelled", "stop_requested"])
def test_completed_browser_team_cannot_override_parent_stop(db, actor, parent_stop):
    team_id, _ = claimed(db, actor)
    team = db.get(AgentTeam, team_id)
    team.status = "completed"
    parent = db.query(AgentResponseRun).one()
    if parent_stop == "cancelled":
        parent.status = "cancelled"
    else:
        parent.checkpoint_json = '{"stop_requested":true}'
    db.commit()
    with pytest.raises(ConflictError, match="所属运行已停止"):
        asyncio.run(team_browser_service.wait_for_browser_team(db, actor, team_id))
    assert db.get(AgentTeam, team_id).status == "completed"


@pytest.mark.parametrize("stop_timing", ["before", "after"])
def test_stopped_parent_rejects_browser_dispatch(db, actor, monkeypatch, stop_timing):
    team_id, claim = claimed(db, actor)
    parent = db.query(AgentResponseRun).one()
    if stop_timing == "before":
        parent.checkpoint_json = '{"stop_requested":true}'
        db.commit()
    called = []

    async def must_not_request(*args, **kwargs):
        called.append(True)
        parent.checkpoint_json = '{"stop_requested":true}'
        db.commit()
        return {"completed": True}

    monkeypatch.setattr(browser, "request_action", must_not_request)
    result = asyncio.run(execute(db, actor, team_id, claim))
    assert result["status"] == "blocked"
    assert len(called) == (0 if stop_timing == "before" else 1)
    assert db.query(AgentBrowserAction).count() == 0


@pytest.mark.parametrize("operation", ["dispatch", "claim", "ack"])
def test_lease_expiring_while_waiting_for_parent_lock_rejects_action(db, actor, monkeypatch, operation):
    """Advance time at the parent locking read, with team/task already locked."""
    team_id, claim = claimed(db, actor)
    task = db.get(agent_team_service.AgentTeamTask, claim["task_id"])
    now = browser._now()
    task.lease_expires_at = now + timedelta(seconds=5)
    db.commit()
    row = action_row(db, actor, team_id, claim, status="executing" if operation == "ack" else "queued")
    if operation == "dispatch":
        db.delete(row)
        db.commit()
    lock_reads = []

    def expire_at_parent_lock(state):
        statement = state.statement
        if (
            state.is_select
            and getattr(statement, "_for_update_arg", None) is not None
            and any(table.name == AgentResponseRun.__tablename__ for table in statement.get_final_froms())
        ):
            lock_reads.append(True)
            monkeypatch.setattr(browser, "_now", lambda: now + timedelta(seconds=6))

    async def unexpected_dispatch(_event):
        raise AssertionError("expired task must not dispatch a browser action")

    event.listen(db, "do_orm_execute", expire_at_parent_lock)
    try:
        with pytest.raises(ConflictError):
            if operation == "dispatch":
                asyncio.run(
                    browser.request_action(
                        db,
                        actor,
                        run_id=row.run_id,
                        session_key=row.session_key,
                        surface=row.surface,
                        call_id=row.call_id,
                        action="inspect",
                        args={},
                        event_sink=unexpected_dispatch,
                    )
                )
            elif operation == "claim":
                browser.claim_action(db, actor, row.id, session_key=row.session_key, surface="user", nonce="n" * 32)
            else:
                browser.acknowledge_action(
                    db,
                    actor,
                    row.id,
                    BrowserAckIn.model_validate(
                        {
                            "session_id": row.session_key,
                            "surface": "user",
                            "client_nonce": "n" * 32,
                            "claim_token": "c" * 32,
                            "outcome": "completed",
                            "observation": {
                                "snapshot_id": "a" * 32,
                                "route": "/dashboard",
                                "title": "首页",
                                "controls": [],
                                "messages": [],
                            },
                        }
                    ),
                )
    finally:
        event.remove(db, "do_orm_execute", expire_at_parent_lock)
    assert lock_reads
    saved = db.query(AgentBrowserAction).populate_existing().all()
    if operation == "dispatch":
        assert not saved
    else:
        assert saved[0].status == ("indeterminate" if operation == "ack" else "cancelled")
        assert saved[0].observation_json is None
