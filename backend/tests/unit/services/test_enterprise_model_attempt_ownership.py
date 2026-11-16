"""Detached attempt bindings: a test fixture is never a reserve/charge authorization."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import ConflictError, ForbiddenError
from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.enterprise_assistant_ownership import EnterpriseAssistantRunOwner
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.models.user import User
from app.services import enterprise_assistant_ownership_service as owners


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setattr(owners, "_FOUNDATION_ACTIVATED", True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            EnterpriseAssistantRun.__table__,
            EnterpriseAssistantRuntimeState.__table__,
            BillingWallet.__table__,
            BillingReservation.__table__,
            EnterpriseAssistantRunOwner.__table__,
            Base.metadata.tables["enterprise_assistant_model_attempt_owner"],
        ],
    )
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with make.begin() as db:
        db.add(
            EnterpriseAssistantRun(
                id=41,
                public_id="run-a",
                request_key="run-a",
                enterprise_id=1,
                conversation_id=2,
                group_id=3,
                initiator_user_id=4,
                source_message_id=5,
                status="running",
                prompt_sha256="a" * 64,
            )
        )
        db.add(
            EnterpriseAssistantRuntimeState(
                assistant_run_id=41,
                enterprise_id=1,
                conversation_id=2,
                group_id=3,
                initiator_user_id=4,
                source_message_id=5,
                status="running",
                permission_snapshot_sha256="a" * 64,
                lease_generation=7,
                billing_invocation_count=1,
                lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            )
        )
        db.add(BillingWallet(id=9, owner_type="enterprise", owner_id=1))
    yield make
    engine.dispose()


def reservation(key="enterprise-assistant:run-a:1:1", **changes):
    data = dict(
        request_key=key,
        model_name="deepseek-flash",
        billable_kind="model",
        enterprise_id=1,
        group_id=3,
        user_id=4,
        usage_kind="member",
        enterprise_wallet_id=9,
        personal_wallet_id=None,
        allow_personal=0,
        requested_milli=100,
        status="reserved",
        usage_state="pending",
    )
    data.update(changes)
    return BillingReservation(**data)


def claim(db, run_secret, row, *, round_key="round:0123456789abcdef0123456789abcdef", payload_digest=b"x" * 32):
    return owners.claim_model_attempt(
        db,
        run_id=41,
        enterprise_id=1,
        conversation_id=2,
        generation=7,
        run_secret=run_secret,
        round_key=round_key,
        payload_digest=payload_digest,
        reservation=row,
    )


def verify(
    db,
    run_secret,
    attempt_secret,
    row_id,
    *,
    round_key="round:0123456789abcdef0123456789abcdef",
    payload_digest=b"x" * 32,
):
    return owners.assert_model_attempt(
        db,
        run_id=41,
        enterprise_id=1,
        conversation_id=2,
        generation=7,
        run_secret=run_secret,
        attempt_secret=attempt_secret,
        round_key=round_key,
        payload_digest=payload_digest,
        reservation_id=row_id,
    )


def test_pending_attempt_binding_is_atomic_one_shot_and_checks_identity(factory):
    from app.models.enterprise_assistant_ownership import EnterpriseAssistantModelAttemptOwner

    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        secret = claim(db, parent, hold)
        rid = hold.id
        verify(db, parent, secret, rid)
        binding = db.query(EnterpriseAssistantModelAttemptOwner).one()
        assert binding.reservation_id == rid and binding.owner_digest != secret._value
        assert b"x" * 32 not in binding.owner_digest
        with pytest.raises(ForbiddenError):
            verify(db, parent, owners.OwnerSecret(b"0" * 32), rid)
        with pytest.raises(ForbiddenError):
            verify(db, parent, secret, rid, payload_digest=b"y" * 32)
    with factory.begin() as db:
        duplicate = reservation("enterprise-assistant:run-a:1:2")
        db.add(duplicate)
        with pytest.raises(ConflictError):
            claim(db, parent, duplicate)
        db.expunge(duplicate)
        assert db.query(BillingReservation).count() == 1


def test_existing_and_flushed_reservations_are_never_rebound(factory):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        old = reservation()
        db.add(old)
        db.flush()
        with pytest.raises(ForbiddenError):
            claim(db, parent, old)
    with factory.begin() as db:
        with pytest.raises(ForbiddenError):
            claim(db, parent, db.query(BillingReservation).one())


@pytest.mark.parametrize(
    "changes",
    [
        {"enterprise_id": 99},
        {"group_id": 99},
        {"user_id": 99},
        {"enterprise_wallet_id": 99},
        {"personal_wallet_id": 10},
        {"allow_personal": 1},
        {"usage_kind": "enterprise_auto"},
        {"billable_kind": "search"},
        {"status": "unknown"},
        {"provider_attempt_started_at": datetime(2026, 1, 1)},
        {"request_key": "enterprise-assistant:another-run:1:1"},
    ],
)
def test_rejects_untrusted_or_mismatched_pending_reservation(factory, changes):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation(**changes)
        db.add(hold)
        with pytest.raises(ForbiddenError):
            claim(db, parent, hold)
        db.expunge(hold)
        assert db.query(BillingReservation).count() == 0


def test_rollback_removes_new_reservation_and_attempt(factory):
    from app.models.enterprise_assistant_ownership import EnterpriseAssistantModelAttemptOwner

    with factory() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        claim(db, parent, hold)
        db.rollback()
    with factory() as db:
        assert db.query(BillingReservation).count() == 0
        assert db.query(EnterpriseAssistantModelAttemptOwner).count() == 0


def test_persisted_hold_identity_change_blocks_further_use(factory):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        secret = claim(db, parent, hold)
        hold_id = hold.id
    with factory.begin() as db:
        db.get(BillingReservation, hold_id).requested_milli = 200
    with factory.begin() as db:
        with pytest.raises(ForbiddenError):
            verify(db, parent, secret, hold_id)


def test_new_generation_cannot_claim_an_existing_round(factory):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        claim(db, parent, hold)
    with factory.begin() as db:
        state = db.query(EnterpriseAssistantRuntimeState).one()
        state.lease_generation = 8
        state.billing_invocation_count = 2
        db.flush()
        next_parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=8)
        second = reservation("enterprise-assistant:run-a:2:1")
        db.add(second)
        with pytest.raises(ConflictError):
            owners.claim_model_attempt(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=8,
                run_secret=next_parent,
                round_key="round:0123456789abcdef0123456789abcdef",
                payload_digest=b"x" * 32,
                reservation=second,
            )
        db.expunge(second)
        assert db.query(BillingReservation).count() == 1


def test_round_id_must_be_opaque_and_cannot_store_prompt(factory):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        with pytest.raises(ForbiddenError):
            claim(db, parent, hold, round_key="请分析这段私密提示词")
        db.expunge(hold)
        assert db.query(BillingReservation).count() == 0


def test_database_rejects_attempt_rebind_and_deletion(factory):
    import importlib.util
    from pathlib import Path
    from unittest.mock import patch

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    path = Path(__file__).resolve().parents[3] / "alembic/versions/122_assistant_call_owner.py"
    spec = importlib.util.spec_from_file_location("owner_122_triggers", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
            migration._install_model_attempt_immutability()
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        claim(db, parent, hold)
    with engine.begin() as connection:
        for sql in (
            "UPDATE enterprise_assistant_model_attempt_owner SET generation=8",
            "DELETE FROM enterprise_assistant_model_attempt_owner",
        ):
            with pytest.raises(IntegrityError, match="immutable"):
                connection.execute(text(sql))


@pytest.mark.asyncio
async def test_checkpoint_retry_round_count_is_not_a_durable_attempt_identity():
    from app.services.deepseek_responses_runtime import (
        FAILED,
        DeepSeekResponsesRuntime,
        InMemoryCheckpointStore,
        RunCheckpoint,
        RuntimeResult,
    )

    store = InMemoryCheckpointStore()
    await store.create(RunCheckpoint(run_id="retry", model="test", transcript=[], tools=[], status=FAILED, rounds=5))
    seen = []

    class Runtime(DeepSeekResponsesRuntime):
        async def _drive(self, checkpoint):
            seen.append(checkpoint.rounds)
            return RuntimeResult(run_id=checkpoint.run_id, status=checkpoint.status)

    await Runtime(transport=object(), tool_executor=object(), checkpoint_store=store).retry("retry")
    assert seen == [0]


def test_duplicate_request_insert_rolls_back_whole_caller_transaction(factory):
    from sqlalchemy.exc import IntegrityError

    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        old = reservation()
        db.add(old)
        db.flush()
    with factory() as db:
        hold = reservation()
        db.add(hold)
        with pytest.raises(IntegrityError):
            claim(db, parent, hold, round_key="round:1123456789abcdef0123456789abcdef")
        db.rollback()
    with factory() as db:
        assert db.query(BillingReservation).count() == 1


def test_model_binding_gate_is_closed_by_default(factory, monkeypatch):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        monkeypatch.setattr(owners, "_FOUNDATION_ACTIVATED", False)
        with pytest.raises(ForbiddenError, match="尚未启用"):
            claim(db, parent, hold)
        db.expunge(hold)
        assert db.query(BillingReservation).count() == 0


def test_real_reserve_pending_defaults_are_not_yet_bindable(factory, monkeypatch):
    """A hook before ledger.reserve's flush alone is insufficient for integration."""
    from fractions import Fraction

    from app.services import billing_ledger_service as ledger

    class Inspected(Exception):
        pass

    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
    with factory() as db:
        monkeypatch.setattr(ledger, "validate_billing_provider_identity", lambda value: value)
        monkeypatch.setattr(ledger, "_rate_snapshot", lambda *_a, **_k: ("v1", Fraction(1, 1), Fraction(1, 1)))
        monkeypatch.setattr(ledger, "_wallet_snapshot_version", lambda *_a, **_k: 1)
        monkeypatch.setattr(
            ledger, "_lock_wallets", lambda session, ids: {i: session.get(BillingWallet, i) for i in ids}
        )
        monkeypatch.setattr(ledger, "_limits", lambda *_a, **_k: [])
        monkeypatch.setattr(ledger, "available_milli", lambda *_a, **_k: 1000)
        original_flush = db.flush

        def inspect_flush(*args, **kwargs):
            pending = [row for row in db.new if isinstance(row, BillingReservation)]
            if pending:
                assert len(pending) == 1
                hold = pending[0]
                assert hold.billable_kind is None and hold.usage_state is None
                with pytest.raises(ForbiddenError):
                    claim(db, parent, hold)
                raise Inspected()
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(db, "flush", inspect_flush)
        with pytest.raises(Inspected):
            ledger.reserve(
                db,
                request_key="enterprise-assistant:run-a:1:1",
                model_name="deepseek-flash",
                user_id=4,
                max_milli=100,
                enterprise_wallet_id=9,
                enterprise_id=1,
                group_id=3,
                allow_personal=False,
                usage_kind="member",
                require_new=True,
                provider_identity=ledger.BillingProviderIdentity(
                    identity_version="deepseek-official-v1",
                    provider_name="deepseek",
                    endpoint_host="api.deepseek.com",
                    credential_source="system",
                ),
            )
        db.rollback()
    with factory() as db:
        assert db.query(BillingReservation).count() == 0


@pytest.mark.parametrize("autoflush", [False, True])
def test_claim_model_does_not_discard_pending_run_changes(factory, autoflush):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
    with factory(autoflush=autoflush) as db:
        run = db.get(EnterpriseAssistantRun, 41)
        run.response_text = "pending-change"
        hold = reservation()
        db.add(hold)
        with pytest.raises(ForbiddenError, match="未刷新"):
            claim(db, parent, hold)
        assert run.response_text == "pending-change"
        db.expunge(hold)
        db.commit()
    with factory() as db:
        assert db.get(EnterpriseAssistantRun, 41).response_text == "pending-change"


@pytest.mark.parametrize("autoflush", [False, True])
def test_assert_model_does_not_discard_pending_reservation_changes(factory, autoflush):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        hold = reservation()
        db.add(hold)
        secret = claim(db, parent, hold)
        rid = hold.id
    with factory(autoflush=autoflush) as db:
        hold = db.get(BillingReservation, rid)
        hold.requested_milli = 200
        with pytest.raises(ForbiddenError, match="未刷新"):
            verify(db, parent, secret, rid)
        assert hold.requested_milli == 200
        db.commit()
    with factory() as db:
        assert db.get(BillingReservation, rid).requested_milli == 200


def test_valid_pending_reservation_works_with_autoflush_enabled(factory):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
    with factory(autoflush=True) as db:
        hold = reservation()
        db.add(hold)
        secret = claim(db, parent, hold)
        rid = hold.id
        db.commit()
    with factory.begin() as db:
        verify(db, parent, secret, rid)


@pytest.mark.parametrize("operation", ["claim", "assert"])
@pytest.mark.parametrize("target", ["run", "state", "wallet", "reservation"])
@pytest.mark.parametrize("autoflush", [False, True])
def test_model_operations_preserve_changed_scope_identity(factory, operation, target, autoflush):
    if operation == "claim" and target == "reservation":
        pytest.skip("claim requires a new reservation rather than an existing reservation id")
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        if operation == "assert":
            hold = reservation()
            db.add(hold)
            secret = claim(db, parent, hold)
            rid = hold.id
    with factory(autoflush=autoflush) as db:
        if target == "run":
            row, field, changed = db.get(EnterpriseAssistantRun, 41), "id", 42
        elif target == "state":
            row = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).one()
            field, changed = "assistant_run_id", 42
        elif target == "wallet":
            row, field, changed = db.get(BillingWallet, 9), "id", 10
        else:
            row, field, changed = db.get(BillingReservation, rid), "id", rid + 100
        setattr(row, field, changed)
        if operation == "claim":
            hold = reservation()
            db.add(hold)
        with pytest.raises(ForbiddenError, match="未刷新"):
            if operation == "claim":
                claim(db, parent, hold)
            else:
                verify(db, parent, secret, rid)
        assert getattr(row, field) == changed and row in db.dirty
        if operation == "claim":
            assert hold in db.new
        db.rollback()
    with factory() as db:
        assert db.get(EnterpriseAssistantRun, 41) is not None
        assert db.query(EnterpriseAssistantRuntimeState).one().assistant_run_id == 41
        assert db.get(BillingWallet, 9) is not None


@pytest.mark.parametrize("operation", ["claim", "assert"])
@pytest.mark.parametrize("autoflush", [False, True])
def test_model_operations_do_not_flush_unrelated_pending_object(factory, operation, autoflush):
    with factory.begin() as db:
        parent = owners.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        if operation == "assert":
            hold = reservation()
            db.add(hold)
            secret = claim(db, parent, hold)
            rid = hold.id
    with factory(autoflush=autoflush) as db:
        unrelated = User(username="unrelated-pending", password="x", role="user", status=1)
        db.add(unrelated)
        if operation == "claim":
            hold = reservation()
            db.add(hold)
        with pytest.raises(ForbiddenError, match="未刷新"):
            if operation == "claim":
                claim(db, parent, hold)
            else:
                verify(db, parent, secret, rid)
        assert unrelated in db.new and unrelated.id is None
        if operation == "claim":
            assert hold in db.new
        db.rollback()
