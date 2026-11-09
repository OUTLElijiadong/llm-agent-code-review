from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.exceptions import ConflictError, ForbiddenError, PaymentRequiredError, ValidationError
from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingOrder,
    BillingPaymentEvent,
    BillingReservation,
    BillingSubscription,
    BillingSubscriptionPeriod,
)
from app.models.enterprise_agent_team import (
    EnterpriseAgentTeam,
    EnterpriseAgentTeamApproval,
    EnterpriseAgentTeamEvent,
    EnterpriseAgentTeamMember,
    EnterpriseAgentTeamTask,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseMember,
)
from app.models.enterprise_project import EnterpriseProject
from app.models.enterprise_security_artifact import EnterpriseSecurityArtifact
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import enterprise_agent_team_dispatcher
from app.services import enterprise_agent_team_runtime_service as runtime

_PLAN_BUDGET = {
    "allowed_models": ["deepseek-flash"],
    "max_calls_per_task": 3,
    "max_input_tokens_per_call": 200,
    "max_output_tokens_per_call": 200,
    "max_team_credits_milli": 10_000,
}


@pytest.fixture
def runtime_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.services.enterprise_assistant_service._require_subscription",
        lambda db, enterprise_id: None,
    )
    monkeypatch.setattr(
        "app.services.enterprise_assistant_service._require_active_limit",
        lambda db, **kwargs: None,
    )
    from app.services import enterprise_assistant_runtime_service

    monkeypatch.setattr(
        enterprise_assistant_runtime_service,
        "_live_role_snapshot",
        lambda db, state, **kwargs: {},
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    plan = {**_PLAN_BUDGET, "risk_level": "low", "requires_approval": False}
    permission = {"messages": []}
    capabilities = {"execution_mode": "model_only", "tools": [], "external_effects": False}
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        db.add_all(
            [
                User(id=1, username="owner", password="x", email="owner@test", role="user", status=1),
                User(id=2, username="member", password="x", email="member@test", role="user", status=1),
                Enterprise(
                    id=10,
                    name="甲公司",
                    owner_id=1,
                    seat_limit=5,
                    status="active",
                    billing_suspended=False,
                    paid_order_id="paid-10",
                ),
                EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
                EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
                EnterpriseConversation(
                    id=100, enterprise_id=10, kind="company", conversation_key="company", group_id=None, created_by=1
                ),
                EnterpriseChatMessage(
                    id=101,
                    enterprise_id=10,
                    conversation_id=100,
                    sender_id=2,
                    sender_type="human",
                    client_message_id="source-101",
                    content="@小菱 执行",
                ),
                EnterpriseAssistantRun(
                    id=200,
                    public_id="run-200",
                    request_key="request-200",
                    enterprise_id=10,
                    conversation_id=100,
                    group_id=None,
                    initiator_user_id=2,
                    source_message_id=101,
                    status="running",
                    attempt_count=1,
                    prompt_sha256="p" * 64,
                ),
                EnterpriseAssistantRuntimeState(
                    id=300,
                    assistant_run_id=200,
                    enterprise_id=10,
                    conversation_id=100,
                    group_id=None,
                    initiator_user_id=2,
                    source_message_id=101,
                    status="running",
                    checkpoint_json="{}",
                    permission_snapshot_json=runtime._canonical_json(permission),
                    permission_snapshot_sha256=runtime._digest(permission),
                    stop_requested=False,
                    lease_generation=1,
                    lease_expires_at=now + timedelta(hours=1),
                    billing_invocation_count=1,
                    version=1,
                ),
            ]
        )
        db.add(
            EnterpriseAgentTeam(
                id=400,
                assistant_run_id=200,
                enterprise_id=10,
                conversation_id=100,
                group_id=None,
                initiator_user_id=2,
                source_message_id=101,
                source_tool_call_id="call-team-400",
                title="只读任务",
                objective="使用受控模型处理",
                status="queued",
                payer_owner_type="enterprise",
                payer_owner_id=10,
                trace_id="trace-team-400",
                request_key_prefix="request-team-400",
                parent_lease_generation=1,
                lease_generation=0,
                plan_sha256=runtime._digest(plan),
                plan_snapshot_json=runtime._canonical_json(plan),
                permission_snapshot_sha256=runtime._digest(permission),
                permission_snapshot_json=runtime._canonical_json(permission),
                result_json="{}",
                error_json="{}",
            )
        )
        db.add(
            EnterpriseAgentTeamMember(
                id=500,
                team_id=400,
                member_key="worker",
                display_name="工作 Agent",
                address="agent:worker",
                kind="registered",
                role="worker",
                snapshot_sha256=runtime._digest(capabilities),
                capabilities_json=runtime._canonical_json(capabilities),
                status="created",
            )
        )
        db.add(
            EnterpriseAgentTeamTask(
                id=600,
                team_id=400,
                member_id=500,
                task_key="work",
                title="执行任务",
                instructions="仅使用平台计量模型",
                dependency_keys_json="[]",
                input_json="{}",
                status="queued",
                priority=0,
                attempt_count=0,
                max_attempts=3,
                lease_generation=0,
                result_json="{}",
                artifacts_json="[]",
                errors_json="[]",
            )
        )
    yield factory, now
    engine.dispose()


def _seed_verified_enterprise_period(db, *, started_at, ends_at, order_no):
    db.add(
        BillingSubscription(
            id=800,
            owner_type="enterprise",
            owner_id=10,
            product="enterprise",
            anchor_at=started_at,
            last_end_at=ends_at,
            next_offset=1,
        )
    )
    db.add(
        BillingOrder(
            order_no=order_no,
            user_id=1,
            enterprise_id=10,
            product="enterprise_create",
            amount_fen=34_500,
            quantity=5,
            channel="alipay",
            status="paid",
            paid_at=started_at,
        )
    )
    db.add(
        BillingPaymentEvent(
            channel="alipay",
            trade_id=f"trade-{order_no}",
            order_no=order_no,
            amount_fen=34_500,
            verified_at=started_at,
        )
    )
    db.add(
        BillingSubscriptionPeriod(
            subscription_id=800,
            order_no=order_no,
            starts_at=started_at,
            ends_at=ends_at,
            credit_milli=25_000_000,
            seats=5,
            granted_at=started_at,
        )
    )


def _bind_runtime_team_to_project(factory, *, project_id=701, group_id=50):
    import json

    from app.services import enterprise_agent_team_service

    with factory.begin() as db:
        role = db.query(Role).filter_by(code="user").first()
        if role is None:
            role = Role(code="user", name="普通用户", status="active")
            db.add(role)
            db.flush()
        permission = db.query(Permission).filter_by(code="security:scan").first()
        if permission is None:
            permission = Permission(code="security:scan", name="安全扫描", module="security")
            db.add(permission)
            db.flush()
        if db.query(UserRole).filter_by(user_id=2, role_id=role.id).first() is None:
            db.add(UserRole(user_id=2, role_id=role.id))
        if db.query(RolePermission).filter_by(role_id=role.id, permission_id=permission.id).first() is None:
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        db.add(EnterpriseGroup(id=group_id, enterprise_id=10, name=f"项目组{group_id}", status="active"))
        db.add(Project(id=project_id, user_id=2, project_name=f"项目{project_id}", status="active"))
        db.flush()
        db.add_all(
            [
                EnterpriseGroupMember(
                    enterprise_id=10,
                    group_id=group_id,
                    user_id=2,
                    role="member",
                    status="active",
                ),
                EnterpriseProject(
                    enterprise_id=10,
                    project_id=project_id,
                    group_id=group_id,
                    status="active",
                ),
            ]
        )
        team = db.get(EnterpriseAgentTeam, 400)
        team.project_id = project_id
        plan = json.loads(team.plan_snapshot_json)
        plan["project_id"] = project_id
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        _contract, capabilities = enterprise_agent_team_service._contract_snapshot("security_sentinel")
        member = db.get(EnterpriseAgentTeamMember, 500)
        member.address = "agent:security_sentinel"
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)


def _security_artifact_payload(*, coverage_status="complete", termination_reason="", provider_calls=1):
    return {
        "audit_run_id": "a" * 32,
        "source_sha256": "b" * 64,
        "coverage_status": coverage_status,
        "termination_reason": termination_reason,
        "summary": {"summary": "受控报告摘要", "finding_count": 1},
        "report": {
            "findings": [{"title": "示例发现", "severity": "低"}],
            "compliance": {"provider_request_count": provider_calls},
            "complete": coverage_status == "complete",
        },
    }


def _settle_runtime_team_attempt(factory, *, claim):
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, task, int(claim["attempt"]), 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                user_id=2,
                usage_kind="member",
                requested_milli=1000,
                actual_milli=100,
                input_tokens=10,
                output_tokens=10,
                status="settled",
                usage_state="reported",
                enterprise_wallet_id=900,
            )
        )


def test_claim_lease_excludes_second_worker_and_fences_expired_attempt(runtime_factory):
    factory, now = runtime_factory
    first = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    assert first["status"] == "running"
    assert first["attempt"] == 1
    assert first["lease_generation"] == 1
    assert runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now) is None

    with factory.begin() as db:
        task = db.get(EnterpriseAgentTeamTask, 600)
        task.lease_expires_at = now - timedelta(seconds=1)
    second = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now + timedelta(seconds=1))
    assert second["attempt"] == 2
    assert second["lease_generation"] == 2
    assert second["lease_token"] != first["lease_token"]
    with pytest.raises(ConflictError):
        runtime.renew_task_lease(
            factory,
            enterprise_id=10,
            task_id=600,
            lease_token=first["lease_token"],
            lease_generation=first["lease_generation"],
            now=now + timedelta(seconds=1),
        )


def test_expired_attempt_with_any_reservation_is_frozen_for_reconciliation(runtime_factory):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        task.lease_expires_at = now - timedelta(seconds=1)
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, task, 1, 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                group_id=None,
                user_id=2,
                usage_kind="member",
                requested_milli=1000,
                status="unknown",
                usage_state="unknown",
                enterprise_wallet_id=900,
            )
        )
    result = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now + timedelta(seconds=1))
    assert result["status"] == "awaiting_reconciliation"
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "awaiting_reconciliation"
        assert db.get(EnterpriseAgentTeam, 400).status == "awaiting_reconciliation"
    assert claim["lease_generation"] == 1


def test_expired_subscription_keeps_queued_task_unclaimed(runtime_factory, monkeypatch):
    factory, now = runtime_factory

    def expired_subscription(_db, _enterprise_id):
        raise PaymentRequiredError("企业订阅已到期")

    monkeypatch.setattr("app.services.enterprise_assistant_service._require_subscription", expired_subscription)

    with pytest.raises(PaymentRequiredError, match="订阅已到期"):
        runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)

    with factory() as db:
        task = db.get(EnterpriseAgentTeamTask, 600)
        assert task.status == "queued"
        assert task.attempt_count == 0
        assert db.query(BillingReservation).count() == 0


def test_expired_subscription_still_allows_billing_reconciliation(runtime_factory, monkeypatch):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        task.lease_expires_at = now - timedelta(seconds=1)
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, task, 1, 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                group_id=None,
                user_id=2,
                usage_kind="member",
                requested_milli=1_000,
                status="unknown",
                usage_state="unknown",
                enterprise_wallet_id=900,
            )
        )

    def expired_subscription(_db, _enterprise_id):
        raise PaymentRequiredError("企业订阅已到期")

    monkeypatch.setattr("app.services.enterprise_assistant_service._require_subscription", expired_subscription)

    result = runtime.claim_next_task(factory, enterprise_id=10, now=now + timedelta(seconds=1))

    assert result is None
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "awaiting_reconciliation"
        assert db.get(EnterpriseAgentTeam, 400).status == "awaiting_reconciliation"
        assert db.query(BillingReservation).one().status == "unknown"
    assert claim["attempt"] == 1


@pytest.mark.parametrize("reservation_status", ["unknown", "reserved"])
def test_deadline_keeps_unresolved_billing_in_reconciliation(runtime_factory, reservation_status):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        team.deadline_at = now - timedelta(seconds=1)
        task.lease_expires_at = now - timedelta(seconds=1)
        db.add(
            EnterpriseAgentTeamTask(
                id=601,
                team_id=400,
                member_id=500,
                task_key="not-started",
                title="截止前未领取",
                instructions="不得在截止后启动",
                dependency_keys_json="[]",
                input_json="{}",
                status="queued",
                attempt_count=0,
                max_attempts=2,
                lease_generation=0,
                result_json="{}",
                artifacts_json="[]",
                errors_json="[]",
            )
        )
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, task, 1, 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                group_id=None,
                user_id=2,
                usage_kind="member",
                requested_milli=1_000,
                status=reservation_status,
                usage_state="unknown" if reservation_status == "unknown" else "pending",
                enterprise_wallet_id=900,
            )
        )

    assert runtime.claim_next_task(factory, enterprise_id=10, now=now) is None
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "awaiting_reconciliation"
        assert db.get(EnterpriseAgentTeamTask, 601).status == "failed"
        assert db.get(EnterpriseAgentTeam, 400).status == "awaiting_reconciliation"
        assert db.query(BillingReservation).one().status == reservation_status
    assert claim["attempt"] == 1


def test_deadline_stops_new_claims_but_does_not_discard_a_live_provider_lease(runtime_factory):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        team.deadline_at = now - timedelta(seconds=1)
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, db.get(EnterpriseAgentTeamTask, 600), 1, 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                group_id=None,
                user_id=2,
                usage_kind="member",
                requested_milli=1_000,
                actual_milli=100,
                input_tokens=30,
                output_tokens=10,
                status="settled",
                usage_state="reported",
                enterprise_wallet_id=900,
            )
        )

    assert runtime.claim_next_task(factory, enterprise_id=10, now=now) is None
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "running"
        assert db.get(EnterpriseAgentTeam, 400).status == "running"

    result = runtime.complete_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        result={"status": "completed", "summary": "截止前已发送的调用完成并已结算"},
        now=now + timedelta(seconds=1),
    )
    assert result["status"] == "completed"
    assert result["team_status"] == "completed"


def test_team_deadline_blocks_new_provider_call_but_preserves_finalization(runtime_factory, monkeypatch):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        db.get(EnterpriseAgentTeam, 400).deadline_at = now - timedelta(seconds=1)
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(
            db,
            wallet_id=int(wallet.id),
            grant_key="team-deadline-provider-call",
            kind="purchase",
            amount_milli=10_000,
        )
    monkeypatch.setattr(runtime, "_now", lambda: now)
    sent = {"count": 0}
    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )

    with pytest.raises(ForbiddenError, match="截止时间"):
        runtime.metered_team_call(
            session_factory=factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            call_index=1,
            model_name="deepseek-flash",
            max_input_tokens=100,
            max_output_tokens=100,
            provider_identity=provider,
            send=lambda: sent.__setitem__("count", sent["count"] + 1) or (
                "should-not-send", {"prompt_tokens": 1, "completion_tokens": 1},
            ),
            now=now,
        )

    assert sent["count"] == 0
    with factory() as db:
        assert db.query(BillingReservation).count() == 0
        assert db.get(EnterpriseAgentTeamTask, 600).status == "running"


def test_team_deadline_final_send_fence_releases_unsent_reservation(runtime_factory, monkeypatch):
    from app.services.billing_metered_call import MeteredRequestNotSent

    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        db.get(EnterpriseAgentTeam, 400).deadline_at = now + timedelta(milliseconds=1)
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(
            db,
            wallet_id=int(wallet.id),
            grant_key="team-deadline-final-send",
            kind="purchase",
            amount_milli=10_000,
        )
    clock_state = {"calls": 0}

    def fake_now():
        clock_state["calls"] += 1
        return now if clock_state["calls"] <= 5 else now + timedelta(seconds=1)

    monkeypatch.setattr(runtime, "_now", fake_now)
    sent = {"count": 0}
    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )

    with pytest.raises(MeteredRequestNotSent, match="发出前"):
        runtime.metered_team_call(
            session_factory=factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            call_index=1,
            model_name="deepseek-flash",
            max_input_tokens=100,
            max_output_tokens=100,
            provider_identity=provider,
            send=lambda: sent.__setitem__("count", sent["count"] + 1) or (
                "should-not-send", {"prompt_tokens": 1, "completion_tokens": 1},
            ),
            now=now,
        )

    assert sent["count"] == 0
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "released"
        assert db.get(EnterpriseAgentTeamTask, 600).status == "running"


def test_project_revocation_blocks_provider_and_success_result_but_allows_failure_closure(runtime_factory):
    import json

    factory, now = runtime_factory
    with factory.begin() as db:
        group = EnterpriseGroup(id=50, enterprise_id=10, name="范围组", status="active")
        project = Project(user_id=2, project_name="企业范围项目", status="active")
        db.add_all([group, project])
        db.flush()
        db.add_all(
            [
                EnterpriseGroupMember(
                    enterprise_id=10,
                    group_id=50,
                    user_id=2,
                    role="member",
                    status="active",
                ),
                EnterpriseProject(
                    enterprise_id=10,
                    project_id=int(project.id),
                    group_id=50,
                    status="active",
                ),
            ]
        )
        team = db.get(EnterpriseAgentTeam, 400)
        team.project_id = int(project.id)
        plan = json.loads(team.plan_snapshot_json)
        plan["project_id"] = int(project.id)
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)

    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        membership = db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=10,
            group_id=50,
            user_id=2,
        ).one()
        membership.status = "inactive"

    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )
    sent = {"count": 0}

    def send():
        sent["count"] += 1
        return "must-not-send", {"prompt_tokens": 1, "completion_tokens": 1}

    with pytest.raises(ForbiddenError, match="项目操作权限"):
        runtime.metered_team_call(
            session_factory=factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            call_index=1,
            model_name="deepseek-flash",
            max_input_tokens=100,
            max_output_tokens=100,
            provider_identity=provider,
            send=send,
            now=now,
        )
    assert sent["count"] == 0

    with pytest.raises(ForbiddenError, match="项目操作权限"):
        runtime.complete_task(
            factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            result={"summary": "未授权项目结果不得发布"},
            now=now,
        )

    closed = runtime.fail_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        error_code="project_access_revoked",
        result={"status": "blocked", "summary": "项目授权已撤销"},
        retryable=False,
        now=now,
    )
    assert closed["status"] == "failed"
    with factory() as db:
        assert db.query(BillingReservation).count() == 0
        assert db.query(EnterpriseSecurityArtifact).count() == 0
        result = json.loads(db.get(EnterpriseAgentTeamTask, 600).result_json)
        assert result["status"] == "blocked"


def test_complete_task_commits_immutable_security_artifact_and_small_reference(runtime_factory):
    import hashlib
    import json

    factory, now = runtime_factory
    _bind_runtime_team_to_project(factory)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    _settle_runtime_team_attempt(factory, claim=claim)
    payload = _security_artifact_payload()

    completed = runtime.complete_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        result={"findings": ["large report must live outside task JSON"]},
        security_artifact=payload,
        now=now,
    )

    assert completed["status"] == "completed"
    assert completed["security_artifact_id"] > 0
    with factory() as db:
        artifact = db.query(EnterpriseSecurityArtifact).one()
        task = db.get(EnterpriseAgentTeamTask, 600)
        result = json.loads(task.result_json)
        assert artifact.team_id == 400 and artifact.task_id == 600
        assert artifact.enterprise_id == 10 and artifact.project_id == 701
        assert artifact.attempt_no == claim["attempt"]
        assert artifact.lease_generation == claim["lease_generation"]
        assert artifact.coverage_status == "complete" and artifact.termination_reason == ""
        assert hashlib.sha256(artifact.report_json.encode("utf-8")).hexdigest() == artifact.report_sha256
        assert result["security_artifact_id"] == artifact.id
        assert result["summary"] == payload["summary"]
        assert "findings" not in result and "report" not in result
        assert db.get(EnterpriseAgentTeam, 400).status == "completed"


def test_security_artifact_can_complete_zero_call_static_scan_without_fake_reservation(runtime_factory):
    factory, now = runtime_factory
    _bind_runtime_team_to_project(factory)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)

    completed = runtime.complete_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        result={"status": "completed"},
        security_artifact=_security_artifact_payload(provider_calls=0),
        now=now,
    )

    assert completed["status"] == "completed"
    with factory() as db:
        assert db.query(BillingReservation).count() == 0
        assert db.query(EnterpriseSecurityArtifact).count() == 1


def test_security_artifact_rejects_model_report_without_a_billing_reservation(runtime_factory):
    factory, now = runtime_factory
    _bind_runtime_team_to_project(factory)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)

    with pytest.raises(PaymentRequiredError, match="预占"):
        runtime.complete_task(
            factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            result={"status": "completed"},
            security_artifact=_security_artifact_payload(provider_calls=1),
            now=now,
        )


def test_security_artifact_rejects_unreported_settled_reservation(runtime_factory):
    factory, now = runtime_factory
    _bind_runtime_team_to_project(factory)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    _settle_runtime_team_attempt(factory, claim=claim)

    with pytest.raises(PaymentRequiredError, match="请求数与企业账本预占不一致"):
        runtime.complete_task(
            factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            result={"status": "completed"},
            security_artifact=_security_artifact_payload(provider_calls=0),
            now=now,
        )


def test_security_artifact_commit_rechecks_live_scan_grant(runtime_factory):
    factory, now = runtime_factory
    _bind_runtime_team_to_project(factory)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    _settle_runtime_team_attempt(factory, claim=claim)
    with factory.begin() as db:
        role = db.query(Role).filter_by(code="user").one()
        permission = db.query(Permission).filter_by(code="security:scan").one()
        assignment = db.query(RolePermission).filter_by(role_id=role.id, permission_id=permission.id).one()
        db.delete(assignment)

    with pytest.raises(ForbiddenError, match="已撤销 security:scan"):
        runtime.complete_task(
            factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            result={"status": "completed"},
            security_artifact=_security_artifact_payload(provider_calls=1),
            now=now,
        )

    with factory() as db:
        assert db.query(EnterpriseSecurityArtifact).count() == 0
        assert db.get(EnterpriseAgentTeamTask, 600).status == "running"


def test_enterprise_dispatcher_persists_project_security_report_as_artifact(runtime_factory, monkeypatch):
    import json
    from contextlib import nullcontext

    from app.services import agent_governance_service, agent_mesh_dispatcher, enterprise_agent_team_service

    factory, _now = runtime_factory
    _bind_runtime_team_to_project(factory)
    with factory.begin() as db:
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(
            db,
            wallet_id=int(wallet.id),
            grant_key="security-artifact-team",
            kind="purchase",
            amount_milli=20_000,
        )
        team = db.get(EnterpriseAgentTeam, 400)
        member = db.get(EnterpriseAgentTeamMember, 500)
        task = db.get(EnterpriseAgentTeamTask, 600)
        _contract, capabilities = enterprise_agent_team_service._contract_snapshot("security_sentinel")
        member.address = "agent:security_sentinel"
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)
        task.input_json = runtime._canonical_json({"scan_mode": "static_full", "top_n": 50})
        team.title = "项目安全审计"

    monkeypatch.setattr(agent_governance_service, "is_runtime_enabled", lambda *_args: True)
    monkeypatch.setattr("app.services.agent_scheduler_runtime.agent_execution_scope", lambda _check: nullcontext())
    report = {
        "project_id": 701,
        "source_archive_sha256": "c" * 64,
        "summary": "项目安全扫描完成",
        "findings": [],
        "compliance": {
            "scan_mode": "static_full",
            "audit_inputs_complete": True,
            "semantic_execution_complete": True,
            "audit_request_accounting_complete": True,
            "static_complete": True,
            "provider_request_count": 0,
            "verification": {"enabled": True, "complete": True, "model_calls": 0},
        },
    }

    def fake_handle(_db, _user, _address, message, *, trusted_team_execution):
        assert trusted_team_execution is True
        assert message["context"]["project_id"] == 701
        assert "project_id" not in message["payload"]
        return "安全哨兵", {
            "status": "completed",
            "summary": "项目安全扫描完成",
            "evidence": [{"source": "request_scoped_agent", "data": report}],
            "errors": [],
        }

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", fake_handle)

    stats = enterprise_agent_team_dispatcher.dispatch_once(
        session_factory=factory,
        execute_claimed=enterprise_agent_team_dispatcher.execute_registered_agent,
        enterprise_id=10,
        batch_size=1,
        concurrency=1,
    )

    assert stats == {"claimed": 1, "completed": 1, "retryable": 0, "reconciliation": 0, "failed": 0}
    with factory() as db:
        artifact = db.query(EnterpriseSecurityArtifact).one()
        task_result = json.loads(db.get(EnterpriseAgentTeamTask, 600).result_json)
        assert artifact.project_id == 701
        assert artifact.coverage_status == "complete"
        assert task_result["security_artifact_id"] == artifact.id
        assert "findings" not in task_result


@pytest.mark.parametrize(
    "verification",
    [
        {"enabled": False, "complete": True},
        {"enabled": True, "complete": False},
        None,
    ],
)
def test_security_report_verification_must_be_explicitly_enabled_and_complete(verification):
    report = {
        "project_id": 701,
        "source_archive_sha256": "c" * 64,
        "summary": "静态扫描完成",
        "findings": [],
        "compliance": {
            "scan_mode": "static_full",
            "audit_inputs_complete": True,
            "semantic_execution_complete": True,
            "audit_request_accounting_complete": True,
            "static_complete": True,
            "provider_request_count": 0,
            "verification": verification,
        },
    }
    result = enterprise_agent_team_dispatcher._security_artifact_from_result(
        {"status": "completed", "evidence": [{"source": "request_scoped_agent", "data": report}]},
        project_id=701,
    )

    assert result is not None
    assert result["coverage_status"] == "incomplete"
    assert result["termination_reason"] == "coverage_incomplete"


def test_failed_incomplete_scan_commits_artifact_only_with_terminal_failure(runtime_factory):
    import json

    factory, now = runtime_factory
    _bind_runtime_team_to_project(factory)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    _settle_runtime_team_attempt(factory, claim=claim)

    failed = runtime.fail_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        error_code="budget_exhausted",
        result={"status": "blocked"},
        retryable=False,
        security_artifact=_security_artifact_payload(
            coverage_status="incomplete",
            termination_reason="budget_exhausted",
        ),
        now=now,
    )

    assert failed["status"] == "failed"
    with factory() as db:
        artifact = db.query(EnterpriseSecurityArtifact).one()
        task_result = json.loads(db.get(EnterpriseAgentTeamTask, 600).result_json)
        assert artifact.coverage_status == "incomplete"
        assert artifact.termination_reason == "budget_exhausted"
        assert task_result["coverage_status"] == "incomplete"
        assert task_result["security_artifact_id"] == artifact.id
        assert db.get(EnterpriseAgentTeam, 400).status == "failed"


def test_invalid_report_rolls_back_task_transition_and_artifact(runtime_factory):
    factory, now = runtime_factory
    _bind_runtime_team_to_project(factory)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    _settle_runtime_team_attempt(factory, claim=claim)
    payload = _security_artifact_payload()
    payload["source_sha256"] = "not-a-digest"

    with pytest.raises(ValidationError, match="源码摘要"):
        runtime.complete_task(
            factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            result={"status": "completed"},
            security_artifact=payload,
            now=now,
        )
    with factory() as db:
        assert db.query(EnterpriseSecurityArtifact).count() == 0
        assert db.get(EnterpriseAgentTeamTask, 600).status == "running"


def test_project_scoped_artifact_commit_can_finish_after_paid_period_expires(runtime_factory, monkeypatch):
    factory, _ = runtime_factory
    billing_now = {"value": datetime.now(timezone.utc).replace(tzinfo=None)}
    period_end = billing_now["value"] + timedelta(minutes=1)
    with factory.begin() as db:
        _seed_verified_enterprise_period(
            db,
            started_at=billing_now["value"] - timedelta(days=30),
            ends_at=period_end,
            order_no="project-artifact-expiry-order",
        )
    _bind_runtime_team_to_project(factory)
    monkeypatch.setattr("app.services.billing_checkout_service._now", lambda: billing_now["value"])
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=billing_now["value"])
    _settle_runtime_team_attempt(factory, claim=claim)
    billing_now["value"] = period_end + timedelta(seconds=1)

    completed = runtime.complete_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        result={"status": "completed"},
        security_artifact=_security_artifact_payload(),
        now=billing_now["value"],
    )

    assert completed["status"] == "completed"
    with factory() as db:
        assert db.query(EnterpriseSecurityArtifact).count() == 1
        assert db.query(BillingReservation).one().status == "settled"


def test_expired_real_subscription_does_not_block_sent_call_settlement_or_completion(runtime_factory, monkeypatch):
    """After a verified paid period expires, sent work may settle; new work stays gated."""
    import json
    from contextlib import nullcontext
    from datetime import timezone
    from types import SimpleNamespace

    from app.agents.base import BaseAgent
    from app.services import agent_governance_service, agent_mesh_dispatcher
    from app.utils.api_resolver import ApiConfig

    factory, _runtime_now = runtime_factory
    billing_now = {"value": datetime.now(timezone.utc).replace(tzinfo=None)}
    ends_at = billing_now["value"] + timedelta(hours=1)
    plan = {**_PLAN_BUDGET, "max_input_tokens_per_call": 2_000}
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        _seed_verified_enterprise_period(
            db,
            started_at=billing_now["value"] - timedelta(days=30),
            ends_at=ends_at,
            order_no="real-expiry-order",
        )
        member = db.get(EnterpriseAgentTeamMember, 500)
        capabilities = {
            "agent_code": "code_reviewer",
            "execution_mode": "model_only",
            "tools": [],
            "external_effects": False,
        }
        member.address = "agent:code_reviewer"
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(
            db, wallet_id=int(wallet.id), grant_key="expired-period-team-credit", kind="purchase", amount_milli=20_000,
        )

    monkeypatch.setattr("app.services.billing_checkout_service._now", lambda: billing_now["value"])
    monkeypatch.setattr(agent_mesh_dispatcher, "dispatch_state", lambda _address: "executable")
    monkeypatch.setattr(agent_governance_service, "is_runtime_enabled", lambda _db, _code: True)
    monkeypatch.setattr(
        "app.services.enterprise_agent_team_service._contract_snapshot",
        lambda _code: (None, capabilities),
    )
    monkeypatch.setattr("app.services.agent_scheduler_runtime.agent_execution_scope", lambda _check: nullcontext())
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: SimpleNamespace(request_url=url, host_header="api.deepseek.com", request_extensions={}),
    )
    provider_calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "deepseek-flash",
                "usage": {"prompt_tokens": 30, "completion_tokens": 10},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"ok": True})}}],
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            provider_calls.append(True)
            billing_now["value"] = ends_at + timedelta(days=8)
            return Response()

    monkeypatch.setattr("app.agents.base.httpx.Client", Client)

    def fake_handle(_db, _user, code, _message, *, trusted_team_execution):
        assert trusted_team_execution is True
        result = BaseAgent(model="deepseek-flash", max_tokens=20).call_json(
            "subscription expiry probe",
            api_config=ApiConfig(
                api_key="fake-key", base_url="https://api.deepseek.com/v1", model="deepseek-flash",
                provider="deepseek", source="system", max_retries=0,
            ),
            max_tokens=20,
        )
        return code, agent_mesh_dispatcher._as_mesh_result(result, action="代码审查")

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", fake_handle)
    stats = enterprise_agent_team_dispatcher.dispatch_once(
        session_factory=factory,
        execute_claimed=enterprise_agent_team_dispatcher.execute_registered_agent,
        enterprise_id=10,
        batch_size=1,
        concurrency=1,
    )
    assert stats == {"claimed": 1, "completed": 1, "retryable": 0, "reconciliation": 0, "failed": 0}
    with factory() as db:
        assert db.query(BillingReservation).one().status == "settled"
        assert db.get(EnterpriseAgentTeamTask, 600).status == "completed"
        assert db.get(EnterpriseAgentTeam, 400).status == "completed"
    assert len(provider_calls) == 1

    from app.services import billing_checkout_service, enterprise_assistant_service

    def require_real_subscription(db, enterprise_id):
        if billing_checkout_service.enterprise_subscription_status(db, enterprise_id) not in {
            "active", "grace",
        }:
            raise PaymentRequiredError("企业订阅已到期")

    monkeypatch.setattr(enterprise_assistant_service, "_require_subscription", require_real_subscription)
    with factory.begin() as db:
        db.get(EnterpriseAgentTeam, 400).status = "running"
        task = db.get(EnterpriseAgentTeamTask, 600)
        task.status = "queued"
        task.completed_at = None
    with pytest.raises(PaymentRequiredError, match="订阅已到期"):
        runtime.claim_task(factory, enterprise_id=10, task_id=600, now=_runtime_now + timedelta(seconds=1))
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).attempt_count == 1
        assert db.query(BillingReservation).count() == 1
    assert len(provider_calls) == 1


def test_expired_real_subscription_still_allows_leased_task_reconciliation(runtime_factory, monkeypatch):
    from datetime import timezone

    factory, runtime_now = runtime_factory
    billing_now = {"value": datetime.now(timezone.utc).replace(tzinfo=None)}
    ends_at = billing_now["value"] + timedelta(hours=1)
    with factory.begin() as db:
        _seed_verified_enterprise_period(
            db,
            started_at=billing_now["value"] - timedelta(days=30),
            ends_at=ends_at,
            order_no="real-expiry-recovery-order",
        )
    monkeypatch.setattr("app.services.billing_checkout_service._now", lambda: billing_now["value"])
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=runtime_now)
    billing_now["value"] = ends_at + timedelta(days=8)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        task.lease_expires_at = runtime_now - timedelta(seconds=1)
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, task, claim["attempt"], 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                group_id=None,
                user_id=2,
                usage_kind="member",
                requested_milli=1_000,
                status="unknown",
                usage_state="unknown",
                enterprise_wallet_id=900,
            )
        )

    assert runtime.claim_next_task(factory, enterprise_id=10, now=runtime_now + timedelta(seconds=1)) is None
    with factory() as db:
        from app.services import billing_checkout_service

        assert billing_checkout_service.enterprise_subscription_status(db, 10) == "expired"
        assert db.get(EnterpriseAgentTeamTask, 600).status == "awaiting_reconciliation"
        assert db.get(EnterpriseAgentTeam, 400).status == "awaiting_reconciliation"
        assert db.query(BillingReservation).one().status == "unknown"


@pytest.mark.parametrize(
    ("reservation_status", "expected_status"),
    [("settled", "failed"), ("unknown", "awaiting_reconciliation")],
)
def test_expired_real_subscription_allows_terminal_failure_without_unfreezing_billing(
    runtime_factory, monkeypatch, reservation_status, expected_status,
):
    factory, runtime_now = runtime_factory
    billing_now = {"value": datetime.now(timezone.utc).replace(tzinfo=None)}
    ends_at = billing_now["value"] + timedelta(hours=1)
    with factory.begin() as db:
        _seed_verified_enterprise_period(
            db,
            started_at=billing_now["value"] - timedelta(days=30),
            ends_at=ends_at,
            order_no=f"real-expiry-fail-{reservation_status}",
        )
    monkeypatch.setattr("app.services.billing_checkout_service._now", lambda: billing_now["value"])
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=runtime_now)
    if reservation_status == "settled":
        _settle_runtime_team_attempt(factory, claim=claim)
    else:
        with factory.begin() as db:
            team = db.get(EnterpriseAgentTeam, 400)
            task = db.get(EnterpriseAgentTeamTask, 600)
            db.add(
                BillingReservation(
                    request_key=runtime._reservation_key(team, task, int(claim["attempt"]), 1),
                    model_name="deepseek-flash",
                    enterprise_id=10,
                    user_id=2,
                    usage_kind="member",
                    requested_milli=1_000,
                    status="unknown",
                    usage_state="unknown",
                    enterprise_wallet_id=900,
                )
            )
    billing_now["value"] = ends_at + timedelta(days=8, seconds=1)

    result = runtime.fail_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        error_code="provider_failure",
        result={"status": "failed"},
        retryable=False,
        now=runtime_now + timedelta(seconds=1),
    )

    assert result["status"] == expected_status
    with factory() as db:
        from app.services import billing_checkout_service

        assert billing_checkout_service.enterprise_subscription_status(db, 10) == "expired"
        assert db.get(EnterpriseAgentTeamTask, 600).status == expected_status
        assert db.get(EnterpriseAgentTeam, 400).status == expected_status
        assert db.query(BillingReservation).one().status == reservation_status


def test_deterministic_failure_keeps_unknown_reservation_frozen(runtime_factory):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, task, 1, 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                group_id=None,
                user_id=2,
                usage_kind="member",
                requested_milli=1_000,
                status="unknown",
                usage_state="unknown",
                enterprise_wallet_id=900,
            )
        )

    result = runtime.fail_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        error_code="budget_exhausted",
        result={"status": "blocked", "coverage_status": "incomplete"},
        retryable=False,
        now=now,
    )

    assert result["status"] == "awaiting_reconciliation"
    assert result["team_status"] == "awaiting_reconciliation"
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "awaiting_reconciliation"
        assert db.get(EnterpriseAgentTeam, 400).status == "awaiting_reconciliation"


def test_incomplete_coverage_summary_is_preserved_and_bounded():
    compact = {
        "stage": "failed",
        "calls": 3,
        "completed_ranges": [[1, 10], [21, 30]],
        "failed_batch": 3,
    }
    result = {"evidence": [{"data": {"coverage": compact}}]}
    assert enterprise_agent_team_dispatcher._incomplete_coverage_summary(result) == compact

    oversized = {
        "stage": "failed",
        "calls": 3,
        "completed_ranges": [[index, index + 1] for index in range(70)],
        "provider_dump": "x" * 50_000,
    }
    summary = enterprise_agent_team_dispatcher._incomplete_coverage_summary(
        {"evidence": [{"data": {"coverage": oversized}}]}
    )
    assert summary["stage"] == "failed"
    assert summary["calls"] == 3
    assert summary["completed_ranges_total"] == 70
    assert summary["completed_ranges_truncated"] is True
    assert "provider_dump" not in summary


def test_runtime_rejects_parent_run_or_runtime_group_scope_drift(runtime_factory):
    factory, now = runtime_factory
    with factory.begin() as db:
        db.get(EnterpriseAssistantRun, 200).group_id = 999
    with pytest.raises(ForbiddenError, match="付款方或会话快照不匹配"):
        runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)


def test_queued_team_continues_after_parent_reply_completes(runtime_factory):
    factory, now = runtime_factory
    with factory.begin() as db:
        run = db.get(EnterpriseAssistantRun, 200)
        state = db.get(EnterpriseAssistantRuntimeState, 300)
        run.status = "completed"
        state.status = "completed"
        state.lease_expires_at = None

    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    assert claim["status"] == "running"


@pytest.mark.parametrize(
    ("run_status", "runtime_status", "stop_requested", "generation"),
    [("cancelled", "cancelled", True, 1), ("completed", "completed", False, 2)],
)
def test_parent_cancellation_or_supersession_blocks_queued_team(
    runtime_factory,
    run_status,
    runtime_status,
    stop_requested,
    generation,
):
    factory, now = runtime_factory
    with factory.begin() as db:
        run = db.get(EnterpriseAssistantRun, 200)
        state = db.get(EnterpriseAssistantRuntimeState, 300)
        team = db.get(EnterpriseAgentTeam, 400)
        run.status = run_status
        state.status = runtime_status
        state.stop_requested = stop_requested
        state.lease_generation = generation
        state.lease_expires_at = None
        if generation != team.parent_lease_generation:
            team.parent_lease_generation = 1
    with pytest.raises(ForbiddenError, match="父运行已停止、过期或被新授权代次替代"):
        runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)


def test_expired_attempt_with_proven_not_sent_hold_can_be_claimed_again(runtime_factory):
    factory, now = runtime_factory
    first = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        task.lease_expires_at = now - timedelta(seconds=1)
        db.add(
            BillingReservation(
                request_key=runtime._reservation_key(team, task, 1, 1),
                model_name="deepseek-flash",
                enterprise_id=10,
                group_id=None,
                user_id=2,
                usage_kind="member",
                requested_milli=1000,
                status="released",
                usage_state="not_sent",
                enterprise_wallet_id=900,
            )
        )
    second = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now + timedelta(seconds=1))
    assert second["status"] == "running"
    assert second["attempt"] == 2
    assert second["lease_generation"] > first["lease_generation"]
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "running"
        assert db.get(EnterpriseAgentTeam, 400).status == "running"


def test_unapproved_high_risk_plan_cannot_be_claimed(runtime_factory):
    factory, now = runtime_factory
    high_risk = {**_PLAN_BUDGET, "risk_level": "high", "requires_approval": True}
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        team.plan_snapshot_json = runtime._canonical_json(high_risk)
        team.plan_sha256 = runtime._digest(high_risk)
    with pytest.raises(ForbiddenError):
        runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).attempt_count == 0


def test_high_risk_plan_uses_current_independent_enterprise_approval(runtime_factory):
    factory, now = runtime_factory
    plan = {**_PLAN_BUDGET, "risk_level": "high", "requires_approval": True}
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        task.approval_sha256 = team.plan_sha256
        task.approved_by_user_id = 1
        db.add(
            EnterpriseAgentTeamApproval(
                id=700,
                team_id=400,
                task_id=600,
                enterprise_id=10,
                conversation_id=100,
                group_id=None,
                initiator_user_id=2,
                approval_key="approve-task-600",
                plan_sha256=team.plan_sha256,
                required_permission_code="enterprise_admin",
                status="approved",
                plan_json=runtime._canonical_json(plan),
                requested_by_user_id=2,
                decided_by_user_id=1,
                decision_reason="已审批",
                decided_at=now,
            )
        )
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    assert claim["status"] == "running"


def test_high_risk_plan_stops_if_approver_loses_current_admin_role(runtime_factory):
    factory, now = runtime_factory
    plan = {**_PLAN_BUDGET, "risk_level": "high", "requires_approval": True}
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        task = db.get(EnterpriseAgentTeamTask, 600)
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        task.approval_sha256 = team.plan_sha256
        task.approved_by_user_id = 1
        db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=1).update({"role": "member"})
        db.add(
            EnterpriseAgentTeamApproval(
                id=701,
                team_id=400,
                task_id=600,
                enterprise_id=10,
                conversation_id=100,
                group_id=None,
                initiator_user_id=2,
                approval_key="approve-task-600-revoked",
                plan_sha256=team.plan_sha256,
                required_permission_code="enterprise_admin",
                status="approved",
                plan_json=runtime._canonical_json(plan),
                requested_by_user_id=2,
                decided_by_user_id=1,
                decision_reason="已审批",
                decided_at=now,
            )
        )
    with pytest.raises(ForbiddenError):
        runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)


def test_enterprise_metered_call_never_uses_personal_wallet(runtime_factory):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )
    with factory.begin() as db:
        enterprise_wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(
            db, wallet_id=int(enterprise_wallet.id), grant_key="team-credit", kind="purchase", amount_milli=20_000
        )
        personal_wallet = ledger.get_or_create_wallet(db, "user", 2)
        ledger.grant(
            db, wallet_id=int(personal_wallet.id), grant_key="personal-credit", kind="purchase", amount_milli=20_000
        )

    send_count = {"value": 0}

    def send_once():
        send_count["value"] += 1
        return "result", {"prompt_tokens": 80, "completion_tokens": 30}

    result = runtime.metered_team_call(
        session_factory=factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        call_index=1,
        model_name="deepseek-flash",
        max_input_tokens=200,
        max_output_tokens=200,
        provider_identity=provider,
        send=send_once,
        now=now,
    )
    assert result == "result"
    with pytest.raises(ledger.IdempotencyConflict):
        runtime.metered_team_call(
            session_factory=factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            call_index=1,
            model_name="deepseek-flash",
            max_input_tokens=200,
            max_output_tokens=200,
            provider_identity=provider,
            send=send_once,
            now=now,
        )
    assert send_count["value"] == 1
    completed = runtime.complete_task(
        factory,
        enterprise_id=10,
        task_id=600,
        lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"],
        result={"answer": "完成"},
        now=now,
    )
    assert completed["status"] == "completed" and completed["team_status"] == "completed"
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "settled"
        assert reservation.enterprise_id == 10 and reservation.user_id == 2
        assert reservation.enterprise_wallet_id is not None
        assert reservation.personal_wallet_id is None and reservation.allow_personal == 0
        assert reservation.input_tokens == 80 and reservation.output_tokens == 30
        enterprise_batch = db.query(BillingCreditBatch).filter_by(grant_key="team-credit").one()
        personal_batch = db.query(BillingCreditBatch).filter_by(grant_key="personal-credit").one()
        assert enterprise_batch.remaining_milli < 20_000
        assert personal_batch.remaining_milli == 20_000
        assert {row.event_type for row in db.query(EnterpriseAgentTeamEvent).all()} >= {
            "task.claimed",
            "billing.reserved",
            "billing.settled",
            "task.completed",
        }


def test_company_team_bills_frozen_default_group_without_changing_chat_scope(runtime_factory, monkeypatch):
    from app.services import enterprise_assistant_runtime_service as assistant_runtime

    monkeypatch.setattr(assistant_runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    factory, now = runtime_factory
    with factory.begin() as db:
        db.add(EnterpriseGroup(id=1000, enterprise_id=10, name="默认组", status="active"))
        db.add(EnterpriseGroupMember(enterprise_id=10, group_id=1000, user_id=2, role="member", status="active"))
        member = db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
        member.default_group_id = 1000
        member.billing_group_version = 3
        run = db.get(EnterpriseAssistantRun, 200)
        run.billing_attribution_version = 1
        run.billing_group_id = 1000
        run.billing_membership_version = 3
        run.billing_user_token_version = int(db.get(User, 2).token_version)
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(db, wallet_id=int(wallet.id), grant_key="team-group-credit", kind="purchase", amount_milli=20_000)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    provider = ledger.BillingProviderIdentity(identity_version="deepseek-official-v1", provider_name="deepseek",
                                               endpoint_host="api.deepseek.com", credential_source="system")
    def send_then_close_gate():
        monkeypatch.setattr(assistant_runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
        return "result", {"prompt_tokens": 80, "completion_tokens": 30}

    assert runtime.metered_team_call(
        session_factory=factory, enterprise_id=10, task_id=600, lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"], call_index=1, model_name="deepseek-flash",
        max_input_tokens=200, max_output_tokens=200, provider_identity=provider,
        send=send_then_close_gate, now=now,
    ) == "result"
    with pytest.raises(ForbiddenError, match="暂不能继续执行"):
        runtime.metered_team_call(
            session_factory=factory, enterprise_id=10, task_id=600, lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"], call_index=2, model_name="deepseek-flash",
            max_input_tokens=200, max_output_tokens=200, provider_identity=provider,
            send=lambda: pytest.fail("closed gate must prevent another provider call"), now=now,
        )
    assert runtime.complete_task(
        factory, enterprise_id=10, task_id=600, lease_token=claim["lease_token"],
        lease_generation=claim["lease_generation"], result={"answer": "完成"}, now=now,
    )["status"] == "completed"
    with factory() as db:
        assert db.query(BillingReservation).one().group_id == 1000
        assert db.get(EnterpriseAgentTeam, 400).group_id is None
        assert db.get(EnterpriseAssistantRun, 200).group_id is None


def test_enterprise_team_budget_rejects_before_provider_send(runtime_factory):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        plan = {**_PLAN_BUDGET, "risk_level": "low", "requires_approval": False, "max_team_credits_milli": 1}
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(db, wallet_id=int(wallet.id), grant_key="team-budget-credit", kind="purchase", amount_milli=20_000)

    send_count = {"value": 0}

    def send_once():
        send_count["value"] += 1
        return "must-not-send", {"prompt_tokens": 80, "completion_tokens": 30}

    with pytest.raises(PaymentRequiredError, match="费用预算已用尽"):
        runtime.metered_team_call(
            session_factory=factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            call_index=1,
            model_name="deepseek-flash",
            max_input_tokens=200,
            max_output_tokens=200,
            provider_identity=provider,
            send=send_once,
            now=now,
        )
    assert send_count["value"] == 0
    with factory() as db:
        assert db.query(BillingReservation).count() == 0
        assert not any(event.event_type.startswith("billing.") for event in db.query(EnterpriseAgentTeamEvent).all())


def test_cancel_invalidates_lease_and_prevents_late_result_write(runtime_factory):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    result = runtime.cancel_team(
        factory,
        enterprise_id=10,
        team_id=400,
        actor_user_id=2,
        reason="用户停止",
        now=now,
    )
    assert result["status"] == "cancelled"
    with pytest.raises(ConflictError):
        runtime.complete_task(
            factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            result={"answer": "late"},
            now=now,
        )
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "cancelled"
        assert db.get(EnterpriseAgentTeam, 400).stop_requested is True


def test_team_retry_requires_direct_chat_participation(runtime_factory):
    factory, now = runtime_factory
    with factory.begin() as db:
        room = db.get(EnterpriseConversation, 100)
        room.kind = "direct"
        room.conversation_key = "direct:2:3"
        room.participant_low_id = 2
        room.participant_high_id = 3
        team = db.get(EnterpriseAgentTeam, 400)
        team.status = "running"
        task = db.get(EnterpriseAgentTeamTask, 600)
        task.status = "retryable"
        task.attempt_count = 1

    with pytest.raises(ForbiddenError, match="私聊运行只允许会话参与者重试"):
        runtime.retry_task(
            factory,
            enterprise_id=10,
            team_id=400,
            task_id=600,
            actor_user_id=1,
            now=now,
        )
    with pytest.raises(ForbiddenError, match="没有该私聊的运行管理权限"):
        runtime.cancel_team(
            factory,
            enterprise_id=10,
            team_id=400,
            actor_user_id=1,
            reason="非参与者停止尝试",
            now=now,
        )
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "retryable"
        assert db.get(EnterpriseAgentTeam, 400).status == "running"

    with factory.begin() as db:
        room = db.get(EnterpriseConversation, 100)
        room.conversation_key = "direct:1:2"
        room.participant_low_id = 1
        room.participant_high_id = 2
    retried = runtime.retry_task(
        factory,
        enterprise_id=10,
        team_id=400,
        task_id=600,
        actor_user_id=1,
        now=now,
    )
    assert retried["status"] == "queued"


def test_cancel_after_reservation_before_send_releases_hold_and_blocks_provider(runtime_factory, monkeypatch):
    factory, now = runtime_factory
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )
    with factory.begin() as db:
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(db, wallet_id=int(wallet.id), grant_key="cancel-race-credit", kind="purchase", amount_milli=20_000)

    real_metered_call = runtime.billing_metered_call.metered_call

    def cancel_after_reserve(**kwargs):
        send_guard = kwargs["send_guard"]

        def cancel_then_send(send, request_key):
            runtime.cancel_team(
                factory,
                enterprise_id=10,
                team_id=400,
                actor_user_id=2,
                reason="竞态取消",
                now=now,
            )
            return send_guard(send, request_key)

        kwargs["send_guard"] = cancel_then_send
        return real_metered_call(**kwargs)

    monkeypatch.setattr(runtime.billing_metered_call, "metered_call", cancel_after_reserve)
    send_count = {"value": 0}

    def send_once():
        send_count["value"] += 1
        return "must-not-send", {"prompt_tokens": 10, "completion_tokens": 10}

    with pytest.raises(runtime.billing_metered_call.MeteredRequestNotSent):
        runtime.metered_team_call(
            session_factory=factory,
            enterprise_id=10,
            task_id=600,
            lease_token=claim["lease_token"],
            lease_generation=claim["lease_generation"],
            call_index=1,
            model_name="deepseek-flash",
            max_input_tokens=200,
            max_output_tokens=200,
            provider_identity=provider,
            send=send_once,
            now=now,
        )
    assert send_count["value"] == 0
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "released" and reservation.usage_state == "not_sent"
        batch = db.query(BillingCreditBatch).filter_by(grant_key="cancel-race-credit").one()
        assert batch.remaining_milli == 20_000 and batch.reserved_milli == 0
        assert db.get(EnterpriseAgentTeam, 400).status == "cancelled"
        assert any(event.event_type == "billing.not_sent" for event in db.query(EnterpriseAgentTeamEvent).all())


def test_dispatcher_marks_only_unbilled_executor_failure_retryable(runtime_factory):
    factory, _ = runtime_factory

    def executor(session_factory, claim):
        raise RuntimeError("worker failed before provider request")

    stats = enterprise_agent_team_dispatcher.dispatch_once(
        session_factory=factory,
        execute_claimed=executor,
        enterprise_id=10,
        batch_size=4,
        concurrency=2,
    )
    assert stats == {"claimed": 1, "completed": 0, "retryable": 1, "reconciliation": 0, "failed": 0}
    with factory() as db:
        assert db.get(EnterpriseAgentTeamTask, 600).status == "retryable"
        assert db.query(BillingReservation).count() == 0


def test_registered_agent_dispatcher_bills_through_enterprise_wallet(runtime_factory, monkeypatch):
    from app.services import agent_governance_service, agent_mesh_dispatcher
    from app.services.billing_runtime_gate import metered_response_request

    factory, now = runtime_factory
    capabilities = {
        "agent_code": "code_reviewer",
        "execution_mode": "model_only",
        "tools": [],
        "external_effects": False,
    }
    with factory.begin() as db:
        assert db.get(EnterpriseAgentTeam, 400) is not None
        member = db.get(EnterpriseAgentTeamMember, 500)
        member.address = "agent:code_reviewer"
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(
            db, wallet_id=int(wallet.id), grant_key="dispatcher-team-credit", kind="purchase", amount_milli=20_000
        )
        wallet_id = int(wallet.id)

    monkeypatch.setattr(agent_mesh_dispatcher, "dispatch_state", lambda _address: "executable")
    monkeypatch.setattr(agent_governance_service, "is_runtime_enabled", lambda _db, _code: True)
    monkeypatch.setattr(
        "app.services.enterprise_agent_team_service._contract_snapshot",
        lambda _code: (None, capabilities),
    )
    sends = []

    class Response:
        @staticmethod
        def json():
            return {"usage": {"prompt_tokens": 30, "completion_tokens": 10}}

    def fake_handle(_db, _user, code, _message, *, trusted_team_execution):
        assert trusted_team_execution is True
        metered_response_request(
            model_name="deepseek-flash",
            payload={"model": "deepseek-flash", "input": "Check this code", "max_tokens": 20},
            send=lambda: (sends.append(code) or Response(), 0.01),
            provider_name="deepseek",
            base_url="https://api.deepseek.com/v1",
            credential_source="system",
        )
        return code, {"status": "completed", "summary": "已完成只读分析", "evidence": []}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", fake_handle)
    stats = enterprise_agent_team_dispatcher.dispatch_once(
        session_factory=factory,
        execute_claimed=enterprise_agent_team_dispatcher.execute_registered_agent,
        enterprise_id=10,
        batch_size=2,
        concurrency=1,
    )

    assert sends == ["agent:code_reviewer"]
    assert stats == {"claimed": 1, "completed": 1, "retryable": 0, "reconciliation": 0, "failed": 0}
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "settled" and reservation.usage_state == "reported"
        assert reservation.enterprise_id == 10 and reservation.enterprise_wallet_id == wallet_id
        assert reservation.personal_wallet_id is None
        assert db.get(EnterpriseAgentTeamTask, 600).status == "completed"
        assert db.get(EnterpriseAgentTeam, 400).status == "completed"
        assert db.query(EnterpriseChatMessage).filter_by(client_message_id="ent-team-400-receipt").count() == 1


@pytest.mark.parametrize(
    (
        "calls_before_rejection",
        "max_calls",
        "team_budget",
        "expire_subscription_after_send",
        "expected_provider_calls",
        "expected_reservation_states",
        "expected_task_status",
        "expected_failure_kind",
        "expected_coverage_status",
    ),
    [
        (0, 3, 1, False, 0, [], "failed", "budget_exhausted", "incomplete"),
        (1, 1, 10_000, False, 1, ["settled"], "failed", "budget_exhausted", "incomplete"),
        (0, 3, 10_000, True, 1, ["settled"], "completed", None, None),
    ],
)
def test_registered_agent_budget_rejection_is_terminal_and_never_replayed(
    runtime_factory,
    monkeypatch,
    calls_before_rejection,
    max_calls,
    team_budget,
    expire_subscription_after_send,
    expected_provider_calls,
    expected_reservation_states,
    expected_task_status,
    expected_failure_kind,
    expected_coverage_status,
):
    import json
    from contextlib import nullcontext
    from types import SimpleNamespace

    from app.agents.base import BaseAgent
    from app.services import agent_governance_service, agent_mesh_dispatcher, billing_runtime_gate
    from app.utils.api_resolver import ApiConfig

    factory, _now = runtime_factory
    capabilities = {
        "agent_code": "code_reviewer",
        "execution_mode": "model_only",
        "tools": [],
        "external_effects": False,
    }
    plan = {
        **_PLAN_BUDGET,
        "risk_level": "low",
        "requires_approval": False,
        "max_input_tokens_per_call": 2_000,
        "max_calls_per_task": max_calls,
        "max_team_credits_milli": team_budget,
    }
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        member = db.get(EnterpriseAgentTeamMember, 500)
        member.address = "agent:code_reviewer"
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)
        wallet = ledger.get_or_create_wallet(db, "enterprise", 10)
        ledger.grant(
            db, wallet_id=int(wallet.id), grant_key="budget-rejection-credit", kind="purchase", amount_milli=20_000,
        )

    monkeypatch.setattr(agent_mesh_dispatcher, "dispatch_state", lambda _address: "executable")
    monkeypatch.setattr(agent_governance_service, "is_runtime_enabled", lambda _db, _code: True)
    monkeypatch.setattr(
        "app.services.enterprise_agent_team_service._contract_snapshot",
        lambda _code: (None, capabilities),
    )
    monkeypatch.setattr("app.services.agent_scheduler_runtime.agent_execution_scope", lambda _check: nullcontext())
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: SimpleNamespace(request_url=url, host_header="api.deepseek.com", request_extensions={}),
    )
    provider_calls = []
    provider_sent = {"value": False}
    if expire_subscription_after_send:
        settlement_rejected = {"value": False}

        def expire_after_send(_db, _enterprise_id):
            if provider_sent["value"] and not settlement_rejected["value"]:
                settlement_rejected["value"] = True
                raise PaymentRequiredError("企業訂閱已到期")

        monkeypatch.setattr(
            "app.services.enterprise_assistant_service._require_subscription",
            expire_after_send,
        )

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "deepseek-flash",
                "usage": {"prompt_tokens": 30, "completion_tokens": 10},
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({"ok": True})},
                }],
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            provider_calls.append(True)
            provider_sent["value"] = True
            return Response()

    monkeypatch.setattr("app.agents.base.httpx.Client", Client)
    billing_errors = []
    metered_request = billing_runtime_gate.metered_response_request

    def observe_billing_error(**kwargs):
        try:
            return metered_request(**kwargs)
        except Exception as exc:
            billing_errors.append(f"{type(exc).__name__}: {exc}")
            raise

    monkeypatch.setattr(billing_runtime_gate, "metered_response_request", observe_billing_error)

    def fake_handle(_db, _user, code, _message, *, trusted_team_execution):
        assert trusted_team_execution is True
        agent = BaseAgent(model="deepseek-flash", max_tokens=20)
        config = ApiConfig(
            api_key="fake-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            provider="deepseek",
            source="system",
            max_retries=0,
        )
        result = None
        for index in range(calls_before_rejection + 1):
            result = agent.call_json(f"budget probe {index}", api_config=config, max_tokens=20)
        return code, agent_mesh_dispatcher._as_mesh_result(result, action="代码审查")

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", fake_handle)
    stats = enterprise_agent_team_dispatcher.dispatch_once(
        session_factory=factory,
        execute_claimed=enterprise_agent_team_dispatcher.execute_registered_agent,
        enterprise_id=10,
        batch_size=1,
        concurrency=1,
    )

    if expected_failure_kind is None:
        assert billing_errors == []
    else:
        assert len(billing_errors) == 1
        assert billing_errors[0].startswith("PaymentRequiredError:")
    assert provider_calls == [True] * expected_provider_calls
    expected_stats = {
        "claimed": 1,
        "completed": int(expected_task_status == "completed"),
        "retryable": 0,
        "reconciliation": int(expected_task_status == "awaiting_reconciliation"),
        "failed": int(expected_task_status == "failed"),
    }
    assert stats == expected_stats
    with factory() as db:
        task = db.get(EnterpriseAgentTeamTask, 600)
        team = db.get(EnterpriseAgentTeam, 400)
        assert task.status == expected_task_status
        assert team.status == expected_task_status
        reservations = db.query(BillingReservation).order_by(BillingReservation.id).all()
        assert [row.status for row in reservations] == expected_reservation_states
        failure_result = json.loads(task.result_json)
        if expected_failure_kind is not None:
            assert failure_result["failure_kind"] == expected_failure_kind
            assert failure_result.get("coverage_status") == expected_coverage_status
            if expected_coverage_status == "incomplete":
                assert failure_result["termination_reason"] == "budget_exhausted"
            assert failure_result["errors"][0]["code"] == expected_failure_kind
        else:
            assert failure_result["status"] == "completed"
    assert runtime.claim_next_task(factory, enterprise_id=10, now=_now) is None
