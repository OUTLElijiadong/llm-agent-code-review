"""Generation races at real MySQL enterprise assistant claim entry points."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
import test_fresh_database_chain as chain
from test_mysql_enterprise_chat_authority import _initiator_user_id, _seed

from app.core.exceptions import ConflictError
from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.enterprise_assistant_runtime import EnterpriseAssistantApproval, EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage
from app.models.enterprise_content import EnterpriseAnnouncement
from app.services import enterprise_assistant_clarification_service as clarification
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import PendingAction, RunCheckpoint, RuntimeResult, ToolCall

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("entry", ["queue", "approval", "answer", "clarification"])
@pytest.mark.parametrize("stage", ["reload", "late_failed", "late_completed"])
def test_claimed_driver_cannot_adopt_replacement_generation(fresh_chain, monkeypatch, entry, stage):
    sessions, enterprise_id, _group, owner, author, room_id, public_id = _seed(
        fresh_chain, prior_authority_before_source=entry == "clarification",
        include_prior_message=entry == "clarification",
    )
    actor = _initiator_user_id(sessions, public_id)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.assistant_service, "_check_scope", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.ledger, "available_milli", lambda *_a, **_kw: 100_000)
    calls = []
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda *_a: calls.append("model-config"))
    wallet = BillingWallet(owner_type="enterprise", owner_id=enterprise_id)
    approval_id = request_id = None
    call = ToolCall(call_id=f"{entry}-call", name="search_enterprise_knowledge",
                    arguments={"query": "回归测试"}, raw_arguments='{"query":"回归测试"}')
    with sessions.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=public_id).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        db.add(wallet)
        checkpoint = RunCheckpoint(run_id=public_id, model="test", transcript=[], tools=[], status="queued")
        if entry == "approval":
            payload = {"title": "测试", "content": "测试"}
            call = ToolCall(call_id="approval-call", name="propose_enterprise_announcement", arguments=payload,
                            raw_arguments=runtime._json(payload))
            approval = EnterpriseAssistantApproval(
                assistant_run_id=run.id, enterprise_id=enterprise_id, conversation_id=room_id,
                group_id=run.group_id, initiator_user_id=actor, source_message_id=run.source_message_id,
                run_public_id=public_id, call_id=call.call_id, tool_name=call.name,
                required_role="enterprise_admin", status="pending", arguments_json=runtime._json(payload),
                arguments_sha256=runtime._digest(payload),
            )
            db.add(approval)
            db.flush()
            approval_id = int(approval.id)
            checkpoint.status = runtime.WAITING_APPROVAL
            checkpoint.pending = PendingAction(kind="approval", call=call, approval_id=approval_id)
        elif entry == "answer":
            call = ToolCall(
                call_id="answer-call", name="ask_user", arguments={"question": "继续？"}, raw_arguments="{}",
            )
            checkpoint.status = runtime.WAITING_INPUT
            checkpoint.pending = PendingAction(kind="input", call=call)
        elif entry == "clarification":
            run.status = state.status = "running"
            db.flush()
            target = {"enterprise_id": enterprise_id, "conversation_id": room_id}
            record = clarification.create_request(
                db, enterprise_id=enterprise_id, conversation_id=room_id, run_id=int(run.id),
                phase="tool", call_id=call.call_id, action=call.name, target=target,
                arguments=call.arguments, expires_at=runtime._now() + timedelta(minutes=20),
            )
            decision = db.query(clarification.EnterpriseAssistantClarificationDecision).filter_by(
                request_id=record.id,
            ).one()
            clarification.decide(db, enterprise_id=enterprise_id, conversation_id=room_id,
                                 request_id=record.id, source_message_id=decision.source_message_id,
                                 user_id=author, decision="allow")
            request_id = int(record.id)
            checkpoint.status = runtime.WAITING_CLARIFICATION
            checkpoint.pending = PendingAction(kind="clarification", call=call, approval_id=request_id,
                                               preview={"request_id": request_id, "version": int(record.version)})
        state.status = checkpoint.status
        state.checkpoint_json = runtime._json(checkpoint.to_dict())
        state.lease_expires_at = None
        run.status = {"queue": "queued", "approval": "awaiting_approval", "answer": "awaiting_input",
                      "clarification": "awaiting_clarification"}[entry]
    held, release = Event(), Event()
    thread_id = []
    original = runtime._run_record

    def pause_reload(db, *args, **kwargs):
        if thread_id and get_ident() == thread_id[0] and not kwargs.get("lock") and not held.is_set():
            held.set()
            if not release.wait(10):
                raise TimeoutError("generation claim barrier timed out")
        return original(db, *args, **kwargs)

    if stage == "reload":
        monkeypatch.setattr(runtime, "_run_record", pause_reload)
    else:
        class DelayedRuntime:
            async def finish(self, *_args, **_kwargs):
                held.set()
                if not release.wait(10):
                    raise TimeoutError("late result barrier timed out")
                return RuntimeResult(run_id=public_id, status=stage.removeprefix("late_"))

            drive_persisted = approve = reject = answer = resume_clarification = finish

        def fake_build(_db, **kwargs):
            assert kwargs["expected_generation"] == 2
            assert kwargs["state"].lease_generation == 2
            return DelayedRuntime(), SimpleNamespace(), None, None

        monkeypatch.setattr(runtime, "_build_runtime", fake_build)
        monkeypatch.setattr(runtime, "_billing_scope", lambda *_a, **_kw: nullcontext())

    def worker():
        thread_id.append(get_ident())
        with sessions() as db:
            try:
                if entry == "queue":
                    asyncio.run(runtime.execute_queued_run(
                        fresh_chain, enterprise_id=enterprise_id, conversation_id=room_id, run_public_id=public_id,
                    ))
                    return "queue-returned"
                if entry == "approval":
                    return asyncio.run(runtime.decide_approval(
                        db, enterprise_id=enterprise_id, approval_id=approval_id, user_id=owner, decision="approve",
                    ))
                if entry == "answer":
                    return asyncio.run(runtime.answer_run(
                        db, enterprise_id=enterprise_id, user_id=actor, conversation_id=room_id,
                        run_public_id=public_id, call_id=call.call_id, answer="继续",
                    ))
                return asyncio.run(runtime.resume_clarification(
                    db, enterprise_id=enterprise_id, conversation_id=room_id, run_public_id=public_id,
                    request_id=request_id, user_id=actor, expected_token_version=0,
                    expected_token_expires_at=int(datetime.now(timezone.utc).timestamp()) + 300,
                ))
            except ConflictError:
                return "fenced"

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            assert held.wait(10), f"{entry} did not reach its post-claim {stage} barrier"
            with sessions.begin() as db:
                state = db.query(EnterpriseAssistantRuntimeState).filter_by(
                    enterprise_id=enterprise_id, conversation_id=room_id,
                ).with_for_update().one()
                assert state.lease_generation == 2
                state.lease_expires_at = runtime._now() - timedelta(seconds=1)
            with sessions.begin() as db:
                _run, state = runtime._claim_new_invocation(
                    db, enterprise_id=enterprise_id, conversation_id=room_id, run_public_id=public_id,
                    allow_statuses=runtime._RETRYABLE | {runtime.WAITING_APPROVAL, runtime.WAITING_INPUT,
                                                        runtime.WAITING_CLARIFICATION},
                    actor_user_id=actor,
                )
                assert state.lease_generation == 3
            release.set()
            assert future.result(timeout=10) == ("queue-returned" if entry == "queue" else "fenced")
        assert calls == []
        with sessions() as db:
            run = db.query(EnterpriseAssistantRun).filter_by(public_id=public_id).one()
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
            assert state.lease_generation == 3 and run.status == "running"
            assert db.query(BillingReservation).filter_by(enterprise_id=enterprise_id).count() == 0
            assert db.query(EnterpriseChatMessage).filter_by(assistant_run_id=run.id).count() == 0
            assert db.query(EnterpriseAnnouncement).filter_by(enterprise_id=enterprise_id).count() == 0
    finally:
        release.set()
