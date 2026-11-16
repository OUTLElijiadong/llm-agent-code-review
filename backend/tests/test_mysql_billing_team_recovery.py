"""MySQL serialization of team billing recovery against in-flight sends."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _ready, _seed

from app.models.billing_ledger import BillingReservation, BillingReservationReconciliationEvent
from app.models.enterprise_agent_team import EnterpriseAgentTeam, EnterpriseAgentTeamTask
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import billing_ledger_service as ledger
from app.services import billing_reservation_reconciliation_service as reconciliation
from app.services import enterprise_agent_team_runtime_service as runtime

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


@pytest.mark.parametrize("prior_state", ["reserved", "unknown"])
def test_recovery_waits_for_active_provider_transaction_and_never_releases(
    fresh_chain, verified_model_catalog, prior_state,
):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, _owner, users, tasks, teams = _seed(factory)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    key = f"ent-team:{teams[0]}:{tasks[0]}:1:1"
    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1", provider_name="deepseek",
        endpoint_host="api.deepseek.com", credential_source="system",
    )
    with factory.begin() as db:
        task = db.get(EnterpriseAgentTeamTask, tasks[0])
        task.status = "running"
        task.attempt_count = 1
        task.lease_token = uuid4().hex
        task.lease_generation = 1
        task.lease_expires_at = now - timedelta(minutes=1)
        wallet = ledger.get_or_create_wallet(db, "enterprise", company)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f"recovery-test:{uuid4().hex}",
                     kind="purchase", amount_milli=10_000)
        reservation = ledger.reserve(db, request_key=key, model_name="deepseek-flash", user_id=users[0],
            max_milli=1_000, enterprise_wallet_id=wallet.id, enterprise_id=company, group_id=group,
            provider_identity=provider)
        reservation_id = reservation.id
        team = db.get(EnterpriseAgentTeam, teams[0])
        run_id = team.assistant_run_id
    with factory.begin() as db:
        ledger.mark_model_attempt_started(db, key, now=now - timedelta(minutes=31))
        if prior_state == "unknown":
            ledger.mark_unknown(db, key)

    in_flight, release_provider, recovery_waiting = Event(), Event(), Event()
    recovery_thread = []

    def observe_recovery(_conn, _cursor, sql, _params, _context, _many):
        if (recovery_thread and get_ident() == recovery_thread[0]
                and "FROM ENTERPRISE_ASSISTANT_RUN" in sql.upper() and "FOR UPDATE" in sql.upper()):
            recovery_waiting.set()

    event.listen(fresh_chain, "before_cursor_execute", observe_recovery)

    def send_and_settle():
        with factory.begin() as db:
            db.query(EnterpriseAssistantRun).filter_by(id=run_id).with_for_update().one()
            db.query(EnterpriseAgentTeam).filter_by(id=teams[0]).with_for_update().one()
            in_flight.set()
            assert release_provider.wait(15)
            actual = ledger.quote_reservation_milli(db, request_key=key, input_tokens=1, output_tokens=1)
            ledger.settle(db, request_key=key, actual_milli=actual, input_tokens=1, output_tokens=1,
                          server_tool_search_calls=0, server_tool_fetch_calls=0)

    def recover():
        recovery_thread.append(get_ident())
        with factory.begin() as db:
            with pytest.raises(reconciliation.ReconciliationBlocked):
                reconciliation.recover_stale_team_model_attempt(
                    db, reservation_id=reservation_id, actor_id=1, note="核验租约", now=now,
                )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            sender = pool.submit(send_and_settle)
            assert in_flight.wait(15)
            recovery = pool.submit(recover)
            assert recovery_waiting.wait(5)
            assert not recovery.done(), "recovery passed a locked in-flight request"
            release_provider.set()
            sender.result(timeout=20)
            recovery.result(timeout=20)
    finally:
        release_provider.set()
        event.remove(fresh_chain, "before_cursor_execute", observe_recovery)
    with factory() as db:
        reservation = db.get(BillingReservation, reservation_id)
        task = db.get(EnterpriseAgentTeamTask, tasks[0])
        assert reservation.status == "settled"
        assert task.status == "running" and task.lease_generation == 1


def test_recovery_waits_for_real_metered_team_call_and_cannot_modify_settled_hold(
    fresh_chain, verified_model_catalog, monkeypatch,
):
    """The actual provider guard keeps recovery outside until its settlement commits."""
    _ready(monkeypatch)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, owner, users, tasks, teams = _seed(factory)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    plan = {
        "project_id": None, "allowed_models": ["deepseek-flash"], "max_calls_per_task": 3,
        "max_input_tokens_per_call": 200, "max_output_tokens_per_call": 200,
        "max_team_credits_milli": 50_000, "risk_level": "low", "requires_approval": False,
    }
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, teams[0])
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        wallet = ledger.get_or_create_wallet(db, "enterprise", company)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f"real-team-recovery:{uuid4().hex}",
                     kind="purchase", amount_milli=50_000)

    claim = runtime.claim_task(factory, enterprise_id=company, task_id=tasks[0], now=now)
    assert claim and claim["status"] == "running"
    key = f"ent-team:{teams[0]}:{tasks[0]}:{claim['attempt']}:1"
    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1", provider_name="deepseek",
        endpoint_host="api.deepseek.com", credential_source="system",
    )
    in_provider, release_provider, recovery_waiting = Event(), Event(), Event()
    recovery_thread = []
    sends = []

    def observe_recovery(_conn, _cursor, sql, _params, _context, _many):
        if (recovery_thread and get_ident() == recovery_thread[0]
                and "FROM ENTERPRISE_ASSISTANT_RUN" in sql.upper() and "FOR UPDATE" in sql.upper()):
            recovery_waiting.set()

    def provider_send():
        sends.append(1)
        in_provider.set()
        assert release_provider.wait(15), "provider release timed out"
        return "model response", {"prompt_tokens": 10, "completion_tokens": 10}

    def run_metered():
        return runtime.metered_team_call(
            session_factory=factory, enterprise_id=company, task_id=tasks[0],
            lease_token=claim["lease_token"], lease_generation=claim["lease_generation"],
            call_index=1, model_name="deepseek-flash", max_input_tokens=100, max_output_tokens=100,
            provider_identity=provider, send=provider_send, now=now,
        )

    def recover(reservation_id):
        recovery_thread.append(get_ident())
        with factory.begin() as db:
            with pytest.raises(reconciliation.ReconciliationBlocked):
                reconciliation.recover_stale_team_model_attempt(
                    db, reservation_id=reservation_id, actor_id=owner, note="检查过期调用",
                    now=now + timedelta(minutes=31),
                )

    event.listen(fresh_chain, "before_cursor_execute", observe_recovery)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            sender = pool.submit(run_metered)
            assert in_provider.wait(15), "actual provider guard was never entered"
            with factory() as db:
                reservation = db.query(BillingReservation).filter_by(request_key=key).one()
                assert reservation.status == "reserved" and reservation.provider_attempt_started_at is not None
                reservation_id = reservation.id
            recovery = pool.submit(recover, reservation_id)
            assert recovery_waiting.wait(5), "recovery never reached parent row lock"
            assert not recovery.done(), "recovery passed the actual active provider transaction"
            release_provider.set()
            assert sender.result(timeout=20) == "model response"
            recovery.result(timeout=20)
    finally:
        release_provider.set()
        event.remove(fresh_chain, "before_cursor_execute", observe_recovery)

    with factory() as db:
        reservation = db.get(BillingReservation, reservation_id)
        task = db.get(EnterpriseAgentTeamTask, tasks[0])
        events = db.query(BillingReservationReconciliationEvent).filter_by(reservation_id=reservation_id).all()
        assert sends == [1]
        assert (reservation.status, reservation.usage_state) == ("settled", "reported")
        assert (reservation.enterprise_id, reservation.group_id, reservation.user_id) == (company, group, users[0])
        assert task.status == "running" and task.lease_generation == claim["lease_generation"]
        assert all(event.decision != "recovered" for event in events)
