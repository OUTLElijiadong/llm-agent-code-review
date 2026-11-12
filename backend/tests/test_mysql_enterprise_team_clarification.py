"""Disposable MySQL coverage for team clarification decisions."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from test_mysql_clarification_authority_118 import _setup

from app.core.exceptions import AuthError
from app.models.billing_ledger import BillingCreditBatch, BillingLimit, BillingWallet
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.services import enterprise_agent_team_service
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import WAITING_CLARIFICATION, PendingAction, RunCheckpoint, ToolCall

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize(
    ("model", "row_key"),
    [
        (BillingWallet, "wallet_id"),
        (BillingLimit, "limit_id"),
        (BillingCreditBatch, "batch_id"),
    ],
    ids=["wallet", "quota", "credit-batch"],
)
def test_mysql_team_budget_holds_each_accounting_lock_until_commit(fresh_chain, model, row_key):
    maker = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    key = uuid4().hex
    now = runtime.enterprise_org_service._now()
    period_start = now.replace(day=1)
    period_end = (period_start + timedelta(days=32)).replace(day=1)
    with maker.begin() as db:
        wallet = BillingWallet(owner_type="enterprise", owner_id=90_000_000 + int(key[:8], 16))
        db.add(wallet)
        db.flush()
        batch = BillingCreditBatch(wallet_id=wallet.id, grant_key=f"team-budget-{key}", kind="subscription",
                                   issued_milli=20_000, remaining_milli=20_000, reserved_milli=0)
        limit = BillingLimit(limit_key=f"team-budget-{key}", enterprise_id=wallet.owner_id, category="normal",
                             period_start=period_start, period_end=period_end, limit_milli=10_000,
                             used_milli=0, reserved_milli=0)
        db.add_all([batch, limit])
        db.flush()
        wallet_id, enterprise_id = int(wallet.id), int(wallet.owner_id)
        row_ids = {"wallet_id": wallet_id, "limit_id": int(limit.id), "batch_id": int(batch.id)}

    started = Event()
    requester = {}

    def reserve_quota():
        with maker.begin() as db:
            requester["connection_id"] = db.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            started.set()
            db.query(model).filter_by(id=row_ids[row_key]).with_for_update().one()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with maker.begin() as holder:
            holder_id = holder.execute(text("SELECT CONNECTION_ID()")).scalar_one()
            budget = enterprise_agent_team_service._team_budget(
                holder, enterprise_id=enterprise_id, group_id=None, user_id=1,
            )
            assert budget == 5_000
            future = pool.submit(reserve_quota)
            assert started.wait(5)
            deadline = monotonic() + 5
            waiting = False
            while monotonic() < deadline:
                with fresh_chain.connect() as observer:
                    waiting = bool(observer.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads r ON r.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "JOIN performance_schema.threads b ON b.THREAD_ID=w.BLOCKING_THREAD_ID "
                        "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                        "WHERE r.PROCESSLIST_ID=:requester_id AND b.PROCESSLIST_ID=:holder_id "
                        "AND l.OBJECT_NAME=:table_name LIMIT 1"
                    ), {"requester_id": requester.get("connection_id"), "holder_id": holder_id,
                        "table_name": model.__tablename__}).first())
                if waiting:
                    break
                sleep(0.05)
            assert waiting, f"team budget must hold the {model.__tablename__} row lock through its write transaction"
            assert not future.done()
        future.result(timeout=10)
    with maker() as db:
        assert db.query(BillingWallet).filter_by(id=wallet_id).count() == 1


def _pending(engine, monkeypatch, key):
    monkeypatch.setattr("app.services.enterprise_org_service._billing_access_suspended", lambda *a, **k: False)
    maker, eid, rid, actor, _ = _setup(engine, key, prior_directive=True)
    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        prior = db.query(EnterpriseChatMessage).filter_by(conversation_id=rid, sender_id=actor).one()
        room = db.get(EnterpriseConversation, rid)
        messages = (
            db.query(EnterpriseChatMessage).filter_by(conversation_id=rid).order_by(EnterpriseChatMessage.id).all()
        )
        snapshot = runtime._role_snapshot(
            db, enterprise_id=eid, room=room, messages=messages, initiator_id=int(run.initiator_user_id)
        )
        state.permission_snapshot_json = runtime._json(snapshot)
        state.permission_snapshot_sha256 = runtime._digest(snapshot)
        db.flush()
        executor = runtime.EnterpriseAssistantToolExecutor(
            db,
            enterprise_id=eid,
            conversation_id=rid,
            run_public_id=run.public_id,
            lease_generation=state.lease_generation,
            actor_user_id=run.initiator_user_id,
        )
        monkeypatch.setattr(executor, "_current", lambda: (state, {}))
        call = ToolCall(
            call_id="guarded",
            name="create_registered_enterprise_agent_team",
            arguments={"title": "团队"},
            raw_arguments="{}",
        )
        result = asyncio.run(executor.execute(call))
        assert result.status == "clarification_required"
        checkpoint = RunCheckpoint(
            run_id=run.public_id,
            model="test",
            transcript=[],
            tools=[],
            status=WAITING_CLARIFICATION,
            pending=PendingAction(
                kind="clarification", call=call, approval_id=result.approval_id, preview=result.preview
            ),
        )
        state.status, state.checkpoint_json = WAITING_CLARIFICATION, runtime._json(checkpoint.to_dict())
        return maker, eid, rid, run.public_id, actor, int(prior.id), int(result.approval_id)


def test_mysql_decision_waits_for_run_lock_then_is_idempotent(fresh_chain, monkeypatch):
    maker, eid, rid, public, actor, source, request_id = _pending(fresh_chain, monkeypatch, 17)
    started = Event()
    expires = int(datetime.now(timezone.utc).timestamp()) + 60

    def decide():
        started.set()
        with maker() as db:
            return runtime.decide_clarification(
                db,
                enterprise_id=eid,
                conversation_id=rid,
                run_public_id=public,
                request_id=request_id,
                source_message_id=source,
                user_id=actor,
                decision="allow",
                expected_token_version=0,
                expected_token_expires_at=expires,
            )

    with maker() as holder:
        holder_id = holder.execute(text("SELECT CONNECTION_ID()")).scalar_one()
        holder.query(EnterpriseAssistantRun).filter_by(public_id=public).with_for_update().one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(decide)
            try:
                assert started.wait(3)
                deadline = monotonic() + 5
                waiting = False
                while monotonic() < deadline and not waiting:
                    with fresh_chain.connect() as connection:
                        waiting = bool(
                            connection.execute(
                                text(
                                    "SELECT 1 FROM performance_schema.data_lock_waits w "
                                    "JOIN performance_schema.threads t ON t.THREAD_ID=w.BLOCKING_THREAD_ID "
                                    "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                                ),
                                {"cid": holder_id},
                            ).first()
                        )
                    if not waiting:
                        sleep(0.05)
                assert waiting, "decision must wait for the held run lock"
            finally:
                holder.commit()
            assert future.result(timeout=10)["status"] == "complete"
    assert decide()["status"] == "complete"


def test_mysql_token_expiring_while_waiting_for_run_lock_is_rejected(fresh_chain, monkeypatch):
    maker, eid, rid, public, actor, source, request_id = _pending(fresh_chain, monkeypatch, 18)
    started = Event()
    expires = int(datetime.now(timezone.utc).timestamp()) + 2

    def decide():
        started.set()
        with maker() as db:
            return runtime.decide_clarification(
                db,
                enterprise_id=eid,
                conversation_id=rid,
                run_public_id=public,
                request_id=request_id,
                source_message_id=source,
                user_id=actor,
                decision="allow",
                expected_token_version=0,
                expected_token_expires_at=expires,
            )

    with maker() as holder:
        holder.query(EnterpriseAssistantRun).filter_by(public_id=public).with_for_update().one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(decide)
            assert started.wait(3)
            while int(datetime.now(timezone.utc).timestamp()) < expires:
                sleep(0.05)
            holder.commit()
            with pytest.raises(AuthError):
                future.result(timeout=10)


def test_mysql_higher_authority_reviews_denial_without_losing_pending(fresh_chain, monkeypatch):
    maker, eid, rid, public, actor, source, request_id = _pending(fresh_chain, monkeypatch, 19)
    expires = int(datetime.now(timezone.utc).timestamp()) + 60

    def decide(user_id, decision):
        with maker() as db:
            return runtime.decide_clarification(
                db,
                enterprise_id=eid,
                conversation_id=rid,
                run_public_id=public,
                request_id=request_id,
                source_message_id=source,
                user_id=user_id,
                decision=decision,
                expected_token_version=0,
                expected_token_expires_at=expires,
            )

    assert decide(actor, "deny")["status"] == "denied"
    with maker() as db:
        pending = asyncio.run(
            runtime.resume_clarification(
                db,
                enterprise_id=eid,
                conversation_id=rid,
                run_public_id=public,
                request_id=request_id,
                user_id=actor + 1,
                expected_token_version=0,
                expected_token_expires_at=expires,
            )
        )
        assert pending["clarification_status"] == "denied" and pending["pending"]["preview"]["request_id"] == request_id
    assert decide(actor - 1, "allow")["status"] == "complete"
