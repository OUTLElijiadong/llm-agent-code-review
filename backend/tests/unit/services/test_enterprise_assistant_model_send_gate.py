"""Persisted authority clearance gates the first and every later model send."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingWallet
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
from app.services import enterprise_assistant_clarification_service as clarification
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import (
    COMPLETED,
    RUNNING,
    WAITING_CLARIFICATION,
    PendingAction,
    RunCheckpoint,
    ToolCall,
)

pytest_plugins = ("test_enterprise_assistant_clarification_records",)


def _freeze(records_db):
    run = records_db.query(EnterpriseAssistantRun).filter_by(id=200).one()
    state = records_db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
    room = records_db.query(EnterpriseConversation).filter_by(id=30).one()
    messages = (records_db.query(EnterpriseChatMessage)
                .filter_by(enterprise_id=10, conversation_id=30, sender_type="human")
                .order_by(EnterpriseChatMessage.id).all())
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages,
                                      initiator_id=3)
    state.permission_snapshot_json = runtime._json(snapshot)
    state.permission_snapshot_sha256 = runtime._digest(snapshot)
    state.checkpoint_json = runtime._json(RunCheckpoint(
        run_id=run.public_id, model="deepseek-flash", transcript=[], tools=[],
    ).to_dict())
    records_db.flush()
    return run, state


def _checkpoint(marker):
    return RunCheckpoint(run_id="clarify-run", model="deepseek-flash", transcript=[], tools=[],
                         context_metadata={"model_send_clearance": marker})


def test_model_send_without_conflicts_requires_exact_frozen_evidence(records_db):
    run, state = _freeze(records_db)
    evidence = clarification.model_send_evidence(records_db, run)
    assert evidence["candidate_message_ids"] == []
    marker = {"status": "not_required", "fingerprint": evidence["fingerprint"],
              "authority_version_sha256": evidence["authority_version_sha256"],
              "candidate_message_ids": []}

    runtime._require_model_send_clearance(records_db, run=run, state=state, checkpoint=_checkpoint(marker))

    records_db.get(EnterpriseChatMessage, 100).content = "改过的历史会话指令"
    records_db.flush()
    with pytest.raises(ForbiddenError, match="权限意见已变化"):
        runtime._require_model_send_clearance(records_db, run=run, state=state, checkpoint=_checkpoint(marker))


def test_new_run_waits_for_higher_authority_directive_before_queueing(records_db, monkeypatch):
    room = records_db.query(EnterpriseConversation).filter_by(id=30).one()
    room.kind = "group"
    room.group_id = 20
    room.conversation_key = "group:20"
    records_db.add(EnterpriseGroup(id=20, enterprise_id=10, name="小组", status="active"))
    records_db.add_all([
        EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=1, role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=3, role="member", status="active"),
        EnterpriseChatMessage(id=104, enterprise_id=10, conversation_id=30, sender_id=3,
                              sender_type="human", client_message_id="new-request",
                              content="@小菱 请帮我处理这件事"),
        BillingWallet(owner_type="enterprise", owner_id=10),
    ])
    records_db.get(EnterpriseChatMessage, 100).sender_id = 1
    records_db.get(EnterpriseChatMessage, 100).content = "@小菱 先暂停当前操作"
    records_db.commit()
    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", False)
    monkeypatch.setattr(runtime.settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.assistant_service, "_model_config",
                        lambda _db: (SimpleNamespace(api_key="test", base_url="https://example.test",
                                                     model="deepseek-flash"), "deepseek-flash"))
    monkeypatch.setattr(runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime.enterprise_knowledge_attachment_service, "search_attachments",
                        lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime.ledger, "available_milli", lambda *_args, **_kwargs: 1)

    result = asyncio.run(runtime.run_for_message(
        records_db, enterprise_id=10, user_id=3, conversation_id=30, source_message_id=104,
    ))

    assert result["status"] == "awaiting_clarification"
    assert result["pending"]["operation"] == "发送本次小菱回复"
    assert result["pending"]["preview"]["candidates"][0]["message_id"] == 100
    run = records_db.query(EnterpriseAssistantRun).filter_by(public_id=result["run_id"]).one()
    state = records_db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
    assert run.status == "awaiting_clarification" and state.status == WAITING_CLARIFICATION
    assert run.attempt_count == 0 and state.billing_invocation_count == 0


def test_model_send_with_peer_directive_requires_recorded_decision_and_fresh_authority(records_db):
    records_db.get(EnterpriseChatMessage, 100).content = "@小菱 先不要创建团队"
    run, state = _freeze(records_db)
    evidence = clarification.model_send_evidence(records_db, run)
    assert evidence["candidate_message_ids"] == [100]
    call_id = f"{runtime._MODEL_SEND_CALL_PREFIX}{run.public_id}"
    target = runtime._model_send_target(10, 30)
    arguments = runtime._model_send_arguments(run, state)
    record = clarification.create_request(
        records_db, enterprise_id=10, conversation_id=30, run_id=run.id, phase="model_send",
        call_id=call_id, action=runtime._MODEL_SEND_ACTION, target=target, arguments=arguments,
        expires_at=clarification._now() + timedelta(hours=1),
    )
    records_db.flush()
    marker = {
        "status": "cleared", "request_id": int(record.id), "version": int(record.version),
        "fingerprint": str(record.authority_sha256),
        "authority_version_sha256": str(record.authority_version_sha256),
        "candidate_message_ids": [100],
    }
    checkpoint = _checkpoint(marker)

    with pytest.raises(ForbiddenError, match="澄清尚未完成"):
        runtime._require_model_send_clearance(records_db, run=run, state=state, checkpoint=checkpoint)

    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=record.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.flush()
    runtime._require_model_send_clearance(records_db, run=run, state=state, checkpoint=checkpoint)

    records_db.get(EnterpriseChatMessage, 100).content = "@小菱 现在允许创建团队"
    records_db.flush()
    with pytest.raises(ForbiddenError, match="内容发生变化"):
        runtime._require_model_send_clearance(records_db, run=run, state=state, checkpoint=checkpoint)


def test_model_send_resume_enters_persisted_model_loop_without_replaying_a_tool(records_db, monkeypatch):
    records_db.get(EnterpriseChatMessage, 100).content = "@小菱 先不要创建团队"
    run, state = _freeze(records_db)
    evidence = clarification.model_send_evidence(records_db, run)
    call_id = f"{runtime._MODEL_SEND_CALL_PREFIX}{run.public_id}"
    arguments = runtime._model_send_arguments(run, state)
    target = runtime._model_send_target(10, 30)
    record = clarification.create_request(
        records_db, enterprise_id=10, conversation_id=30, run_id=run.id, phase="model_send",
        call_id=call_id, action=runtime._MODEL_SEND_ACTION, target=target, arguments=arguments,
        expires_at=clarification._now() + timedelta(hours=1),
    )
    preview = {"request_id": int(record.id), "version": int(record.version),
               "action": runtime._MODEL_SEND_ACTION,
               "candidates": [{"message_id": 100, "author_id": 2, "rank": 3,
                               "content": "@小菱 先不要创建团队"}]}
    pending_call = ToolCall(call_id=call_id, name=runtime._MODEL_SEND_ACTION,
                            arguments=arguments, raw_arguments=runtime._json(arguments))
    checkpoint = RunCheckpoint(
        run_id=run.public_id, model="deepseek-flash", transcript=[], tools=[],
        status=WAITING_CLARIFICATION,
        pending=PendingAction(kind="clarification", call=pending_call,
                             operation="发送本次小菱回复", approval_id=record.id, preview=preview),
        context_metadata={"model_send_clearance": {
            "status": "pending", "request_id": int(record.id), "version": int(record.version),
            "fingerprint": evidence["fingerprint"],
            "authority_version_sha256": evidence["authority_version_sha256"],
            "candidate_message_ids": [100],
        }},
    )
    state.status = WAITING_CLARIFICATION
    state.checkpoint_json = runtime._json(checkpoint.to_dict())
    run.status = "awaiting_clarification"
    records_db.add(BillingWallet(owner_type="enterprise", owner_id=10))
    records_db.commit()
    clarification.decide(records_db, enterprise_id=10, conversation_id=30, request_id=record.id,
                         source_message_id=100, user_id=2, decision="allow")
    records_db.commit()

    for owner, name in (
        (runtime, "_check_unresolved_billing"),
        (runtime.assistant_service, "_require_subscription"),
        (runtime.assistant_service, "_require_active_limit"),
    ):
        monkeypatch.setattr(owner, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_validated_billing_group", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.ledger, "available_milli", lambda *_args, **_kwargs: 10)
    monkeypatch.setattr(runtime, "_billing_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(runtime, "_result_for_runtime", lambda _factory, **kwargs: {"status": kwargs["result"].status})

    class PersistedRuntime:
        def __init__(self, db):
            self.db = db

        async def drive_persisted(self, run_public_id):
            assert run_public_id == run.public_id
            persisted = self.db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
            resumed = RunCheckpoint.from_dict(runtime._parse_json(persisted.checkpoint_json, message="bad"))
            assert resumed.status == RUNNING and resumed.pending is None
            assert resumed.context_metadata["model_send_clearance"]["status"] == "cleared"
            return SimpleNamespace(status=COMPLETED)

        async def resume_clarification(self, *_args):
            pytest.fail("模型发送澄清不能伪装成工具调用恢复")

    monkeypatch.setattr(runtime, "_build_runtime",
                        lambda work, **_kwargs: (PersistedRuntime(work), None, None, None))

    result = asyncio.run(runtime.resume_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id=run.public_id,
        request_id=int(record.id), user_id=3, expected_token_version=0,
        expected_token_expires_at=int(datetime.now(timezone.utc).timestamp()) + 60,
    ))

    assert result == {"status": COMPLETED}
