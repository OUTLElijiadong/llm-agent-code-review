"""Disposable MySQL 8 migration and concurrency tests for detached ownership records."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import inspect
from test_mysql_enterprise_chat_authority import _seed

from app.core.exceptions import ConflictError, ForbiddenError
from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.enterprise_assistant_ownership import EnterpriseAssistantRunOwner, EnterpriseAssistantToolOwner
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import enterprise_assistant_ownership_service as ownership
from app.services import enterprise_assistant_runtime_service as runtime

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.fixture(autouse=True)
def activate_detached_test_primitives(monkeypatch):
    monkeypatch.setattr(ownership, "_FOUNDATION_ACTIVATED", True)


def test_migration_keeps_historical_rows_and_uses_binary_identity(fresh_chain):
    columns = {row["name"]: row for row in inspect(fresh_chain).get_columns("enterprise_assistant_tool_owner")}
    assert {"call_digest", "call_bytes", "request_digest", "owner_digest"}.issubset(columns)
    assert "VARBINARY" in str(columns["call_digest"]["type"]).upper()
    unique = {item["name"] for item in inspect(fresh_chain).get_unique_constraints("enterprise_assistant_tool_owner")}
    assert {"uq_ent_tool_owner_call", "uq_ent_tool_owner_request"}.issubset(unique)
    with fresh_chain.connect() as db:
        assert db.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "122_assistant_call_owner"
        assert db.exec_driver_sql("SELECT COUNT(*) FROM enterprise_assistant_run_owner").scalar_one() == 0
        assert db.exec_driver_sql("SELECT COUNT(*) FROM enterprise_assistant_tool_owner").scalar_one() == 0


def _ready(engine):
    sessions, enterprise, _group, _owner, _author, conversation, public = _seed(engine, include_prior_message=False)
    with sessions.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=public).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        run.status = state.status = "running"
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)
        wallet = BillingWallet(owner_type="enterprise", owner_id=enterprise)
        db.add(wallet)
        db.flush()
        return sessions, enterprise, conversation, int(run.id), int(wallet.id)


def test_two_concurrent_run_claims_have_exactly_one_owner(fresh_chain):
    sessions, enterprise, conversation, run_id, _wallet = _ready(fresh_chain)
    barrier = Barrier(2, timeout=10)

    def claim():
        with sessions.begin() as db:
            barrier.wait()
            try:
                return ownership.claim_run(
                    db, run_id=run_id, enterprise_id=enterprise, conversation_id=conversation, generation=1
                )
            except ConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        secrets = [future.result(timeout=10) for future in futures]
    assert sum(value is not None for value in secrets) == 1
    with sessions() as db:
        assert db.query(EnterpriseAssistantRunOwner).filter_by(assistant_run_id=run_id).count() == 1
        assert db.query(BillingReservation).filter_by(enterprise_id=enterprise).count() == 0


def test_two_tool_claims_and_case_sensitive_identity(fresh_chain):
    sessions, enterprise, conversation, run_id, wallet = _ready(fresh_chain)
    with sessions.begin() as db:
        run_secret = ownership.claim_run(
            db, run_id=run_id, enterprise_id=enterprise, conversation_id=conversation, generation=1
        )
    barrier = Barrier(2, timeout=10)

    def claim():
        with sessions.begin() as db:
            barrier.wait()
            try:
                return ownership.claim_tool(
                    db,
                    run_id=run_id,
                    enterprise_id=enterprise,
                    conversation_id=conversation,
                    generation=1,
                    run_secret=run_secret,
                    call_id="Call-A",
                    tool_name="search",
                    arguments={"query": "内容"},
                    wallet_id=wallet,
                )
            except ConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        secrets = [future.result(timeout=10) for future in futures]
    assert sum(value is not None for value in secrets) == 1
    with sessions.begin() as db:
        other = ownership.claim_tool(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=1,
            run_secret=run_secret,
            call_id="call-a",
            tool_name="search",
            arguments={"query": "内容"},
            wallet_id=wallet,
        )
        assert other is not None
        assert db.query(EnterpriseAssistantToolOwner).filter_by(assistant_run_id=run_id).count() == 2
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).with_for_update().one()
        state.lease_generation += 1
        db.flush()
        with pytest.raises(ForbiddenError):
            ownership.assert_tool_owner(
                db,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=run_secret,
                tool_secret=other,
                call_id="call-a",
                tool_name="search",
                arguments={"query": "内容"},
                wallet_id=wallet,
            )


def test_two_model_claimants_cannot_rebind_round_and_caller_rollback_is_atomic(fresh_chain):
    from app.models.enterprise_assistant_ownership import EnterpriseAssistantModelAttemptOwner

    sessions, enterprise, conversation, run_id, wallet = _ready(fresh_chain)
    with sessions.begin() as db:
        run = db.get(EnterpriseAssistantRun, run_id)
        db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one().billing_invocation_count = 1
        payer, group, public = int(run.initiator_user_id), run.group_id, run.public_id
        parent = ownership.claim_run(
            db, run_id=run_id, enterprise_id=enterprise, conversation_id=conversation, generation=1
        )
    barrier = Barrier(2, timeout=10)

    def execute(index):
        with sessions.begin() as db:
            hold = BillingReservation(
                request_key=f"enterprise-assistant:{public}:1:{index}",
                model_name="deepseek-flash",
                billable_kind="model",
                enterprise_id=enterprise,
                group_id=group,
                user_id=payer,
                usage_kind="member",
                enterprise_wallet_id=wallet,
                personal_wallet_id=None,
                allow_personal=0,
                requested_milli=100,
                status="reserved",
                usage_state="pending",
            )
            db.add(hold)
            barrier.wait()
            try:
                result = ownership.claim_model_attempt(
                    db,
                    run_id=run_id,
                    enterprise_id=enterprise,
                    conversation_id=conversation,
                    generation=1,
                    run_secret=parent,
                    round_key="round:0123456789abcdef0123456789abcdef",
                    payload_digest=b"x" * 32,
                    reservation=hold,
                )
                return result, int(hold.id)
            except ConflictError:
                db.expunge(hold)
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, i) for i in (1, 2)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(result is not None for result in results) == 1
    with sessions.begin() as db:
        assert db.query(EnterpriseAssistantModelAttemptOwner).filter_by(assistant_run_id=run_id).count() == 1
        assert db.query(BillingReservation).filter_by(enterprise_id=enterprise).count() == 1
        secret, rid = next(result for result in results if result is not None)
        ownership.assert_model_attempt(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=1,
            run_secret=parent,
            attempt_secret=secret,
            round_key="round:0123456789abcdef0123456789abcdef",
            payload_digest=b"x" * 32,
            reservation_id=rid,
        )


def test_mysql_model_binding_immutability_checks_and_old_hold_rejection(fresh_chain):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.models.enterprise_assistant_ownership import EnterpriseAssistantModelAttemptOwner

    sessions, enterprise, conversation, run_id, wallet = _ready(fresh_chain)
    with sessions.begin() as db:
        run = db.get(EnterpriseAssistantRun, run_id)
        db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one().billing_invocation_count = 1
        parent = ownership.claim_run(
            db, run_id=run_id, enterprise_id=enterprise, conversation_id=conversation, generation=1
        )
        hold = BillingReservation(
            request_key=f"enterprise-assistant:{run.public_id}:1:1",
            model_name="deepseek-flash",
            billable_kind="model",
            enterprise_id=enterprise,
            group_id=run.group_id,
            user_id=run.initiator_user_id,
            usage_kind="member",
            enterprise_wallet_id=wallet,
            allow_personal=0,
            requested_milli=100,
            status="reserved",
            usage_state="pending",
        )
        db.add(hold)
        secret = ownership.claim_model_attempt(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=1,
            run_secret=parent,
            round_key="round:0123456789abcdef0123456789abcdef",
            payload_digest=b"x" * 32,
            reservation=hold,
        )
        rid = hold.id
    with sessions.begin() as db:
        hold = db.get(BillingReservation, rid)
        with pytest.raises(ForbiddenError):
            ownership.claim_model_attempt(
                db,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=parent,
                round_key="round:1123456789abcdef0123456789abcdef",
                payload_digest=b"x" * 32,
                reservation=hold,
            )
        ownership.assert_model_attempt(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=1,
            run_secret=parent,
            attempt_secret=secret,
            round_key="round:0123456789abcdef0123456789abcdef",
            payload_digest=b"x" * 32,
            reservation_id=rid,
        )
    with fresh_chain.begin() as db:
        binding_id = db.execute(
            text("SELECT id FROM enterprise_assistant_model_attempt_owner WHERE reservation_id=:rid"), {"rid": rid}
        ).scalar_one()
        for statement in (
            "UPDATE enterprise_assistant_model_attempt_owner SET generation=2 WHERE id=:id",
            "DELETE FROM enterprise_assistant_model_attempt_owner WHERE id=:id",
        ):
            with pytest.raises(DBAPIError) as failure:
                db.execute(text(statement), {"id": binding_id})
            assert failure.value.orig.args[0] == 1644
    with sessions() as db:
        assert db.query(EnterpriseAssistantModelAttemptOwner).filter_by(reservation_id=rid).count() == 1


def test_model_binding_lock_blocks_generation_change_and_stale_snapshot_is_rejected(fresh_chain):
    from threading import Event

    sessions, enterprise, conversation, run_id, wallet = _ready(fresh_chain)
    with sessions.begin() as db:
        run = db.get(EnterpriseAssistantRun, run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one()
        state.billing_invocation_count = 1
        parent = ownership.claim_run(
            db, run_id=run_id, enterprise_id=enterprise, conversation_id=conversation, generation=1
        )
        hold = BillingReservation(
            request_key=f"enterprise-assistant:{run.public_id}:1:1",
            model_name="deepseek-flash",
            billable_kind="model",
            enterprise_id=enterprise,
            group_id=run.group_id,
            user_id=run.initiator_user_id,
            usage_kind="member",
            enterprise_wallet_id=wallet,
            allow_personal=0,
            requested_milli=100,
            status="reserved",
            usage_state="pending",
        )
        db.add(hold)
        secret = ownership.claim_model_attempt(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=1,
            run_secret=parent,
            round_key="round:0123456789abcdef0123456789abcdef",
            payload_digest=b"x" * 32,
            reservation=hold,
        )
        rid = int(hold.id)
    locked, allow_commit, writer_done = Event(), Event(), Event()

    def verify_then_wait():
        with sessions.begin() as db:
            ownership.assert_model_attempt(
                db,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=parent,
                attempt_secret=secret,
                round_key="round:0123456789abcdef0123456789abcdef",
                payload_digest=b"x" * 32,
                reservation_id=rid,
            )
            locked.set()
            assert allow_commit.wait(10)

    def revoke():
        with sessions.begin() as db:
            db.query(EnterpriseAssistantRun).filter_by(id=run_id).with_for_update().one()
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).with_for_update().one()
            state.lease_generation = 2
        writer_done.set()

    with sessions() as stale:
        old_state = stale.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one()
        assert old_state.lease_generation == 1
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(verify_then_wait)
            assert locked.wait(5)
            second = pool.submit(revoke)
            assert not writer_done.wait(0.5)
            allow_commit.set()
            first.result(timeout=10)
            second.result(timeout=10)
        with pytest.raises(ForbiddenError):
            ownership.assert_model_attempt(
                stale,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=parent,
                attempt_secret=secret,
                round_key="round:0123456789abcdef0123456789abcdef",
                payload_digest=b"x" * 32,
                reservation_id=rid,
            )
