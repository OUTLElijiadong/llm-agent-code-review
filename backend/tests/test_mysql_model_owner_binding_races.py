"""Race probes only: disposable, loopback-only MySQL from fresh_chain fixture."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_mysql_enterprise_ownership_foundation import _ready

from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.enterprise_assistant_ownership import (
    EnterpriseAssistantModelAttemptOwner,
    EnterpriseAssistantToolOwner,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage
from app.services import enterprise_assistant_ownership_service as ownership
from app.services import enterprise_assistant_runtime_service as runtime

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
ROUND_1 = "round:0123456789abcdef0123456789abcdef"
ROUND_2 = "round:1123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def activate_isolated_only(monkeypatch):
    monkeypatch.setattr(ownership, "_FOUNDATION_ACTIVATED", True)


def _new_hold(run, wallet, request_key):
    return BillingReservation(
        request_key=request_key,
        model_name="deepseek-flash",
        billable_kind="model",
        enterprise_id=run.enterprise_id,
        group_id=run.group_id,
        user_id=run.initiator_user_id,
        usage_kind="member",
        enterprise_wallet_id=wallet,
        personal_wallet_id=None,
        allow_personal=0,
        requested_milli=100,
        status="reserved",
        usage_state="pending",
    )


def test_different_round_same_request_flush_1062_rolls_back_all_outer_changes(fresh_chain):
    sessions, enterprise, conversation, run_id, wallet_id = _ready(fresh_chain)
    with sessions.begin() as db:
        run = db.get(EnterpriseAssistantRun, run_id)
        parent = ownership.claim_run(
            db, run_id=run_id, enterprise_id=enterprise, conversation_id=conversation, generation=1
        )
        request_key = f"enterprise-assistant:{run.public_id}:1:1"
        hold = _new_hold(run, wallet_id, request_key)
        db.add(hold)
        ownership.claim_model_attempt(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=1,
            run_secret=parent,
            round_key=ROUND_1,
            payload_digest=b"x" * 32,
            reservation=hold,
        )
        hold_id = int(hold.id)
        wallet_version = int(db.get(BillingWallet, wallet_id).lock_version)
    with pytest.raises(IntegrityError) as failure:
        with sessions.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            wallet = db.get(BillingWallet, wallet_id)
            wallet.lock_version = wallet_version + 123
            db.flush([wallet])
            ownership.claim_tool(
                db,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=parent,
                call_id="uncommitted-probe",
                tool_name="search",
                arguments={"query": "probe"},
                wallet_id=wallet_id,
            )
            db.add(duplicate := _new_hold(run, wallet_id, request_key))
            ownership.claim_model_attempt(
                db,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=parent,
                round_key=ROUND_2,
                payload_digest=b"y" * 32,
                reservation=duplicate,
            )
    assert failure.value.orig.args[0] == 1062
    with sessions() as db:
        assert db.get(BillingWallet, wallet_id).lock_version == wallet_version
        assert db.query(BillingReservation).filter_by(enterprise_id=enterprise).count() == 1
        assert db.get(BillingReservation, hold_id).status == "reserved"
        assert db.query(EnterpriseAssistantModelAttemptOwner).filter_by(assistant_run_id=run_id).count() == 1
        assert db.query(EnterpriseAssistantToolOwner).filter_by(assistant_run_id=run_id).count() == 0


def _two_runs_with_same_wallet(fresh_chain):
    sessions, enterprise, conversation, first_id, wallet_id = _ready(fresh_chain)
    with sessions.begin() as db:
        first = db.get(EnterpriseAssistantRun, first_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=first_id).one()
        suffix = uuid4().hex[:12]
        source = EnterpriseChatMessage(
            enterprise_id=enterprise,
            conversation_id=conversation,
            sender_id=first.initiator_user_id,
            sender_type="human",
            client_message_id=f"lock-test-{suffix}",
            content="test",
            sender_authority_rank=1,
        )
        db.add(source)
        db.flush()
        second = EnterpriseAssistantRun(
            public_id=f"lock-{suffix}",
            request_key=f"lock-{suffix}",
            enterprise_id=enterprise,
            conversation_id=conversation,
            group_id=first.group_id,
            initiator_user_id=first.initiator_user_id,
            source_message_id=source.id,
            status="running",
            prompt_sha256="a" * 64,
        )
        db.add(second)
        db.flush()
        db.add(
            EnterpriseAssistantRuntimeState(
                assistant_run_id=second.id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                group_id=first.group_id,
                initiator_user_id=first.initiator_user_id,
                source_message_id=source.id,
                status="running",
                permission_snapshot_sha256="a" * 64,
                lease_generation=1,
                billing_invocation_count=1,
                lease_expires_at=runtime._now() + timedelta(minutes=5),
            )
        )
        state.billing_invocation_count = 1
        second_id = int(second.id)
    parents = {}
    with sessions.begin() as db:
        for run_id in (first_id, second_id):
            parents[run_id] = ownership.claim_run(
                db, run_id=run_id, enterprise_id=enterprise, conversation_id=conversation, generation=1
            )
    return sessions, enterprise, conversation, first_id, second_id, wallet_id, parents


def _tool_then_model(db, run, enterprise, conversation, wallet_id, parent, *, acquired, wait_for=None, token=None):
    kwargs = dict(
        run_id=int(run.id),
        enterprise_id=enterprise,
        conversation_id=conversation,
        generation=1,
        run_secret=parent,
        call_id=f"call-{run.id}",
        tool_name="search",
        arguments={"query": "probe"},
        wallet_id=wallet_id,
    )
    if token is None:
        ownership.claim_tool(db, **kwargs)
    else:
        ownership.assert_tool_owner(db, tool_secret=token, **kwargs)
    acquired.set()
    if wait_for is not None:
        assert wait_for.wait(10), "wallet claim barrier timed out"
    hold = _new_hold(run, wallet_id, f"enterprise-assistant:{run.public_id}:1:1")
    db.add(hold)
    ownership.claim_model_attempt(
        db,
        run_id=int(run.id),
        enterprise_id=enterprise,
        conversation_id=conversation,
        generation=1,
        run_secret=parent,
        round_key=ROUND_1,
        payload_digest=b"x" * 32,
        reservation=hold,
    )


@pytest.mark.parametrize("mode", ["claim", "assert"])
def test_same_wallet_tool_to_model_waits_without_lock_upgrade_deadlock(fresh_chain, mode):
    sessions, enterprise, conversation, first_id, second_id, wallet_id, parents = _two_runs_with_same_wallet(
        fresh_chain
    )
    first_locked, second_started, second_locked, release = Event(), Event(), Event(), Event()
    tokens = {}
    if mode == "assert":
        for run_id in (first_id, second_id):
            with sessions.begin() as db:
                tokens[run_id] = ownership.claim_tool(
                    db,
                    run_id=run_id,
                    enterprise_id=enterprise,
                    conversation_id=conversation,
                    generation=1,
                    run_secret=parents[run_id],
                    call_id=f"call-{run_id}",
                    tool_name="search",
                    arguments={"query": "probe"},
                    wallet_id=wallet_id,
                )

    def execute(run_id, first):
        try:
            with sessions.begin() as db:
                db.execute(text("SET SESSION innodb_lock_wait_timeout=8"))
                run = db.get(EnterpriseAssistantRun, run_id)
                if not first:
                    second_started.set()
                _tool_then_model(
                    db,
                    run,
                    enterprise,
                    conversation,
                    wallet_id,
                    parents[run_id],
                    acquired=first_locked if first else second_locked,
                    wait_for=release if first else None,
                    token=tokens.get(run_id),
                )
            return "committed"
        except DBAPIError as exc:
            return exc.orig.args[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, first_id, True)
        assert first_locked.wait(5)
        second = pool.submit(execute, second_id, False)
        assert second_started.wait(5)
        entered_before_release = second_locked.wait(0.75)
        release.set()
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert not entered_before_release, "same wallet was not held exclusively by the first claim"
    assert results == ["committed", "committed"]
    with sessions() as db:
        assert db.query(EnterpriseAssistantToolOwner).filter_by(enterprise_id=enterprise).count() == 2
        assert db.query(EnterpriseAssistantModelAttemptOwner).filter_by(enterprise_id=enterprise).count() == 2
        assert db.query(BillingReservation).filter_by(enterprise_id=enterprise).count() == 2


def test_different_wallets_claim_tool_and_model_concurrently(fresh_chain):
    left = _ready(fresh_chain)
    right = _ready(fresh_chain)
    assert left[1] != right[1] and left[4] != right[4]
    parents = []
    for sessions, enterprise, conversation, run_id, _wallet in (left, right):
        with sessions.begin() as db:
            parents.append(
                ownership.claim_run(
                    db,
                    run_id=run_id,
                    enterprise_id=enterprise,
                    conversation_id=conversation,
                    generation=1,
                )
            )
    barrier = Barrier(2, timeout=10)

    def execute(scope, parent):
        sessions, enterprise, conversation, run_id, wallet_id = scope
        with sessions.begin() as db:
            db.execute(text("SET SESSION innodb_lock_wait_timeout=8"))
            run = db.get(EnterpriseAssistantRun, run_id)
            ownership.claim_tool(
                db,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=parent,
                call_id=f"call-{run_id}",
                tool_name="search",
                arguments={"query": "probe"},
                wallet_id=wallet_id,
            )
            barrier.wait()
            hold = _new_hold(run, wallet_id, f"enterprise-assistant:{run.public_id}:1:1")
            db.add(hold)
            ownership.claim_model_attempt(
                db,
                run_id=run_id,
                enterprise_id=enterprise,
                conversation_id=conversation,
                generation=1,
                run_secret=parent,
                round_key=ROUND_1,
                payload_digest=b"x" * 32,
                reservation=hold,
            )
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, scope, parent) for scope, parent in zip((left, right), parents)]
        assert [future.result(timeout=20) for future in futures] == ["committed", "committed"]


def test_flushed_generation_and_run_owner_rollback_atomically(fresh_chain):
    from app.models.enterprise_assistant_ownership import EnterpriseAssistantRunOwner

    sessions, enterprise, conversation, run_id, _wallet = _ready(fresh_chain)
    with sessions() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(id=run_id).with_for_update().one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).with_for_update().one()
        state.lease_generation = 2
        state.billing_invocation_count = 2
        run.attempt_count = 2
        db.flush()
        secret = ownership.claim_run(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=2,
        )
        ownership.assert_run_owner(
            db,
            run_id=run_id,
            enterprise_id=enterprise,
            conversation_id=conversation,
            generation=2,
            secret=secret,
        )
        db.rollback()
    with sessions() as db:
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one()
        assert state.lease_generation == 1
        assert db.query(EnterpriseAssistantRunOwner).filter_by(assistant_run_id=run_id).count() == 0
