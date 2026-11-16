"""Detached ownership primitives: no caller or billing path is enabled by these tests."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import ConflictError, ForbiddenError
from app.models.billing_ledger import BillingWallet
from app.models.enterprise_assistant_ownership import EnterpriseAssistantRunOwner, EnterpriseAssistantToolOwner
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.models.user import User
from app.services import enterprise_assistant_ownership_service as ownership


@pytest.fixture
def owner_db(monkeypatch):
    monkeypatch.setattr(ownership, "_FOUNDATION_ACTIVATED", True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            EnterpriseAssistantRun.__table__,
            EnterpriseAssistantRuntimeState.__table__,
            BillingWallet.__table__,
            EnterpriseAssistantRunOwner.__table__,
            EnterpriseAssistantToolOwner.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
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
                lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            )
        )
        db.add(BillingWallet(id=9, owner_type="enterprise", owner_id=1))
    yield factory
    engine.dispose()


def test_ownership_is_one_shot_scoped_and_does_not_store_secrets(owner_db):
    with owner_db.begin() as db:
        run_secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        tool_secret = ownership.claim_tool(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=run_secret,
            call_id="Call-A",
            tool_name="search",
            arguments={"query": " 搜索 "},
            wallet_id=9,
        )
        ownership.assert_tool_owner(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=run_secret,
            tool_secret=tool_secret,
            call_id="Call-A",
            tool_name="search",
            arguments={"query": "搜索"},
            wallet_id=9,
        )
        run_row, tool_row = db.query(EnterpriseAssistantRunOwner).one(), db.query(EnterpriseAssistantToolOwner).one()
        assert run_row.owner_digest != tool_row.owner_digest
        assert len(run_row.owner_digest) == len(tool_row.owner_digest) == 32
        assert "搜索" not in repr(run_secret) and "搜索" not in repr(tool_secret)
    with owner_db.begin() as db:
        with pytest.raises(ConflictError):
            ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        with pytest.raises(ConflictError):
            ownership.claim_tool(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                run_secret=run_secret,
                call_id="Call-A",
                tool_name="search",
                arguments={"query": "搜索"},
                wallet_id=9,
            )
        # Exact UTF-8 identity is not MySQL text collation equality.
        other = ownership.claim_tool(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=run_secret,
            call_id="call-a",
            tool_name="search",
            arguments={"query": "搜索"},
            wallet_id=9,
        )
        assert repr(other) == repr(tool_secret)


def test_tampering_stale_generation_and_changed_binding_fail_closed(owner_db):
    with owner_db.begin() as db:
        run_secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        tool_secret = ownership.claim_tool(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=run_secret,
            call_id="call",
            tool_name="search",
            arguments={"query": "a"},
            wallet_id=9,
        )
        for kwargs in ({"call_id": "CALL"}, {"arguments": {"query": "b"}}, {"wallet_id": 10}):
            args = dict(
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                run_secret=run_secret,
                tool_secret=tool_secret,
                call_id="call",
                tool_name="search",
                arguments={"query": "a"},
                wallet_id=9,
            )
            args.update(kwargs)
            with pytest.raises((ForbiddenError, ConflictError)):
                ownership.assert_tool_owner(db, **args)
        with pytest.raises(ForbiddenError):
            ownership.assert_run_owner(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                secret=ownership.OwnerSecret(b"\x00" * 32),
            )
        db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).one().lease_generation = 8
        db.flush()
        with pytest.raises(ForbiddenError):
            ownership.assert_tool_owner(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                run_secret=run_secret,
                tool_secret=tool_secret,
                call_id="call",
                tool_name="search",
                arguments={"query": "a"},
                wallet_id=9,
            )


def test_rolled_back_secret_is_not_authority(owner_db):
    with owner_db() as db:
        run_secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        db.rollback()
    with owner_db.begin() as db:
        with pytest.raises(ForbiddenError):
            ownership.assert_run_owner(
                db, run_id=41, enterprise_id=1, conversation_id=2, generation=7, secret=run_secret
            )


def test_call_digest_hit_must_match_exact_bytes_and_no_argument_plaintext(owner_db):
    with owner_db.begin() as db:
        run_secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        tool_secret = ownership.claim_tool(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=run_secret,
            call_id="Call-A",
            tool_name="search",
            arguments={"query": "sensitive-query"},
            wallet_id=9,
        )
        row = db.query(EnterpriseAssistantToolOwner).one()
        assert not hasattr(row, "arguments_bytes")
        assert b"sensitive-query" not in row.arguments_digest
        # Simulate a digest collision/corrupt binding: exact stored bytes must still match.
        row.call_digest = ownership._digest(b"Call-B")
        db.flush()
        with pytest.raises(ForbiddenError):
            ownership.assert_tool_owner(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                run_secret=run_secret,
                tool_secret=tool_secret,
                call_id="Call-B",
                tool_name="search",
                arguments={"query": "sensitive-query"},
                wallet_id=9,
            )


def test_new_generation_cannot_reclaim_same_call_or_use_old_capability(owner_db):
    with owner_db.begin() as db:
        first = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        old_tool = ownership.claim_tool(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=first,
            call_id="call",
            tool_name="search",
            arguments={"query": "a"},
            wallet_id=9,
        )
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).one()
        state.lease_generation = 8
        db.flush()
        second = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=8)
        with pytest.raises(ForbiddenError):
            ownership.assert_run_owner(db, run_id=41, enterprise_id=1, conversation_id=2, generation=8, secret=first)
        with pytest.raises(ConflictError):
            ownership.claim_tool(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=8,
                run_secret=second,
                call_id="call",
                tool_name="search",
                arguments={"query": "different"},
                wallet_id=9,
            )
        assert old_tool is not None


@pytest.mark.parametrize("condition", ["cancelled", "expired", "wrong_scope"])
def test_invalid_or_stale_run_cannot_get_owner(owner_db, condition):
    with owner_db.begin() as db:
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).one()
        if condition == "cancelled":
            state.stop_requested = True
        elif condition == "expired":
            state.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.flush()
        with pytest.raises(ForbiddenError):
            ownership.claim_run(
                db, run_id=41, enterprise_id=99 if condition == "wrong_scope" else 1, conversation_id=2, generation=7
            )
        assert db.query(EnterpriseAssistantRunOwner).count() == 0


def test_foundation_is_disabled_by_default(owner_db, monkeypatch):
    monkeypatch.setattr(ownership, "_FOUNDATION_ACTIVATED", False)
    with owner_db.begin() as db:
        with pytest.raises(ForbiddenError, match="尚未启用"):
            ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        assert db.query(EnterpriseAssistantRunOwner).count() == 0


def test_stored_digests_are_not_reusable_as_owner_capabilities(owner_db):
    with owner_db.begin() as db:
        run_secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        tool_secret = ownership.claim_tool(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=run_secret,
            call_id="proof",
            tool_name="search",
            arguments={"query": "a"},
            wallet_id=9,
        )
        run_digest = bytes(db.query(EnterpriseAssistantRunOwner).one().owner_digest)
        tool_digest = bytes(db.query(EnterpriseAssistantToolOwner).one().owner_digest)
        with pytest.raises(ForbiddenError):
            ownership.assert_run_owner(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                secret=ownership.OwnerSecret(run_digest),
            )
        with pytest.raises(ForbiddenError):
            ownership.assert_tool_owner(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                run_secret=run_secret,
                tool_secret=ownership.OwnerSecret(tool_digest),
                call_id="proof",
                tool_name="search",
                arguments={"query": "a"},
                wallet_id=9,
            )
        assert tool_secret is not None


@pytest.mark.parametrize("operation", ["claim_run", "claim_tool", "assert_run", "assert_tool"])
def test_sqlite_scope_check_and_claim_serialize_against_generation_change(tmp_path, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    monkeypatch.setattr(ownership, "_FOUNDATION_ACTIVATED", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'owner-race.db'}", connect_args={"timeout": 5})
    Base.metadata.create_all(
        engine,
        tables=[
            EnterpriseAssistantRun.__table__,
            EnterpriseAssistantRuntimeState.__table__,
            BillingWallet.__table__,
            EnterpriseAssistantRunOwner.__table__,
            EnterpriseAssistantToolOwner.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add(
            EnterpriseAssistantRun(
                id=41,
                public_id="run-race",
                request_key="run-race",
                enterprise_id=1,
                conversation_id=2,
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
                initiator_user_id=4,
                source_message_id=5,
                status="running",
                permission_snapshot_sha256="a" * 64,
                lease_generation=7,
                lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            )
        )
        db.add(BillingWallet(id=9, owner_type="enterprise", owner_id=1))
    run_secret = tool_secret = None
    if operation != "claim_run":
        with factory.begin() as db:
            run_secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
            if operation == "assert_tool":
                tool_secret = ownership.claim_tool(
                    db,
                    run_id=41,
                    enterprise_id=1,
                    conversation_id=2,
                    generation=7,
                    run_secret=run_secret,
                    call_id="proof",
                    tool_name="search",
                    arguments={"query": "a"},
                    wallet_id=9,
                )
    reached, release, changed = Event(), Event(), Event()
    original = ownership._locked_scope

    def hold_after_scope(*args, **kwargs):
        result = original(*args, **kwargs)
        reached.set()
        if not release.wait(5):
            raise TimeoutError("claim barrier timed out")
        return result

    monkeypatch.setattr(ownership, "_locked_scope", hold_after_scope)

    def claim():
        with factory.begin() as db:
            try:
                if operation == "claim_run":
                    return ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
                if operation == "claim_tool":
                    return ownership.claim_tool(
                        db,
                        run_id=41,
                        enterprise_id=1,
                        conversation_id=2,
                        generation=7,
                        run_secret=run_secret,
                        call_id="new",
                        tool_name="search",
                        arguments={"query": "a"},
                        wallet_id=9,
                    )
                if operation == "assert_run":
                    ownership.assert_run_owner(
                        db, run_id=41, enterprise_id=1, conversation_id=2, generation=7, secret=run_secret
                    )
                else:
                    ownership.assert_tool_owner(
                        db,
                        run_id=41,
                        enterprise_id=1,
                        conversation_id=2,
                        generation=7,
                        run_secret=run_secret,
                        tool_secret=tool_secret,
                        call_id="proof",
                        tool_name="search",
                        arguments={"query": "a"},
                        wallet_id=9,
                    )
                return "authorized"
            except ForbiddenError:
                return None

    def change():
        with factory.begin() as db:
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).one()
            state.lease_generation = 8
        changed.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(claim)
            assert reached.wait(5)
            second = pool.submit(change)
            won_before_claim = changed.wait(1)
            release.set()
            claim_result = first.result(timeout=5)
            second.result(timeout=5)
        with factory() as db:
            row_count = db.query(EnterpriseAssistantRunOwner).count()
            assert db.query(EnterpriseAssistantRuntimeState).one().lease_generation == 8
            if won_before_claim:
                assert claim_result is None
                assert row_count == (0 if operation == "claim_run" else 1)
            else:
                assert claim_result is not None and row_count == 1
                old_secret = claim_result if operation == "claim_run" else run_secret
                with pytest.raises(ForbiddenError):
                    ownership.assert_run_owner(
                        db, run_id=41, enterprise_id=1, conversation_id=2, generation=7, secret=old_secret
                    )
    finally:
        release.set()
        engine.dispose()


@pytest.fixture
def wal_owner_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ownership, "_FOUNDATION_ACTIVATED", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'owner-wal.db'}", connect_args={"timeout": 2})
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(
        engine,
        tables=[
            EnterpriseAssistantRun.__table__,
            EnterpriseAssistantRuntimeState.__table__,
            BillingWallet.__table__,
            EnterpriseAssistantRunOwner.__table__,
            EnterpriseAssistantToolOwner.__table__,
        ],
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE external_work (value TEXT NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE state_updates (value TEXT NOT NULL)")
        connection.exec_driver_sql(
            "CREATE TRIGGER watch_state AFTER UPDATE ON enterprise_assistant_runtime_state "
            "BEGIN INSERT INTO state_updates(value) VALUES ('updated'); END"
        )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add(
            EnterpriseAssistantRun(
                id=41,
                public_id="wal-run",
                request_key="wal-run",
                enterprise_id=1,
                conversation_id=2,
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
                initiator_user_id=4,
                source_message_id=5,
                status="running",
                permission_snapshot_sha256="a" * 64,
                lease_generation=7,
                lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            )
        )
    yield factory
    engine.dispose()


def test_sqlite_existing_stale_wal_snapshot_fails_closed(wal_owner_db):
    from sqlalchemy.exc import OperationalError

    with wal_owner_db() as prior:
        prior.connection().exec_driver_sql("BEGIN")
        assert prior.query(EnterpriseAssistantRuntimeState).one().lease_generation == 7
        with wal_owner_db.begin() as writer:
            writer.query(EnterpriseAssistantRuntimeState).one().lease_generation = 8
        with pytest.raises(OperationalError) as error:
            ownership.claim_run(prior, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        assert getattr(error.value.orig, "sqlite_errorcode", None) == 517  # SQLITE_BUSY_SNAPSHOT
        prior.rollback()
    with wal_owner_db() as db:
        assert db.query(EnterpriseAssistantRunOwner).count() == 0
        assert db.query(EnterpriseAssistantRuntimeState).one().lease_generation == 8


def test_sqlite_existing_caller_write_is_not_committed_and_no_update_trigger(wal_owner_db):
    with wal_owner_db() as db:
        db.connection().exec_driver_sql("INSERT INTO external_work(value) VALUES ('pending')")
        ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        assert db.connection().exec_driver_sql("SELECT COUNT(*) FROM external_work").scalar_one() == 1
        assert db.connection().exec_driver_sql("SELECT COUNT(*) FROM state_updates").scalar_one() == 0
        db.rollback()
    with wal_owner_db() as db:
        assert db.connection().exec_driver_sql("SELECT COUNT(*) FROM external_work").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT COUNT(*) FROM state_updates").scalar_one() == 0
        assert db.query(EnterpriseAssistantRunOwner).count() == 0


def test_sqlite_pending_caller_orm_write_is_never_committed(owner_db):
    with owner_db() as db:
        pending = BillingWallet(id=10, owner_type="enterprise", owner_id=2)
        db.add(pending)
        with pytest.raises(ForbiddenError, match="未刷新"):
            ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        assert pending in db.new
        db.rollback()
    with owner_db() as db:
        assert db.get(BillingWallet, 10) is None
        assert db.query(EnterpriseAssistantRunOwner).count() == 0


def test_send_authority_guard_is_not_a_normal_cancel_accounting_guard(owner_db):
    with owner_db.begin() as db:
        run_secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
    with owner_db.begin() as db:
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).one()
        state.stop_requested = True
        state.lease_generation = 8
        db.flush()
        with pytest.raises(ForbiddenError):
            ownership.assert_run_owner(
                db,
                run_id=41,
                enterprise_id=1,
                conversation_id=2,
                generation=7,
                secret=run_secret,
            )


@pytest.mark.parametrize("operation", ["claim", "assert"])
@pytest.mark.parametrize("autoflush", [False, True])
def test_tool_operations_do_not_discard_caller_pending_run_changes(owner_db, operation, autoflush):
    with owner_db.begin() as db:
        parent = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        prior = ownership.claim_tool(
            db,
            run_id=41,
            enterprise_id=1,
            conversation_id=2,
            generation=7,
            run_secret=parent,
            call_id="prior",
            tool_name="search",
            arguments={"query": "a"},
            wallet_id=9,
        )
    with owner_db(autoflush=autoflush) as db:
        run = db.get(EnterpriseAssistantRun, 41)
        run.response_text = "pending-change"
        with pytest.raises(ForbiddenError, match="未刷新"):
            if operation == "claim":
                ownership.claim_tool(
                    db,
                    run_id=41,
                    enterprise_id=1,
                    conversation_id=2,
                    generation=7,
                    run_secret=parent,
                    call_id="new",
                    tool_name="search",
                    arguments={"query": "a"},
                    wallet_id=9,
                )
            else:
                ownership.assert_tool_owner(
                    db,
                    run_id=41,
                    enterprise_id=1,
                    conversation_id=2,
                    generation=7,
                    run_secret=parent,
                    tool_secret=prior,
                    call_id="prior",
                    tool_name="search",
                    arguments={"query": "a"},
                    wallet_id=9,
                )
        assert run.response_text == "pending-change"
        db.commit()
    with owner_db() as db:
        assert db.get(EnterpriseAssistantRun, 41).response_text == "pending-change"


@pytest.mark.parametrize("operation", ["claim", "assert"])
@pytest.mark.parametrize("autoflush", [False, True])
def test_run_operations_do_not_discard_caller_pending_changes(owner_db, operation, autoflush):
    parent = None
    if operation == "assert":
        with owner_db.begin() as db:
            parent = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
    with owner_db(autoflush=autoflush) as db:
        run = db.get(EnterpriseAssistantRun, 41)
        run.response_text = "pending-change"
        with pytest.raises(ForbiddenError, match="未刷新"):
            if operation == "claim":
                ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
            else:
                ownership.assert_run_owner(
                    db, run_id=41, enterprise_id=1, conversation_id=2, generation=7, secret=parent
                )
        assert run.response_text == "pending-change"
        db.commit()
    with owner_db() as db:
        assert db.get(EnterpriseAssistantRun, 41).response_text == "pending-change"


def test_flushed_new_generation_and_run_owner_share_caller_transaction(owner_db):
    with owner_db() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(id=41).with_for_update().one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).with_for_update().one()
        state.lease_generation = 8
        state.billing_invocation_count = 2
        run.attempt_count = 2
        db.flush()
        secret = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=8)
        ownership.assert_run_owner(db, run_id=41, enterprise_id=1, conversation_id=2, generation=8, secret=secret)
        db.rollback()
    with owner_db() as db:
        assert db.query(EnterpriseAssistantRuntimeState).one().lease_generation == 7
        assert db.query(EnterpriseAssistantRunOwner).count() == 0


@pytest.mark.parametrize("operation", ["claim_run", "assert_run", "claim_tool", "assert_tool"])
@pytest.mark.parametrize("target", ["run", "state"])
@pytest.mark.parametrize("autoflush", [False, True])
def test_changed_scope_identity_is_never_overwritten(owner_db, operation, target, autoflush):
    parent = tool = None
    if operation != "claim_run":
        with owner_db.begin() as db:
            parent = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
            if operation == "assert_tool":
                tool = ownership.claim_tool(
                    db, run_id=41, enterprise_id=1, conversation_id=2, generation=7,
                    run_secret=parent, call_id="old", tool_name="search", arguments={"query": "a"}, wallet_id=9,
                )
    with owner_db(autoflush=autoflush) as db:
        if target == "run":
            row, field = db.get(EnterpriseAssistantRun, 41), "id"
        else:
            row, field = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=41).one(), "assistant_run_id"
        setattr(row, field, 42)
        common = dict(run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        with pytest.raises(ForbiddenError, match="未刷新"):
            if operation == "claim_run":
                ownership.claim_run(db, **common)
            elif operation == "assert_run":
                ownership.assert_run_owner(db, **common, secret=parent)
            else:
                tool_args = dict(run_secret=parent, call_id="old" if tool else "new", tool_name="search",
                                 arguments={"query": "a"}, wallet_id=9)
                if tool:
                    ownership.assert_tool_owner(db, **common, tool_secret=tool, **tool_args)
                else:
                    ownership.claim_tool(db, **common, **tool_args)
        assert getattr(row, field) == 42 and row in db.dirty
        db.rollback()
    with owner_db() as db:
        assert db.get(EnterpriseAssistantRun, 41) is not None
        assert db.query(EnterpriseAssistantRuntimeState).one().assistant_run_id == 41


@pytest.mark.parametrize("operation", ["claim_run", "assert_run", "claim_tool", "assert_tool"])
@pytest.mark.parametrize("autoflush", [False, True])
def test_unrelated_pending_object_is_not_implicitly_flushed(owner_db, operation, autoflush):
    parent = tool = None
    if operation != "claim_run":
        with owner_db.begin() as db:
            parent = ownership.claim_run(db, run_id=41, enterprise_id=1, conversation_id=2, generation=7)
            if operation == "assert_tool":
                tool = ownership.claim_tool(
                    db, run_id=41, enterprise_id=1, conversation_id=2, generation=7,
                    run_secret=parent, call_id="old", tool_name="search", arguments={"query": "a"}, wallet_id=9,
                )
    with owner_db(autoflush=autoflush) as db:
        unrelated = User(username="unrelated-pending", password="x", role="user", status=1)
        db.add(unrelated)
        common = dict(run_id=41, enterprise_id=1, conversation_id=2, generation=7)
        with pytest.raises(ForbiddenError, match="未刷新"):
            if operation == "claim_run":
                ownership.claim_run(db, **common)
            elif operation == "assert_run":
                ownership.assert_run_owner(db, **common, secret=parent)
            else:
                tool_args = dict(run_secret=parent, call_id="old" if tool else "new", tool_name="search",
                                 arguments={"query": "a"}, wallet_id=9)
                if tool:
                    ownership.assert_tool_owner(db, **common, tool_secret=tool, **tool_args)
                else:
                    ownership.claim_tool(db, **common, **tool_args)
        assert unrelated in db.new and unrelated.id is None
        db.rollback()
