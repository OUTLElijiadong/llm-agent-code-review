"""MySQL group attribution locks must stay shared across team authorization reads."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - register models for the fresh-chain fixture
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.enterprise_agent_team import EnterpriseAgentTeam, EnterpriseAgentTeamMember, EnterpriseAgentTeamTask
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_assistant_billing_group as billing_group
from app.services import enterprise_assistant_runtime_service as parent_runtime
from app.services import enterprise_assistant_service as assistant_service
from app.services import enterprise_org_service as org

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _seed(factory):
    suffix = uuid4().hex[:12]
    permission, plan, capability = {"messages": []}, {"project_id": None}, {"execution_mode": "model_only"}
    with factory.begin() as db:
        owner = User(username=f"group-lock-owner-{suffix}", password="x", role="user", status=1)
        users = [User(username=f"group-lock-{suffix}-{i}", password="x", role="user", status=1) for i in range(2)]
        db.add_all([owner, *users])
        db.flush()
        company = Enterprise(
            name="计费锁测试",
            owner_id=owner.id,
            seat_limit=3,
            status="active",
            billing_suspended=False,
            paid_order_id=f"group-lock-{suffix}",
        )
        db.add(company)
        db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name=f"组-{suffix}", status="active")
        db.add(group)
        db.flush()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
        for user in users:
            db.add(EnterpriseMember(enterprise_id=company.id, user_id=user.id, role="member", status="active"))
            db.add(
                EnterpriseGroupMember(
                    enterprise_id=company.id, group_id=group.id, user_id=user.id, role="member", status="active"
                )
            )
        room = EnterpriseConversation(
            enterprise_id=company.id,
            kind="group",
            conversation_key=f"group:{group.id}",
            group_id=group.id,
            created_by=owner.id,
        )
        db.add(room)
        db.flush()
        tasks, teams = [], []
        for index, user in enumerate(users):
            message = EnterpriseChatMessage(
                enterprise_id=company.id,
                conversation_id=room.id,
                sender_id=user.id,
                sender_type="human",
                client_message_id=f"message-{suffix}-{index}",
                content="@小菱 执行任务",
            )
            db.add(message)
            db.flush()
            run = EnterpriseAssistantRun(
                public_id=f"run-{suffix}-{index}",
                request_key=f"request-{suffix}-{index}",
                enterprise_id=company.id,
                conversation_id=room.id,
                group_id=group.id,
                initiator_user_id=user.id,
                source_message_id=message.id,
                status="completed",
                attempt_count=1,
                prompt_sha256="p" * 64,
                billing_attribution_version=1,
                billing_group_id=group.id,
            )
            db.add(run)
            db.flush()
            state = EnterpriseAssistantRuntimeState(
                assistant_run_id=run.id,
                enterprise_id=company.id,
                conversation_id=room.id,
                group_id=group.id,
                initiator_user_id=user.id,
                source_message_id=message.id,
                status="completed",
                checkpoint_json="{}",
                permission_snapshot_json=runtime._canonical_json(permission),
                permission_snapshot_sha256=runtime._digest(permission),
                stop_requested=False,
                lease_generation=1,
                billing_invocation_count=0,
                version=1,
            )
            db.add(state)
            team = EnterpriseAgentTeam(
                assistant_run_id=run.id,
                enterprise_id=company.id,
                conversation_id=room.id,
                group_id=group.id,
                initiator_user_id=user.id,
                source_message_id=message.id,
                source_tool_call_id=f"call-{suffix}-{index}",
                title="任务",
                objective="测试",
                status="queued",
                payer_owner_type="enterprise",
                payer_owner_id=company.id,
                trace_id=f"trace-{suffix}-{index}",
                request_key_prefix=f"prefix-{suffix}-{index}",
                parent_lease_generation=1,
                lease_generation=0,
                plan_sha256=runtime._digest(plan),
                plan_snapshot_json=runtime._canonical_json(plan),
                permission_snapshot_sha256=runtime._digest(permission),
                permission_snapshot_json=runtime._canonical_json(permission),
                result_json="{}",
                error_json="{}",
            )
            db.add(team)
            db.flush()
            member = EnterpriseAgentTeamMember(
                team_id=team.id,
                member_key="worker",
                display_name="工作 Agent",
                address="agent:worker",
                kind="registered",
                role="worker",
                snapshot_sha256=runtime._digest(capability),
                capabilities_json=runtime._canonical_json(capability),
                status="created",
            )
            db.add(member)
            db.flush()
            task = EnterpriseAgentTeamTask(
                team_id=team.id,
                member_id=member.id,
                task_key="work",
                title="工作",
                instructions="只执行模型",
                dependency_keys_json="[]",
                input_json="{}",
                status="queued",
                priority=0,
                attempt_count=0,
                max_attempts=3,
                lease_generation=0,
                result_json="{}",
                artifacts_json="{}",
                errors_json="{}",
            )
            db.add(task)
            db.flush()
            tasks.append(int(task.id))
            teams.append(int(team.id))
        return int(company.id), int(group.id), int(owner.id), [int(u.id) for u in users], tasks, teams


def _ready(monkeypatch):
    monkeypatch.setattr(parent_runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    monkeypatch.setattr(parent_runtime, "_live_role_snapshot", lambda *_a, **_kw: {})
    monkeypatch.setattr(assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(assistant_service, "_require_active_limit", lambda *_a, **_kw: None)


def test_two_group_v1_teams_do_not_upgrade_shared_group_lock(fresh_chain, monkeypatch):
    """Group S locks cannot both upgrade to X on the later v1 attribution read."""
    _ready(monkeypatch)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, _users, tasks, teams = _seed(factory)
    barrier, thread_ids, observed = Barrier(2, timeout=15), set(), set()

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        tid, sql = get_ident(), statement.upper()
        if (
            tid in thread_ids
            and tid not in observed
            and "FROM ENTERPRISE_GROUP " in sql
            and ("FOR SHARE" in sql or "LOCK IN SHARE MODE" in sql)
        ):
            observed.add(tid)
            barrier.wait()

    def worker(task_id):
        thread_ids.add(get_ident())
        with factory.begin() as db:
            context = runtime._lock_live_context(db, enterprise_id=company, task_id=task_id, lock_company=False)
            return int(context["team"].id)

    event.listen(fresh_chain, "after_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, task) for task in tasks]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=25))
                except OperationalError as exc:
                    outcomes.append(("mysql", exc.orig.args[0]))
        assert outcomes == teams
    finally:
        event.remove(fresh_chain, "after_cursor_execute", observe)


@pytest.mark.parametrize("change", ["remove_member", "deactivate_group"])
def test_locked_group_v1_team_serializes_later_revocation(fresh_chain, monkeypatch, change):
    """A shared group read still fences real member removal and group status updates."""
    _ready(monkeypatch)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group_id, owner, users, tasks, _teams = _seed(factory)
    held, release, started, finished = Event(), Event(), Event(), Event()
    writer_ids = []

    def read_authority():
        with factory.begin() as db:
            assert (
                runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)[
                    "run"
                ].billing_group_id
                == group_id
            )
            held.set()
            assert release.wait(15)

    def revoke():
        assert held.wait(15)
        try:
            with factory.begin() as db:
                writer_ids.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
                started.set()
                if change == "remove_member":
                    org.remove_group_member(db, company, group_id, owner, users[0])
                else:
                    group = db.query(EnterpriseGroup).filter_by(id=group_id).with_for_update().one()
                    group.status = "inactive"
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(read_authority)
        try:
            assert held.wait(15)
            writer = pool.submit(revoke)
            assert started.wait(15)
            deadline, waiting_table = monotonic() + 8, None
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting_table = observer.execute(
                        text(
                            "SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                            "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                            "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                        ),
                        {"cid": writer_ids[0]},
                    ).scalar_one_or_none()
                if waiting_table:
                    break
                sleep(0.05)
            assert waiting_table in {"enterprise", "enterprise_group", "enterprise_group_member", "enterprise_member"}
            assert not finished.is_set()
        finally:
            release.set()
        reader.result(timeout=15)
        writer.result(timeout=15)
    with factory() as db:
        with pytest.raises((ForbiddenError, NotFoundError)):
            runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)


@pytest.mark.parametrize("change", ["remove_member", "deactivate_group"])
def test_group_v1_team_rechecks_old_snapshot_after_revocation(fresh_chain, monkeypatch, change):
    _ready(monkeypatch)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group_id, owner, users, tasks, _teams = _seed(factory)
    with factory() as stale:
        assert stale.query(EnterpriseGroup).filter_by(id=group_id, status="active").one()
        with factory.begin() as writer:
            if change == "remove_member":
                org.remove_group_member(writer, company, group_id, owner, users[0])
            else:
                group = writer.query(EnterpriseGroup).filter_by(id=group_id).with_for_update().one()
                group.status = "inactive"
        with pytest.raises((ForbiddenError, NotFoundError)):
            runtime._lock_live_context(stale, enterprise_id=company, task_id=tasks[0], lock_company=False)


def test_team_attribution_rejects_cross_enterprise_and_closed_v1(fresh_chain, monkeypatch):
    _ready(monkeypatch)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, _users, tasks, _teams = _seed(factory)
    other_company, _group2, _owner2, _users2, other_tasks, _teams2 = _seed(factory)
    with factory() as db:
        with pytest.raises(NotFoundError):
            runtime._lock_live_context(db, enterprise_id=company, task_id=other_tasks[0], lock_company=False)
        with pytest.raises(NotFoundError):
            runtime._lock_live_context(db, enterprise_id=other_company, task_id=tasks[0], lock_company=False)
    monkeypatch.setattr(parent_runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
    with factory() as db:
        with pytest.raises(billing_group.BillingAttributionUnavailable):
            runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)


class CompanyRowSerialization(Exception):
    """Two independent team transactions contend on the company row."""



def _install_live_snapshots(factory, company, teams, *, legacy):
    """Build real role snapshots; keep the tests independent of a model provider."""
    with factory.begin() as db:
        for index, team_id in enumerate(teams):
            team = db.get(EnterpriseAgentTeam, team_id)
            run = db.get(EnterpriseAssistantRun, team.assistant_run_id)
            if legacy:
                run.billing_attribution_version = 0
                run.billing_group_id = None
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
            room = db.get(EnterpriseConversation, run.conversation_id)
            message = db.get(EnterpriseChatMessage, run.source_message_id)
            if index == 1:
                # Avoid making the later message an independent superseding command.
                message.content = "普通消息"
            snapshot = parent_runtime._role_snapshot(
                db,
                enterprise_id=company,
                room=room,
                messages=[message],
                initiator_id=run.initiator_user_id,
            )
            state.permission_snapshot_json = parent_runtime._json(snapshot)
            state.permission_snapshot_sha256 = parent_runtime._digest(snapshot)
            team.permission_snapshot_json = parent_runtime._json(snapshot)
            team.permission_snapshot_sha256 = parent_runtime._digest(snapshot)


@pytest.mark.xfail(
    raises=CompanyRowSerialization,
    strict=True,
    reason="真实角色快照会在 lock_company=False 路径持企业 X 锁，使同企业团队串行",
)
def test_live_team_authorization_does_not_serialize_independent_members(fresh_chain, monkeypatch):
    real_live_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, "_live_role_snapshot", real_live_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, _users, tasks, teams = _seed(factory)
    _install_live_snapshots(factory, company, teams, legacy=True)
    held, release, started = Event(), Event(), Event()
    writer_ids = []

    def first():
        with factory.begin() as db:
            runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[0], lock_company=False)
            held.set()
            assert release.wait(15)

    def second():
        assert held.wait(15)
        with factory.begin() as db:
            writer_ids.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            started.set()
            runtime._lock_live_context(db, enterprise_id=company, task_id=tasks[1], lock_company=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        try:
            assert held.wait(15)
            b = pool.submit(second)
            assert started.wait(15)
            deadline, waiting_table = monotonic() + 5, None
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting_table = observer.execute(
                        text(
                            "SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                            "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                            "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                        ),
                        {"cid": writer_ids[0]},
                    ).scalar_one_or_none()
                if waiting_table or b.done():
                    break
                sleep(0.05)
            if waiting_table == "enterprise":
                raise CompanyRowSerialization("不同成员团队等待 enterprise 行")
            assert waiting_table is None and b.done()
        finally:
            release.set()
        a.result(timeout=15)
        b.result(timeout=15)
