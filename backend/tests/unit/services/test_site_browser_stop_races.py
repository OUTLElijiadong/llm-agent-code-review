"""页面团队停止回归：停止意图落库后，不再接受 ACK、核验或任务成功提交。"""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.database import Base
from app.core.exceptions import ConflictError
from app.core.permission_codes import PermissionCode
from app.models.agent_browser_action import AgentBrowserAction
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution, response_run_stop_requested
from app.models.agent_team import AgentTeam, AgentTeamTask
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_browser import BrowserAckIn
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_browser_service as browser
from app.services import agent_team_service, site_agent_service
from app.services.agent_responses_service import PrismToolExecutor
from app.services.ai_usage_context import run_attribution, usage_context
from app.services.deepseek_responses_runtime import ToolCall

RUN_ID = "page-stop-race-run"
SESSION_KEY = "page-stop-race-session"


@pytest.fixture
def stop_race_sessions():
    # 独立物理连接共享内存数据库；不使用默认 :memory: 的同线程单连接池，
    # 也不依赖旧测试模块的导入名或 pytest 的 import mode。
    engine = create_engine(
        f"sqlite:///file:page_stop_{uuid4().hex}?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def race_db(stop_race_sessions):
    with stop_race_sessions() as db:
        yield db


@pytest.fixture
def race_owner(race_db, monkeypatch):
    monkeypatch.setattr(browser, "POLL_SECONDS", .001)
    actor = User(username="page-stop-owner", password="unused", role="user", status=1)
    role = Role(name="普通用户", code="user", status="active")
    permission = Permission(code=PermissionCode.AGENT_CHAT, name="使用小菱", module="agent")
    race_db.add_all([actor, role, permission])
    race_db.flush()
    race_db.add_all([
        UserRole(user_id=actor.id, role_id=role.id),
        RolePermission(role_id=role.id, permission_id=permission.id),
        AgentResponseRun(
            run_id=RUN_ID, user_id=actor.id, surface="user", session_key=SESSION_KEY,
            status="running", checkpoint_json="{}",
        ),
    ])
    race_db.commit()
    return actor


def _plan():
    return AgentTeamCreateIn.model_validate({
        "surface": "user", "session_id": SESSION_KEY,
        "title": "页面停止竞态", "objective": "只核对持久浏览器观察",
        "members": [
            {"member_key": "operator", "display_name": "页面观察",
             "address": "agent:site_operator", "role": "worker"},
            {"member_key": "verifier", "display_name": "观察核验",
             "address": "agent:site_operator", "role": "verifier"},
        ],
        "tasks": [
            {"task_key": "observe", "member_key": "operator", "title": "观察页面",
             "instructions": "按页面合同观察",
             "input": {"operation": "operate_current_page", "action": "inspect", "args": {}},
             "max_attempts": 1},
            {"task_key": "verify", "member_key": "verifier", "title": "核对观察",
             "instructions": "观察不能证明业务写入",
             "input": {"operation": "verify_results"}, "depends_on": ["observe"],
             "max_attempts": 1},
        ],
        "max_attempts": 1,
    })


def _create_team(db, owner):
    with usage_context(owner.id, run_attribution(db, owner.id, RUN_ID)):
        return agent_team_service.create_team_from_xiaoling(db, owner, _plan())


def _commit_parent_stop(factory, reader, *, run_id=RUN_ID):
    """模拟取消入口先提交停止意图、尚未清理团队的真实事务边界。"""
    with factory() as stopper:
        assert (stopper.connection().connection.driver_connection
                is not reader.connection().connection.driver_connection)
        parent = stopper.query(AgentResponseRun).filter_by(run_id=run_id).one()
        checkpoint = json.loads(parent.checkpoint_json or "{}")
        checkpoint.update(stop_requested=True, stop_reason="人工停止竞态回归")
        parent.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False)
        parent.version = int(parent.version or 0) + 1
        stopper.commit()
        assert response_run_stop_requested(parent)
        assert parent.status == "running"  # 不能仅依靠 cancelled 终态拒绝。


async def _pending_event(db, owner, execution):
    deadline = asyncio.get_running_loop().time() + 3
    while asyncio.get_running_loop().time() < deadline:
        actions = browser.pending_actions(
            db, owner, run_id=RUN_ID, session_key=SESSION_KEY, surface="user",
        )["actions"]
        if actions:
            return actions[0]
        if execution.done():
            pytest.fail(f"页面成员在产生动作前结束：{execution.result()!r}")
        await asyncio.sleep(.001)
    pytest.fail("正式页面成员未产生可领取的动作")


def _claim(db, owner, event):
    return browser.claim_action(
        db, owner, event["action_id"], session_key=SESSION_KEY, surface="user",
        nonce=event["client_nonce"],
    )


def _ack_payload(event, claim):
    return BrowserAckIn.model_validate({
        "session_id": SESSION_KEY, "surface": "user", "client_nonce": event["client_nonce"],
        "claim_token": claim["claim_token"], "outcome": "completed",
        "observation": {
            "snapshot_id": event["observation_snapshot_id"], "route": "/forum",
            "title": "停止竞态页面", "controls": [], "messages": [],
        },
    })


async def _execute(db, owner, created, lease):
    return await site_agent_service.execute_site_task(
        db, owner, team_id=created["team_id"], task_id=lease["task_id"],
        lease_token=lease["lease_token"],
    )


async def _finish_inspect(db, owner, factory):
    created = _create_team(db, owner)
    lease = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    assert lease is not None
    with factory() as worker_db:
        execution = asyncio.create_task(_execute(worker_db, owner, created, lease))
        try:
            event = await _pending_event(db, owner, execution)
            claim = _claim(db, owner, event)
            browser.acknowledge_action(db, owner, event["action_id"], _ack_payload(event, claim))
            result = await asyncio.wait_for(execution, timeout=3)
            assert result["status"] == "completed"
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    agent_team_service.complete_task(
        db, created["team_id"], lease["task_id"], lease_token=lease["lease_token"],
        result=result, success=True,
    )
    return created, event["action_id"]


@pytest.mark.asyncio
async def test_ack_rejects_stop_committed_after_parent_precheck(
    race_db, race_owner, stop_race_sessions, monkeypatch,
):
    created = _create_team(race_db, race_owner)
    lease = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert lease is not None
    with stop_race_sessions() as worker_db:
        execution = asyncio.create_task(_execute(worker_db, race_owner, created, lease))
        try:
            event = await _pending_event(race_db, race_owner, execution)
            claim = _claim(race_db, race_owner, event)
            original_owner_run = browser._owner_run
            stops = []

            def check_parent_then_stop(db, user, **kwargs):
                parent = original_owner_run(db, user, **kwargs)
                if db is race_db and not stops:
                    # 保留真实前置校验；只在校验通过后插入另一连接的停止提交。
                    assert not response_run_stop_requested(parent)
                    _commit_parent_stop(stop_race_sessions, db)
                    stops.append(True)
                return parent

            with monkeypatch.context() as patch:
                patch.setattr(browser, "_owner_run", check_parent_then_stop)
                with pytest.raises(ConflictError):
                    browser.acknowledge_action(
                        race_db, race_owner, event["action_id"], _ack_payload(event, claim),
                    )
            assert stops == [True]
            race_db.rollback()
            action = race_db.get(AgentBrowserAction, event["action_id"], populate_existing=True)
            assert action.status != "completed"
            assert not action.observation_json
            result = await asyncio.wait_for(execution, timeout=3)
            assert result["status"] == "blocked"
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_verifier_rejects_parent_stop_after_inspect_completes(
    race_db, race_owner, stop_race_sessions,
):
    created, action_id = await _finish_inspect(race_db, race_owner, stop_race_sessions)
    verifier = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert verifier is not None and verifier["task_key"] == "verify"
    _commit_parent_stop(stop_race_sessions, race_db)

    with stop_race_sessions() as verifier_db:
        result = await _execute(verifier_db, race_owner, created, verifier)
    assert result["status"] == "blocked", "父运行已停止，核验成员不得再交回成功结果"
    race_db.rollback()
    # 停止不应抹掉停止前已经成立的观察事实。
    assert race_db.get(AgentBrowserAction, action_id, populate_existing=True).status == "completed"


@pytest.mark.asyncio
async def test_wait_checks_parent_stop_before_returning_completed_team(
    race_db, race_owner, stop_race_sessions,
):
    created, _ = await _finish_inspect(race_db, race_owner, stop_race_sessions)
    verifier = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert verifier is not None
    with stop_race_sessions() as verifier_db:
        result = await _execute(verifier_db, race_owner, created, verifier)
    assert result["status"] == "completed"
    agent_team_service.complete_task(
        race_db, created["team_id"], verifier["task_id"], lease_token=verifier["lease_token"],
        result=result, success=True,
    )
    assert race_db.get(AgentTeam, created["team_id"], populate_existing=True).status == "completed"
    _commit_parent_stop(stop_race_sessions, race_db)
    executor = PrismToolExecutor(
        race_db, race_owner, surface="user", session_key=SESSION_KEY, run_id=RUN_ID,
        mcp_provider=SimpleNamespace(), tool_scope="xiaoling_coordinator",
    )
    call = ToolCall("wait-stopped-page-team", "create_agent_team", _plan().model_dump(mode="json"), "{}")

    with pytest.raises(ConflictError):
        await asyncio.wait_for(executor._wait_for_browser_team(call, created), timeout=3)


@pytest.mark.asyncio
async def test_complete_task_rejects_success_if_parent_stopped_before_submission(
    race_db, race_owner, stop_race_sessions,
):
    created, action_id = await _finish_inspect(race_db, race_owner, stop_race_sessions)
    verifier = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert verifier is not None
    with stop_race_sessions() as verifier_db:
        result = await _execute(verifier_db, race_owner, created, verifier)
    assert result["status"] == "completed"  # 计算时仍活跃，只在提交前停止。
    _commit_parent_stop(stop_race_sessions, race_db)

    with pytest.raises((agent_team_service.AgentTeamLeaseError, ConflictError)):
        agent_team_service.complete_task(
            race_db, created["team_id"], verifier["task_id"], lease_token=verifier["lease_token"],
            result=result, success=True,
        )
    race_db.rollback()
    task = race_db.get(AgentTeamTask, verifier["task_id"], populate_existing=True)
    assert task.status != "completed"
    assert not task.result_json or json.loads(task.result_json).get("status") != "completed"
    assert race_db.get(AgentTeam, created["team_id"], populate_existing=True).status != "completed"
    assert race_db.get(AgentBrowserAction, action_id, populate_existing=True).status == "completed"


@pytest.mark.asyncio
async def test_request_action_rejects_stop_committed_after_parent_precheck(
    race_db, race_owner, stop_race_sessions, monkeypatch,
):
    created = _create_team(race_db, race_owner)
    lease = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert lease is not None

    with stop_race_sessions() as requester:
        original_owner_run = browser._owner_run
        stops = []

        def check_parent_then_stop(db, user, **kwargs):
            parent = original_owner_run(db, user, **kwargs)
            if db is requester and not stops:
                _commit_parent_stop(stop_race_sessions, db)
                stops.append(True)
            return parent

        monkeypatch.setattr(browser, "_owner_run", check_parent_then_stop)
        with pytest.raises(ConflictError):
            await asyncio.wait_for(browser.request_action(
                requester, race_owner, run_id=RUN_ID, session_key=SESSION_KEY,
                surface="user", call_id="request-stop-after-precheck", action="inspect", args={},
                event_sink=None,
                team_binding={"team_id": created["team_id"], "task_id": lease["task_id"],
                              "lease_sha256": hashlib.sha256(lease["lease_token"].encode()).hexdigest()},
            ), timeout=3)
        assert stops == [True]
        requester.rollback()
    race_db.rollback()
    assert race_db.query(AgentBrowserAction).count() == 0


@pytest.mark.asyncio
async def test_browser_success_ledger_rejects_stop_committed_after_final_live_check(
    race_db, race_owner, stop_race_sessions, monkeypatch,
):
    created = _create_team(race_db, race_owner)
    lease = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert lease is not None
    original_live_task = site_agent_service._live_task
    stops = []
    with stop_race_sessions() as worker_db:
        def check_live_then_stop(db, *args, **kwargs):
            live = original_live_task(db, *args, **kwargs)
            if (db is worker_db and not stops
                    and db.query(AgentBrowserAction).filter_by(run_id=RUN_ID, status="completed").count()):
                _commit_parent_stop(stop_race_sessions, db)
                stops.append(True)
            return live

        monkeypatch.setattr(site_agent_service, "_live_task", check_live_then_stop)
        execution = asyncio.create_task(_execute(worker_db, race_owner, created, lease))
        try:
            pending = await _pending_event(race_db, race_owner, execution)
            claim = _claim(race_db, race_owner, pending)
            browser.acknowledge_action(race_db, race_owner, pending["action_id"], _ack_payload(pending, claim))
            result = await asyncio.wait_for(execution, timeout=3)
            assert stops == [True]
            assert result["status"] == "blocked"
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    race_db.rollback()
    ledger = race_db.query(AgentToolExecution).filter_by(tool_name="site_operate_current_page").one()
    assert ledger.status != "success"
    assert not ledger.result_json or json.loads(ledger.result_json).get("status") != "completed"
    assert race_db.get(AgentBrowserAction, pending["action_id"], populate_existing=True).status == "completed"


@pytest.mark.asyncio
async def test_distinct_root_scope_keeps_page_bridge_and_parent_lock_order(
    race_db, race_owner, stop_race_sessions,
):
    root = race_db.query(AgentResponseRun).filter_by(run_id=RUN_ID).one()
    root.run_id = "page-stop-root-run"
    root.session_key = "root-only-session"
    race_db.flush()
    direct = AgentResponseRun(
        run_id=RUN_ID, user_id=race_owner.id, surface="user", session_key=SESSION_KEY,
        status="running", checkpoint_json="{}", root_agent_run_id=root.id,
    )
    race_db.add(direct)
    race_db.commit()
    created = _create_team(race_db, race_owner)
    team = race_db.get(AgentTeam, created["team_id"])
    assert team.root_agent_run_id == root.id and team.agent_run_id == direct.id
    lease = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert lease is not None

    def record_locks(target):
        def record(state):
            statement = state.statement
            if not state.is_select or getattr(statement, "_for_update_arg", None) is None:
                return
            entity = statement.column_descriptions[0].get("entity")
            target.append((entity, statement.compile().params))
        return record

    def assert_parent_prefix(locks):
        # SQLite 不执行行锁；这里只核对真实调用发起的锁查询顺序。
        # 停止拒绝由本文件其它独立连接交错用例验证。
        assert locks[0][0] is AgentResponseRun and locks[0][1]["id_1"] == root.id
        assert locks[1][0] is AgentResponseRun and locks[1][1]["id_1"] == direct.id
        assert any(entity is AgentTeam for entity, _ in locks[2:])
        assert any(entity is AgentTeamTask for entity, _ in locks[2:])

    reader_locks, writer_locks = [], []
    with stop_race_sessions() as worker_db:
        record_reader, record_writer = record_locks(reader_locks), record_locks(writer_locks)
        event.listen(race_db, "do_orm_execute", record_reader)
        event.listen(worker_db, "do_orm_execute", record_writer)
        execution = asyncio.create_task(_execute(worker_db, race_owner, created, lease))
        try:
            pending = await _pending_event(race_db, race_owner, execution)
            assert_parent_prefix(writer_locks)  # request_action 的创建边界
            reader_locks.clear()
            claim = _claim(race_db, race_owner, pending)
            assert_parent_prefix(reader_locks)
            reader_locks.clear()
            browser.acknowledge_action(race_db, race_owner, pending["action_id"], _ack_payload(pending, claim))
            assert_parent_prefix(reader_locks)
            result = await asyncio.wait_for(execution, timeout=3)
            assert result["status"] == "completed"
        finally:
            event.remove(race_db, "do_orm_execute", record_reader)
            event.remove(worker_db, "do_orm_execute", record_writer)
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    reader_locks.clear()
    event.listen(race_db, "do_orm_execute", record_reader)
    try:
        agent_team_service.complete_task(
            race_db, created["team_id"], lease["task_id"], lease_token=lease["lease_token"], result=result,
        )
        assert_parent_prefix(reader_locks)
    finally:
        event.remove(race_db, "do_orm_execute", record_reader)
    verifier = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert verifier is not None
    _commit_parent_stop(stop_race_sessions, race_db, run_id=root.run_id)
    with stop_race_sessions() as verifier_db:
        rejected = await _execute(verifier_db, race_owner, created, verifier)
    assert rejected["status"] == "blocked"


def _commit_login_rotation(factory, reader, owner_id):
    """只轮换登录版本，父运行与任务租约保持有效。"""
    with factory() as writer:
        assert (writer.connection().connection.driver_connection
                is not reader.connection().connection.driver_connection)
        actor = writer.get(User, owner_id)
        actor.token_version = int(actor.token_version or 0) + 1
        writer.commit()
        return int(actor.token_version)


@pytest.mark.asyncio
async def test_page_team_rejects_login_rotation_before_first_action(
    race_db, race_owner, stop_race_sessions,
):
    created = _create_team(race_db, race_owner)
    lease = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    owner_id = race_owner.id
    original_version = int(race_owner.token_version or 0)
    assert _commit_login_rotation(stop_race_sessions, race_db, owner_id) == original_version + 1
    race_db.refresh(race_owner)  # 新客户端持有新版身份，不能让旧客户端检查替代团队绑定。
    with stop_race_sessions() as worker_db:
        execution = asyncio.create_task(_execute(worker_db, race_owner, created, lease))
        try:
            # 旧实现会为旧团队签发新版动作；真实客户端回执使红灯直接落在业务断言上。
            for _ in range(100):
                await asyncio.sleep(.001)
                if execution.done():
                    break
                actions = browser.pending_actions(
                    race_db, race_owner, run_id=RUN_ID, session_key=SESSION_KEY, surface="user",
                )["actions"]
                if actions:
                    action = actions[0]
                    claim = _claim(race_db, race_owner, action)
                    browser.acknowledge_action(race_db, race_owner, action["action_id"],
                                               _ack_payload(action, claim))
                    break
            result = await asyncio.wait_for(execution, timeout=3)
            assert result["status"] == "blocked", "旧页面团队不得取得新登录凭据后继续执行"
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    race_db.rollback()
    assert race_db.query(AgentBrowserAction).count() == 0
    assert race_db.query(AgentToolExecution).filter_by(status="success").count() == 0


@pytest.mark.asyncio
async def test_browser_ledger_rejects_login_rotation_after_final_live_check(
    race_db, race_owner, stop_race_sessions, monkeypatch,
):
    created = _create_team(race_db, race_owner)
    lease = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    owner_id = race_owner.id
    with stop_race_sessions() as worker_db:
        original_live_task = site_agent_service._live_task
        checks = []

        def check_live_then_rotate(db, user, *args, **kwargs):
            live = original_live_task(db, user, *args, **kwargs)
            if db is worker_db:
                checks.append(True)
                if len(checks) == 2:
                    _commit_login_rotation(stop_race_sessions, db, owner_id)
            return live

        monkeypatch.setattr(site_agent_service, "_live_task", check_live_then_rotate)
        execution = asyncio.create_task(_execute(worker_db, race_owner, created, lease))
        try:
            pending = await _pending_event(race_db, race_owner, execution)
            claim = _claim(race_db, race_owner, pending)
            browser.acknowledge_action(race_db, race_owner, pending["action_id"], _ack_payload(pending, claim))
            result = await asyncio.wait_for(execution, timeout=3)
            assert len(checks) >= 2
            assert result["status"] == "blocked", "最终校验后轮换登录版本不得写入成功账本"
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    race_db.rollback()
    ledger = race_db.query(AgentToolExecution).filter_by(tool_name="site_operate_current_page").one()
    assert ledger.status != "success"
    assert race_db.get(AgentBrowserAction, pending["action_id"], populate_existing=True).status == "completed"


@pytest.mark.asyncio
async def test_page_verifier_rejects_login_rotation_after_observation(
    race_db, race_owner, stop_race_sessions,
):
    created, action_id = await _finish_inspect(race_db, race_owner, stop_race_sessions)
    verifier = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    assert verifier is not None
    _commit_login_rotation(stop_race_sessions, race_db, race_owner.id)
    with stop_race_sessions() as verifier_db:
        result = await _execute(verifier_db, race_owner, created, verifier)
    assert result["status"] == "blocked", "核验必须沿用创建团队时的登录版本"
    race_db.rollback()
    assert race_db.get(AgentBrowserAction, action_id, populate_existing=True).status == "completed"


@pytest.mark.asyncio
async def test_page_wait_rejects_login_rotation_before_returning_completed_team(
    race_db, race_owner, stop_race_sessions,
):
    from app.core.exceptions import ForbiddenError

    created, _ = await _finish_inspect(race_db, race_owner, stop_race_sessions)
    verifier = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    with stop_race_sessions() as verifier_db:
        result = await _execute(verifier_db, race_owner, created, verifier)
    assert result["status"] == "completed"
    agent_team_service.complete_task(
        race_db, created["team_id"], verifier["task_id"], lease_token=verifier["lease_token"],
        result=result, success=True,
    )
    _commit_login_rotation(stop_race_sessions, race_db, race_owner.id)
    executor = PrismToolExecutor(
        race_db, race_owner, surface="user", session_key=SESSION_KEY, run_id=RUN_ID,
        mcp_provider=SimpleNamespace(), tool_scope="xiaoling_coordinator",
    )
    call = ToolCall("wait-rotated-page-team", "create_agent_team", _plan().model_dump(mode="json"), "{}")
    with pytest.raises(ForbiddenError):
        await asyncio.wait_for(executor._wait_for_browser_team(call, created), timeout=3)


@pytest.mark.asyncio
async def test_request_action_keeps_creation_version_and_locks_user_for_issue_claim_ack(
    race_db, race_owner, stop_race_sessions, monkeypatch,
):
    created = _create_team(race_db, race_owner)
    lease = agent_team_service.claim_next_task(race_db, created["team_id"], lease_seconds=60)
    original_version = int(race_owner.token_version or 0)
    original_owner_run = browser._owner_run
    locking_reads = []
    owner_checks = []

    def record_user_lock(state):
        mapper = state.bind_arguments.get("mapper")
        if (mapper is not None and mapper.class_ is User
                and getattr(state.statement, "_for_update_arg", None) is not None):
            locking_reads.append(state.session)

    with stop_race_sessions() as requester:
        def check_creation_version(db, user, **kwargs):
            if db is requester:
                owner_checks.append(kwargs.get("expected_token_version"))
                assert kwargs.get("expected_token_version") == original_version
            return original_owner_run(db, user, **kwargs)

        monkeypatch.setattr(browser, "_owner_run", check_creation_version)
        event.listen(requester, "do_orm_execute", record_user_lock)
        event.listen(race_db, "do_orm_execute", record_user_lock)
        request_kwargs = {
            "run_id": RUN_ID, "session_key": SESSION_KEY, "surface": "user",
            "call_id": "fixed-version-through-browser-bridge", "action": "inspect", "args": {},
            "event_sink": None,
            "team_binding": {"team_id": created["team_id"], "task_id": lease["task_id"],
                             "lease_sha256": hashlib.sha256(lease["lease_token"].encode()).hexdigest()},
        }
        execution = asyncio.create_task(browser.request_action(requester, race_owner, **request_kwargs))
        try:
            pending = await _pending_event(race_db, race_owner, execution)
            assert requester in locking_reads, "签发必须用 User 锁定读复核固定版本"
            locking_reads.clear()
            claim = _claim(race_db, race_owner, pending)
            assert race_db in locking_reads, "领取必须用 User 锁定读复核固定版本"
            locking_reads.clear()
            browser.acknowledge_action(race_db, race_owner, pending["action_id"], _ack_payload(pending, claim))
            assert race_db in locking_reads, "ACK 必须用 User 锁定读复核固定版本"
            result = await asyncio.wait_for(execution, timeout=3)
            assert result["completed"] is True
            assert len(owner_checks) >= 3
            action = race_db.get(AgentBrowserAction, pending["action_id"], populate_existing=True)
            assert action.token_version == original_version
            locking_reads.clear()
            repeated = await browser.request_action(requester, race_owner, **request_kwargs)
            assert repeated["completed"] is True
            assert requester in locking_reads, "重入返回结果前也必须用 User 锁定读复核固定版本"
        finally:
            event.remove(requester, "do_orm_execute", record_user_lock)
            event.remove(race_db, "do_orm_execute", record_user_lock)
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
