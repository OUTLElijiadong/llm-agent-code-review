"""正式审查原生线程及重启接续必须具备持久、可跨线程轮询的执行边界。"""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.multi_agent import GENERAL_AGENT
from app.core.config import settings
from app.core.database import Base
from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
from app.models.billing_ledger import BillingLimit, BillingReservation
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseMember
from app.models.enterprise_project import EnterpriseProject
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services import agent_scheduler_runtime as runtime
from app.services import review_service
from app.services.ai_usage_context import usage_context
from app.services.billing_ledger_service import get_or_create_wallet, grant
from app.services.review_input_service import freeze_task_inputs


@pytest.fixture
def rows(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker-scope.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(review_service, "SessionLocal", sessions)
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_a, **_k: True)
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *_a, **_k: [])
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **_k: SimpleNamespace(model="isolated"))
    monkeypatch.setattr(review_service, "_enabled_review_profiles", lambda _db, profiles: profiles)
    monkeypatch.setattr("app.services.declarative_agent_runtime.DeclarativeReviewAgentFactory.snapshot_profiles",
                        lambda *_a, **_k: ())
    monkeypatch.setattr("app.services.experience_service.retrieve", lambda *_a, **_k: [])
    monkeypatch.setattr("app.services.personalization_service.build_review_context", lambda *_a, **_k: "")
    monkeypatch.setattr("app.services.agent_model_service.resolve_subagent_config", lambda *_a, **_k: None)
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *_a, **_k: None)
    with sessions() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        user = User(username="worker-scope", password="unused", role="admin", status=1)
        db.add(user)
        db.flush()
        project = Project(user_id=user.id, project_name="worker scope", status="active", language="python")
        db.add(project)
        db.flush()
        source = CodeFile(project_id=project.id, file_name="one.py", file_path="one.py", content="print(1)\n",
                          language="python", status="active", is_binary=0, version_no=1)
        team = AgentTeam(user_id=user.id, surface="user", session_key="worker-scope", title="正式审查",
                         objective="完成代码审查", status="running", trace_id="worker-scope")
        db.add_all([source, team])
        db.flush()
        db.add(CodeVersion(file_id=source.id, version_no=1, content=source.content,
                           create_time=datetime.now(timezone.utc)))
        member = AgentTeamMember(team_id=team.id, member_key="review", display_name="审查编排",
                                 address="agent:review_orchestrator", kind="runtime", role="worker", status="running")
        db.add(member)
        db.flush()
        team_task = AgentTeamTask(team_id=team.id, member_id=member.id, task_key="review", title="正式审查",
                                  instructions="审查项目", status="running", lease_token="team-token",
                                  lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                                  input_json=json.dumps({"operation": "run_review", "project_id": project.id,
                                                         "review_type": "full"}))
        db.add(team_task)
        db.flush()
        review = ReviewTask(user_id=user.id, project_id=project.id, status="running", review_type="full",
                            total_files=1, processed_files=0, execution_token="review-token", rules_snapshot=[],
                            agent_team_id=team.id, agent_team_task_id=team_task.id,
                            start_time=datetime.now(timezone.utc))
        db.add(review)
        db.flush()
        freeze_task_inputs(db, review.id, [source])
        db.commit()
        yield SimpleNamespace(sessions=sessions, db=db, user=user, project=project, source=source,
                              team=team, team_task=team_task, review=review, engine=engine)
    engine.dispose()


def test_native_review_thread_preserves_callers_execution_scope(rows, monkeypatch):
    observations = []
    threads = []
    native_thread = threading.Thread
    monkeypatch.setattr(review_service, "_run_review_task",
                        lambda *_a: observations.append(runtime.has_agent_execution_scope()))

    def spawn(**kwargs):
        thread = native_thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(review_service.threading, "Thread", spawn)
    with runtime.agent_execution_scope(lambda: None), usage_context(rows.user.id, {
        "agent_team_id": rows.team.id, "agent_team_task_id": rows.team_task.id,
    }, db=rows.db):
        review_service.start(rows.db, rows.user, ReviewStartIn(
            project_id=rows.project.id, file_ids=[rows.source.id], review_type="full",
        ))
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert observations == [True]


@pytest.mark.parametrize("entry", ["worker", "recovery"])
def test_worker_cancellation_cancels_inflight_model_and_prevents_followup(rows, monkeypatch, entry):
    observed = []
    calls = []
    cleaned = []

    def execute(*_args, **_kwargs):
        observed.append(runtime.has_agent_execution_scope())

        async def provider():
            calls.append("first")
            with rows.sessions() as cancellation:
                team = cancellation.get(AgentTeam, rows.team.id)
                review = cancellation.get(ReviewTask, rows.review.id)
                team.status = "cancelled"
                review.status = "cancelled"
                review.execution_token = "user-stopped"
                cancellation.commit()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

        async def request():
            try:
                await asyncio.wait_for(runtime.await_agent_coroutine(provider()), timeout=1.5)
                calls.append("followup")
            except runtime.AgentExecutionCancelled:
                observed.append("cancelled")
                raise
            except asyncio.TimeoutError:
                observed.append("unguarded-timeout")

        asyncio.run(request())

    monkeypatch.setattr(review_service, "_execute_review", execute)
    if entry == "worker":
        review_service._run_review_task(rows.review.id, rows.user.id, "review-token")
    else:
        review_service._resume_interrupted_task(rows.review.id, rows.user.id, "review-token")
    assert observed == [True, "cancelled"]
    assert calls == ["first"] and cleaned == [True]
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)
    review_service._resume_interrupted_task(rows.review.id, rows.user.id, "user-stopped")
    dispatch.assert_not_called()
    with rows.sessions() as db:
        assert db.get(ReviewTask, rows.review.id).status == "cancelled"


@pytest.mark.parametrize("changed", [
    "cancelled", "expired", "foreign-owner", "wrong-project", "lease-expired", "deadline-expired",
    "wrong-member", "wrong-operation", "partial-binding", "wrong-review-owner",
])
def test_recovery_rejects_invalid_team_before_claim_or_dispatch(rows, monkeypatch, changed):
    with rows.sessions() as db:
        if changed in {"cancelled", "expired"}:
            db.get(AgentTeam, rows.team.id).status = changed
        elif changed == "foreign-owner":
            db.get(AgentTeam, rows.team.id).user_id = rows.user.id + 100
        elif changed == "wrong-project":
            task = db.get(AgentTeamTask, rows.team_task.id)
            task.input_json = json.dumps({"operation": "run_review", "project_id": rows.project.id + 100})
        elif changed == "lease-expired":
            task = db.get(AgentTeamTask, rows.team_task.id)
            task.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif changed == "deadline-expired":
            db.get(AgentTeam, rows.team.id).deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif changed == "wrong-member":
            db.get(AgentTeamMember, rows.team_task.member_id).address = "agent:code_reviewer"
        elif changed == "wrong-operation":
            db.get(AgentTeamTask, rows.team_task.id).input_json = json.dumps({
                "operation": "run_roundtable", "project_id": rows.project.id,
            })
        elif changed == "partial-binding":
            db.get(ReviewTask, rows.review.id).agent_team_task_id = None
        else:
            db.get(ReviewTask, rows.review.id).user_id = rows.user.id + 100
        db.commit()
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)
    review_service._resume_interrupted_task(rows.review.id, rows.user.id, "review-token")
    dispatch.assert_not_called()
    with rows.sessions() as db:
        stored = db.get(ReviewTask, rows.review.id)
        assert stored.execution_token == "review-token" and stored.status == "running"


@pytest.mark.parametrize("changed", ["lease", "member", "input"])
def test_worker_does_not_adopt_replaced_team_authorization(rows, monkeypatch, changed):
    calls = []

    def execute(*_args, authorization_check, **_kwargs):
        calls.append("first")
        with rows.sessions() as db:
            task = db.get(AgentTeamTask, rows.team_task.id)
            if changed == "lease":
                task.lease_token = "next-owner"
            elif changed == "member":
                member = AgentTeamMember(
                    team_id=rows.team.id, member_key="replacement", display_name="接管者",
                    address="agent:review_orchestrator", kind="runtime", role="worker", status="running",
                )
                db.add(member)
                db.flush()
                task.member_id = member.id
            else:
                task.input_json = json.dumps({
                    "operation": "run_review", "project_id": rows.project.id, "review_type": "full",
                    "file_ids": [rows.source.id],
                })
            db.commit()
        authorization_check()
        calls.append("followup")

    monkeypatch.setattr(review_service, "_execute_review", execute)
    review_service._run_review_task(rows.review.id, rows.user.id, "review-token")
    assert calls == ["first"]


def test_guard_detaches_from_callers_connection_transaction(rows, monkeypatch):
    with rows.engine.connect() as connection, connection.begin():
        sessions = sessionmaker(bind=connection)
        monkeypatch.setattr(review_service, "SessionLocal", sessions)
        review_service._assert_review_execution_authorized(rows.review.id, rows.user.id, "review-token")
        with rows.sessions() as db:
            db.get(AgentTeam, rows.team.id).status = "cancelled"
            db.commit()
        with pytest.raises(review_service.TaskSupersededError):
            review_service._assert_review_execution_authorized(rows.review.id, rows.user.id, "review-token")
        assert connection.in_transaction()


def test_review_start_persists_authenticated_personal_payer(rows, monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    with rows.sessions.begin() as db:
        get_or_create_wallet(db, "user", rows.user.id)

    dispatched = []

    class DeferredThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            dispatched.append(self.kwargs)

    monkeypatch.setattr(review_service.threading, "Thread", DeferredThread)
    result = review_service.start(rows.db, rows.user, ReviewStartIn(
        project_id=rows.project.id, file_ids=[rows.source.id], review_type="full",
    ))

    with rows.sessions() as db:
        task = db.get(ReviewTask, result.id)
        assert task.coverage["_billing_subject"] == {
            "owner_type": "user",
            "owner_id": rows.user.id,
            "enterprise_id": None,
            "group_id": None,
            "binding_status": "unbound",
        }
    assert len(dispatched) == 1


def test_enterprise_review_without_company_wallet_fails_closed_despite_personal_wallet(rows, monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    with rows.sessions.begin() as db:
        get_or_create_wallet(db, "user", rows.user.id)
        company = Enterprise(name="无企业钱包", owner_id=rows.user.id, seat_limit=4,
                             paid_order_id=f"review-no-wallet-{rows.user.id}", status="active")
        db.add(company)
        db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name="小组", status="active")
        db.add_all([
            EnterpriseMember(enterprise_id=company.id, user_id=rows.user.id, role="owner", status="active"),
            group,
        ])
        db.flush()
        db.add(EnterpriseProject(enterprise_id=company.id, project_id=rows.project.id,
                                 group_id=group.id, status="active"))

    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_status",
        lambda *_args, **_kwargs: "active",
    )
    with rows.sessions() as db:
        with pytest.raises(review_service.PaymentRequiredError, match="计费账户尚未开通"):
            review_service._resolve_review_billing_context(
                db, db.get(User, rows.user.id), db.get(Project, rows.project.id),
            )


def test_review_worker_rejects_changed_payer_snapshot(rows, monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    with rows.sessions.begin() as db:
        get_or_create_wallet(db, "user", rows.user.id)

    wrong_subject = {
        "owner_type": "enterprise", "owner_id": rows.user.id + 1,
        "enterprise_id": rows.user.id + 1, "group_id": 9, "binding_status": "active",
    }
    with rows.sessions() as db:
        with pytest.raises(review_service.ReviewAuthorizationRevokedError, match="计费主体已变化"):
            review_service._resolve_review_billing_context(
                db, db.get(User, rows.user.id), db.get(Project, rows.project.id),
                expected_subject=wrong_subject,
            )


@pytest.mark.parametrize("payer", ["personal", "enterprise"])
def test_review_worker_scope_reaches_collaboration_child_and_charges_bound_wallet(rows, monkeypatch, payer):
    from app.services import billing_runtime_gate

    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    enterprise_id = None
    group_id = None
    with rows.sessions.begin() as db:
        personal_wallet = get_or_create_wallet(db, "user", rows.user.id)
        grant(db, wallet_id=personal_wallet.id, grant_key=f"review-personal-{payer}",
              kind="purchase", amount_milli=1_000_000)
        if payer == "personal":
            expected_subject = {
                "owner_type": "user", "owner_id": rows.user.id,
                "enterprise_id": None, "group_id": None, "binding_status": "unbound",
            }
        else:
            company = Enterprise(name="审查计费企业", owner_id=rows.user.id, seat_limit=4,
                                 paid_order_id=f"review-paid-{rows.user.id}", status="active")
            db.add(company)
            db.flush()
            enterprise_id = company.id
            group = EnterpriseGroup(enterprise_id=enterprise_id, name="审查小组", status="active")
            db.add_all([
                EnterpriseMember(enterprise_id=enterprise_id, user_id=rows.user.id,
                                 role="owner", status="active"),
                group,
            ])
            db.flush()
            group_id = group.id
            db.add(EnterpriseProject(enterprise_id=enterprise_id, project_id=rows.project.id,
                                     group_id=group_id, status="active"))
            enterprise_wallet = get_or_create_wallet(db, "enterprise", enterprise_id)
            grant(db, wallet_id=enterprise_wallet.id, grant_key=f"review-enterprise-{payer}",
                  kind="purchase", amount_milli=1_000_000)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            db.add(BillingLimit(
                limit_key=f"review-limit-{payer}", enterprise_id=enterprise_id, category="normal",
                group_id=group_id, user_id=rows.user.id,
                period_start=now - timedelta(days=1), period_end=now + timedelta(days=30),
                limit_milli=1_000_000, used_milli=0, reserved_milli=0,
            ))
            expected_subject = {
                "owner_type": "enterprise", "owner_id": enterprise_id,
                "enterprise_id": enterprise_id, "group_id": group_id, "binding_status": "active",
            }
        task = db.get(ReviewTask, rows.review.id)
        task.coverage = {**(task.coverage or {}), "_billing_subject": expected_subject}

    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_status",
        lambda *_args, **_kwargs: "active",
    )
    observed = []

    def metered_child_call(*_args, **_kwargs):
        ticket = billing_runtime_gate.begin_attempt(
            model_name="deepseek-flash",
            payload={"model": "deepseek-flash", "input": "审查源码", "max_output_tokens": 4},
                provider_name="deepseek",
                base_url=settings.deepseek_base_url,
                credential_source="system",
        )
        observed.append({
            "personal_wallet_id": ticket["scope"]["personal_wallet_id"],
            "enterprise_wallet_id": ticket["scope"]["enterprise_wallet_id"],
            "enterprise_id": ticket["scope"]["enterprise_id"],
            "group_id": ticket["scope"]["group_id"],
            "allow_personal": ticket["scope"]["allow_personal"],
        })
        billing_runtime_gate.finish_attempt(ticket, {"input_tokens": 2, "output_tokens": 1})
        return '{"summary":"未发现问题","issues":[]}', {}

    monkeypatch.setattr(review_service, "_call_single_agent", metered_child_call)

    def execute(db, shared_agent, api_config, task, user, files, rules, profiles, experience_section,
                *, authorization_check=None, **_kwargs):
        findings = review_service._review_chunk_collaborative(
            db, shared_agent, api_config, task, rows.source, rules, user,
            (GENERAL_AGENT,), 0,
            SimpleNamespace(text=rows.source.content, start_line=1, context=""),
            experience_section=experience_section, authorization_check=authorization_check,
        )
        assert findings == []

    monkeypatch.setattr(review_service, "_execute_review", execute)
    review_service._run_review_task(rows.review.id, rows.user.id, "review-token")

    assert len(observed) == 1
    assert observed[0]["allow_personal"] is False
    with rows.sessions() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "settled"
        assert reservation.user_id == rows.user.id
        if payer == "personal":
            assert observed[0] == {
                "personal_wallet_id": personal_wallet.id,
                "enterprise_wallet_id": None,
                "enterprise_id": None,
                "group_id": None,
                "allow_personal": False,
            }
            assert reservation.personal_wallet_id == personal_wallet.id
            assert reservation.enterprise_wallet_id is None
        else:
            assert observed[0]["personal_wallet_id"] is None
            assert observed[0]["enterprise_wallet_id"] is not None
            assert observed[0]["enterprise_id"] == enterprise_id
            assert observed[0]["group_id"] == group_id
            assert reservation.personal_wallet_id is None
            assert reservation.enterprise_wallet_id == observed[0]["enterprise_wallet_id"]
            assert reservation.enterprise_id == enterprise_id
            assert reservation.group_id == group_id


def test_worker_rejects_foreign_review_without_mutating_it(rows, monkeypatch):
    with rows.sessions() as db:
        db.get(ReviewTask, rows.review.id).user_id = rows.user.id + 100
        db.commit()
    execute = Mock()
    monkeypatch.setattr(review_service, "_execute_review", execute)
    review_service._run_review_task(rows.review.id, rows.user.id, "review-token")
    execute.assert_not_called()
    with rows.sessions() as db:
        assert db.get(ReviewTask, rows.review.id).status == "running"


def test_native_worker_cannot_adopt_lease_replaced_before_thread_starts(rows, monkeypatch):
    from app.services import agent_team_service

    release = threading.Event()
    threads = []
    native_thread = threading.Thread
    execute = Mock()
    monkeypatch.setattr(review_service, "_execute_review", execute)

    def spawn(**kwargs):
        target = kwargs.pop("target")
        args = kwargs.pop("args")

        def deferred():
            assert release.wait(timeout=3)
            target(*args)

        thread = native_thread(target=deferred, **kwargs)
        threads.append(thread)
        return thread

    def inherited_guard():
        with rows.sessions() as db:
            agent_team_service.require_active_task_lease(
                db, team_id=rows.team.id, task_id=rows.team_task.id,
                owner_user_id=rows.user.id, lease_token="team-token",
            )

    monkeypatch.setattr(review_service.threading, "Thread", spawn)
    with runtime.agent_execution_scope(inherited_guard), usage_context(rows.user.id, {
        "agent_team_id": rows.team.id, "agent_team_task_id": rows.team_task.id,
    }, db=rows.db):
        task = review_service.start(rows.db, rows.user, ReviewStartIn(
            project_id=rows.project.id, file_ids=[rows.source.id], review_type="full",
        ))
    with rows.sessions() as db:
        db.get(AgentTeamTask, rows.team_task.id).lease_token = "next-owner"
        db.commit()
    release.set()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    execute.assert_not_called()
    with rows.sessions() as db:
        assert db.get(ReviewTask, task.id).status == "running"


def test_recovery_cannot_adopt_lease_replaced_between_cas_and_dispatch(rows, monkeypatch):
    commit = review_service._safe_commit

    def replace_lease(db, task):
        commit(db, task)
        with rows.sessions() as writer:
            writer.get(AgentTeamTask, rows.team_task.id).lease_token = "next-owner"
            writer.commit()

    monkeypatch.setattr(review_service, "_safe_commit", replace_lease)
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)
    review_service._resume_interrupted_task(rows.review.id, rows.user.id, "review-token")
    dispatch.assert_not_called()
