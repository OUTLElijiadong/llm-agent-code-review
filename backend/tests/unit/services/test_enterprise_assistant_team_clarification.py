"""Enterprise team creation must honor explicit prior authority directives."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest

from app.core.exceptions import AuthError, ForbiddenError
from app.models.billing_ledger import BillingWallet
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import WAITING_CLARIFICATION, PendingAction, RunCheckpoint, ToolCall

pytest_plugins = ("test_enterprise_assistant_clarification_records",)


def test_owner_directive_requires_clarification_before_team_creation(records_db, monkeypatch):
    earlier = records_db.get(EnterpriseChatMessage, 100)
    earlier.sender_id = 1
    earlier.content = "@小菱 不要创建任何团队"
    records_db.commit()
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    room = records_db.get(EnterpriseConversation, 30)
    messages = (
        records_db.query(EnterpriseChatMessage).filter_by(conversation_id=30).order_by(EnterpriseChatMessage.id).all()
    )
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages, initiator_id=3)
    state.permission_snapshot_json = runtime._json(snapshot)
    state.permission_snapshot_sha256 = runtime._digest(snapshot)
    records_db.commit()
    executor = runtime.EnterpriseAssistantToolExecutor(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        lease_generation=0,
        actor_user_id=3,
    )
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))
    calls = []
    monkeypatch.setattr(
        runtime.enterprise_agent_team_service,
        "create_team_from_assistant",
        lambda *args, **kwargs: calls.append(kwargs) or {"team_id": 1},
    )
    call = ToolCall(
        call_id="team-1",
        name="create_registered_enterprise_agent_team",
        arguments={"title": "团队"},
        raw_arguments="{}",
    )
    result = asyncio.run(executor.execute(call))
    assert result.status == "clarification_required"
    assert calls == []


def test_untagged_earlier_message_does_not_block_team_creation(records_db, monkeypatch):
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    room = records_db.get(EnterpriseConversation, 30)
    messages = (
        records_db.query(EnterpriseChatMessage).filter_by(conversation_id=30).order_by(EnterpriseChatMessage.id).all()
    )
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages, initiator_id=3)
    state.permission_snapshot_json, state.permission_snapshot_sha256 = (
        runtime._json(snapshot),
        runtime._digest(snapshot),
    )
    records_db.commit()
    executor = runtime.EnterpriseAssistantToolExecutor(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        lease_generation=0,
        actor_user_id=3,
    )
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))
    calls = []
    monkeypatch.setattr(
        runtime.enterprise_agent_team_service,
        "create_team_from_assistant",
        lambda *args, **kwargs: calls.append(kwargs) or {"team_id": 1},
    )
    from app.models.billing_ledger import BillingWallet

    records_db.add(BillingWallet(owner_type="enterprise", owner_id=10))
    records_db.commit()
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime.ledger, "available_milli", lambda *_a, **_k: 1)
    call = ToolCall(
        call_id="team-1",
        name="create_registered_enterprise_agent_team",
        arguments={"title": "团队"},
        raw_arguments="{}",
    )
    result = asyncio.run(executor.execute(call))
    assert result.status == "success", result.error
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["group", "direct"])
def test_explicit_owner_directive_is_scoped_to_group_or_direct_room(records_db, monkeypatch, kind):
    room = records_db.get(EnterpriseConversation, 30)
    room.kind = kind
    if kind == "group":
        room.group_id = 20
        records_db.add(EnterpriseGroup(id=20, enterprise_id=10, name="小组", status="active"))
        records_db.add_all(
            [
                EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=1, role="member", status="active"),
                EnterpriseGroupMember(enterprise_id=10, group_id=20, user_id=3, role="member", status="active"),
            ]
        )
    else:
        room.participant_low_id, room.participant_high_id = 1, 3
    records_db.commit()
    request_id = _pending(records_db, monkeypatch)
    preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=1
    )
    assert preview["request_id"] == request_id
    assert preview["candidates"][0]["author_id"] == 1


def test_directive_outside_frozen_30_messages_is_not_candidate(records_db, monkeypatch):
    records_db.add_all(
        [
            EnterpriseChatMessage(
                id=i,
                enterprise_id=10,
                conversation_id=30,
                sender_id=1 if i == 10 else 3,
                sender_type="human",
                client_message_id=f"old-{i}",
                content="@小菱 不要建队" if i == 10 else "日常消息",
            )
            for i in range(10, 45)
        ]
    )
    records_db.commit()
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    room = records_db.get(EnterpriseConversation, 30)
    messages = list(
        reversed(
            records_db.query(EnterpriseChatMessage)
            .filter_by(conversation_id=30)
            .order_by(EnterpriseChatMessage.id.desc())
            .limit(30)
            .all()
        )
    )
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages, initiator_id=3)
    state.permission_snapshot_json, state.permission_snapshot_sha256 = (
        runtime._json(snapshot),
        runtime._digest(snapshot),
    )
    records_db.commit()
    executor = runtime.EnterpriseAssistantToolExecutor(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        lease_generation=0,
        actor_user_id=3,
    )
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))
    monkeypatch.setattr(runtime.enterprise_agent_team_service, "create_team_from_assistant", lambda *_a, **_k: {})
    from app.models.billing_ledger import BillingWallet

    records_db.add(BillingWallet(owner_type="enterprise", owner_id=10))
    records_db.commit()
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime.ledger, "available_milli", lambda *_a, **_k: 1)
    call = ToolCall(
        call_id="team-1",
        name="create_registered_enterprise_agent_team",
        arguments={"title": "团队"},
        raw_arguments="{}",
    )
    assert asyncio.run(executor.execute(call)).status == "success"


def test_initial_team_write_rolls_back_if_subscription_expires_after_flush(records_db, monkeypatch):
    from app.models.project import Project

    earlier = records_db.get(EnterpriseChatMessage, 100)
    earlier.content = "普通讨论，没有指令"
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    room = records_db.get(EnterpriseConversation, 30)
    messages = records_db.query(EnterpriseChatMessage).filter_by(conversation_id=30).order_by(
        EnterpriseChatMessage.id
    ).all()
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages, initiator_id=3)
    state.permission_snapshot_json, state.permission_snapshot_sha256 = (
        runtime._json(snapshot), runtime._digest(snapshot),
    )
    records_db.commit()
    expired = [False]
    checks = []

    def gate(*_args, **_kwargs):
        checks.append(expired[0])
        if expired[0]:
            raise ForbiddenError("企业订阅已过期")

    executor = runtime.EnterpriseAssistantToolExecutor(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        lease_generation=0,
        actor_user_id=3,
    )

    def current():
        gate()
        return state, snapshot

    def create(db, **_kwargs):
        db.add(Project(user_id=3, project_name="initial-expiry-probe", status="active"))
        db.flush()
        expired[0] = True
        return {"team_id": 1}

    monkeypatch.setattr(executor, "_current", current)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", gate)
    monkeypatch.setattr(runtime.enterprise_agent_team_service, "create_team_from_assistant", create)
    call = ToolCall(
        call_id="team-1",
        name="create_registered_enterprise_agent_team",
        arguments={"title": "团队"},
        raw_arguments="{}",
    )
    result = asyncio.run(executor.execute(call))
    assert result.status == "error" and checks == [False, True]
    assert records_db.query(Project).filter_by(project_name="initial-expiry-probe").count() == 0


def _pending(records_db, monkeypatch, author_id=1):
    earlier = records_db.get(EnterpriseChatMessage, 100)
    earlier.sender_id, earlier.content = author_id, "@小菱 不要创建任何团队"
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    room = records_db.get(EnterpriseConversation, 30)
    messages = (
        records_db.query(EnterpriseChatMessage).filter_by(conversation_id=30).order_by(EnterpriseChatMessage.id).all()
    )
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages, initiator_id=3)
    state.permission_snapshot_json, state.permission_snapshot_sha256 = (
        runtime._json(snapshot),
        runtime._digest(snapshot),
    )
    records_db.commit()
    executor = runtime.EnterpriseAssistantToolExecutor(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        lease_generation=0,
        actor_user_id=3,
    )
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))
    call = ToolCall(
        call_id="team-1",
        name="create_registered_enterprise_agent_team",
        arguments={"title": "团队"},
        raw_arguments="{}",
    )
    result = asyncio.run(executor.execute(call))
    assert result.status == "clarification_required"
    transcript = [
        {"type": "function_call", "call_id": "team-1", "name": call.name, "arguments": "{}"},
        {"type": "function_call", "call_id": "later", "name": "test", "arguments": "{}"},
    ]
    checkpoint = RunCheckpoint(
        run_id="clarify-run",
        model="test",
        transcript=transcript,
        tools=[],
        status=WAITING_CLARIFICATION,
        pending=PendingAction(
            kind="clarification",
            call=call,
            remaining_calls=[ToolCall(call_id="later", name="test", arguments={}, raw_arguments="{}")],
            approval_id=result.approval_id,
            preview=result.preview,
        ),
    )
    state.status = WAITING_CLARIFICATION
    state.checkpoint_json = runtime._json(checkpoint.to_dict())
    records_db.commit()
    return result.approval_id


def _decision(db, request_id, *, user_id, decision, expires=None):
    return runtime.decide_clarification(
        db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        request_id=request_id,
        source_message_id=100,
        user_id=user_id,
        decision=decision,
        expected_token_version=0,
        expected_token_expires_at=expires if expires is not None else int(datetime.now(timezone.utc).timestamp()) + 60,
    )


def test_author_decision_is_idempotent_and_visible(records_db, monkeypatch):
    request_id = _pending(records_db, monkeypatch)
    preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=3
    )
    assert preview["candidates"][0]["author_id"] == 1
    assert preview["candidates"][0]["can_decide"] is False and preview["can_resume"] is False
    records_db.rollback()
    preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=1
    )
    assert preview["candidates"][0]["can_decide"] is True and preview["can_resume"] is False
    records_db.rollback()
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "complete"
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "complete"
    preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=3
    )
    assert preview["candidates"][0]["decision"] == "allow" and preview["can_resume"] is True
    records_db.rollback()
    with pytest.raises(ForbiddenError):
        _decision(records_db, request_id, user_id=2, decision="allow")


def test_denial_preserves_pending_for_author_revision_and_blocks_lower_rank(records_db, monkeypatch):
    request_id = _pending(records_db, monkeypatch)
    assert _decision(records_db, request_id, user_id=1, decision="deny")["status"] == "denied"
    records_db.expire_all()
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    checkpoint = RunCheckpoint.from_dict(runtime._parse_json(state.checkpoint_json, message="bad"))
    assert state.status == WAITING_CLARIFICATION and len(checkpoint.pending.remaining_calls) == 1
    run = records_db.get(EnterpriseAssistantRun, 200)
    assert runtime._status_payload(records_db, run, state)["status"] == "awaiting_clarification"
    assert runtime._minimal_run_status(run, state)["status"] == "awaiting_clarification"
    with pytest.raises(ForbiddenError):
        _decision(records_db, request_id, user_id=2, decision="allow")
    result = asyncio.run(
        runtime.resume_clarification(
            records_db,
            enterprise_id=10,
            conversation_id=30,
            run_public_id="clarify-run",
            request_id=request_id,
            user_id=3,
            expected_token_version=0,
            expected_token_expires_at=int(datetime.now(timezone.utc).timestamp()) + 60,
        )
    )
    assert result["status"] == "awaiting_clarification" and result["clarification_status"] == "denied"
    assert result["pending"]["preview"]["request_id"] == request_id
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "complete"


def test_lower_candidate_allow_cannot_resume_while_higher_candidate_is_pending(records_db, monkeypatch):
    records_db.get(EnterpriseChatMessage, 100).sender_id = 1
    records_db.get(EnterpriseChatMessage, 100).content = "@小菱 不要创建团队"
    records_db.add(EnterpriseChatMessage(
        id=99, enterprise_id=10, conversation_id=30, sender_id=4, sender_type="human",
        client_message_id="peer-directive", content="@小菱 可以创建团队",
    ))
    room = records_db.get(EnterpriseConversation, 30)
    messages = records_db.query(EnterpriseChatMessage).filter_by(
        enterprise_id=10, conversation_id=30, sender_type="human",
    ).order_by(EnterpriseChatMessage.id).all()
    snapshot = runtime._role_snapshot(records_db, enterprise_id=10, room=room, messages=messages, initiator_id=3)
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    state.permission_snapshot_json, state.permission_snapshot_sha256 = (
        runtime._json(snapshot),
        runtime._digest(snapshot),
    )
    records_db.commit()

    request_id = _pending(records_db, monkeypatch, author_id=1)
    preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=3
    )
    assert [candidate["message_id"] for candidate in preview["candidates"]] == [99, 100]
    records_db.rollback()

    decision = runtime.decide_clarification(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        request_id=request_id,
        source_message_id=99,
        user_id=4,
        decision="allow",
        expected_token_version=0,
        expected_token_expires_at=int(datetime.now(timezone.utc).timestamp()) + 60,
    )
    assert decision["status"] == "waiting"
    records_db.rollback()

    resumed = asyncio.run(
        runtime.resume_clarification(
            records_db,
            enterprise_id=10,
            conversation_id=30,
            run_public_id="clarify-run",
            request_id=request_id,
            user_id=3,
            expected_token_version=0,
            expected_token_expires_at=int(datetime.now(timezone.utc).timestamp()) + 60,
        )
    )
    assert resumed["status"] == "awaiting_clarification"
    assert resumed["clarification_status"] == "waiting"
    assert resumed["pending"]["preview"]["request_id"] == request_id


def test_decision_rechecks_expired_token(records_db, monkeypatch):
    request_id = _pending(records_db, monkeypatch)
    with pytest.raises(AuthError):
        _decision(records_db, request_id, user_id=1, decision="allow", expires=0)


def test_author_can_close_stale_request_after_initiator_disabled(records_db, monkeypatch):
    request_id = _pending(records_db, monkeypatch)
    records_db.get(User, 3).status = 0
    records_db.commit()
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "stale"
    records_db.expire_all()
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    assert state.status == "failed"
    assert RunCheckpoint.from_dict(runtime._parse_json(state.checkpoint_json, message="bad")).pending is None


def test_new_high_authority_message_disables_resume_preview(records_db, monkeypatch):
    request_id = _pending(records_db, monkeypatch)
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "complete"
    records_db.add(
        EnterpriseChatMessage(
            id=110,
            enterprise_id=10,
            conversation_id=30,
            sender_id=1,
            sender_type="human",
            client_message_id="new-owner",
            content="@小菱 禁止创建团队",
        )
    )
    records_db.commit()
    preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=3
    )
    assert preview["status"] == "stale" and preview["can_resume"] is False
    assert _decision(records_db, request_id, user_id=1, decision="deny")["status"] == "stale"
    records_db.expire_all()
    from app.models.enterprise_assistant_clarification import (
        EnterpriseAssistantClarification,
        EnterpriseAssistantClarificationDecision,
    )

    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    checkpoint = RunCheckpoint.from_dict(runtime._parse_json(state.checkpoint_json, message="bad"))
    clarification = records_db.get(EnterpriseAssistantClarification, request_id)
    decision = records_db.query(EnterpriseAssistantClarificationDecision).filter_by(
        request_id=request_id, source_message_id=100
    ).one()
    assert state.status == "failed" and checkpoint.pending is None
    assert clarification.status == "cancelled"
    assert decision.decision == "allow"


def test_subscription_expiring_during_team_write_rolls_back(records_db, monkeypatch):
    from app.models.project import Project

    records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=3).one().role = "admin"
    records_db.commit()
    request_id = _pending(records_db, monkeypatch)
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "complete"
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    state.status = "running"
    records_db.commit()
    executor = runtime.EnterpriseAssistantToolExecutor(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        lease_generation=0,
        actor_user_id=3,
    )
    executor._clarification_resuming = True
    executor._clarification_token = (0, int(datetime.now(timezone.utc).timestamp()) + 60)
    expired, checks = [False], []

    def gate(*_args, **_kwargs):
        checks.append(expired[0])
        if expired[0]:
            raise ForbiddenError("企业订阅已过期")

    def current():
        gate()
        return state, {}

    def create(db, **_kwargs):
        db.add(Project(user_id=3, project_name="expired-probe", status="active"))
        db.flush()
        expired[0] = True
        return {"team_id": 1}

    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", gate)
    monkeypatch.setattr(executor, "_current", current)
    monkeypatch.setattr(runtime.enterprise_agent_team_service, "create_team_from_assistant", create)
    call = ToolCall(
        call_id="team-1",
        name="create_registered_enterprise_agent_team",
        arguments={"title": "团队"},
        raw_arguments="{}",
    )
    result = asyncio.run(executor.execute(call))
    assert result.failure_kind == "clarification_closed" and checks == [False, True]
    assert records_db.query(Project).filter_by(project_name="expired-probe").count() == 0


@pytest.mark.parametrize("initial_decision", ["allow", "deny", "unrelated"])
def test_higher_authority_review_is_available_for_every_lower_decision(records_db, monkeypatch,
                                                                      initial_decision):
    records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().role = "member"
    records_db.commit()
    request_id = _pending(records_db, monkeypatch, author_id=2)
    assert _decision(records_db, request_id, user_id=2, decision=initial_decision)["status"] in {
        "complete", "denied",
    }

    owner_preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=1
    )

    assert owner_preview["candidates"][0]["decision"] == initial_decision
    assert owner_preview["candidates"][0]["can_decide"] is True
    assert owner_preview["can_resume"] is False
    records_db.rollback()
    assert _decision(records_db, request_id, user_id=1, decision="deny")["status"] == "denied"
    waiting = asyncio.run(
        runtime.resume_clarification(
            records_db,
            enterprise_id=10,
            conversation_id=30,
            run_public_id="clarify-run",
            request_id=request_id,
            user_id=3,
            expected_token_version=0,
            expected_token_expires_at=int(datetime.now(timezone.utc).timestamp()) + 60,
        )
    )
    assert waiting["status"] == "awaiting_clarification" and waiting["clarification_status"] == "denied"


def test_resume_replays_exact_pending_call_after_decision(records_db, monkeypatch):
    from test_deepseek_responses_runtime import ScriptedTransport, _message_response

    from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime

    records_db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one().role = "member"
    records_db.commit()
    request_id = _pending(records_db, monkeypatch, author_id=2)
    assert _decision(records_db, request_id, user_id=2, decision="deny")["status"] == "denied"
    owner_preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=1
    )
    assert owner_preview["candidates"][0]["can_decide"] is True
    records_db.rollback()
    peer_preview = runtime.get_clarification(
        records_db, enterprise_id=10, conversation_id=30, run_public_id="clarify-run", user_id=4
    )
    assert peer_preview["candidates"][0]["can_decide"] is False
    records_db.rollback()
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "complete"
    records_db.add(BillingWallet(owner_type="enterprise", owner_id=10))
    records_db.commit()
    for owner, name in (
        (runtime, "_check_unresolved_billing"),
        (runtime.assistant_service, "_require_subscription"),
        (runtime.assistant_service, "_require_active_limit"),
    ):
        monkeypatch.setattr(owner, name, lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_validated_billing_group", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_a, **_k: {})
    monkeypatch.setattr(runtime.ledger, "available_milli", lambda *_a, **_k: 10)
    monkeypatch.setattr(runtime, "_billing_scope", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(runtime, "_result_for_runtime", lambda _factory, **kwargs: {"status": kwargs["result"].status})
    created = []
    monkeypatch.setattr(
        runtime.enterprise_agent_team_service,
        "create_team_from_assistant",
        lambda *_a, **kw: created.append(kw) or {"team_id": 41},
    )
    transport = ScriptedTransport([_message_response("完成")])

    def build(work, *, enterprise_id, conversation_id, run, state, actor_user_id):
        store = runtime.EnterpriseAssistantCheckpointStore(
            work,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            run_public_id=run.public_id,
            lease_generation=state.lease_generation,
            permission_snapshot=runtime._parse_json(state.permission_snapshot_json, message="bad"),
        )
        executor = runtime.EnterpriseAssistantToolExecutor(
            work,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            run_public_id=run.public_id,
            lease_generation=state.lease_generation,
            actor_user_id=actor_user_id,
        )
        monkeypatch.setattr(executor, "_current", lambda: (state, {}))
        machine = DeepSeekResponsesRuntime(transport=transport, tool_executor=executor, checkpoint_store=store)
        return machine, executor, store, "test"

    monkeypatch.setattr(runtime, "_build_runtime", build)
    result = asyncio.run(
        runtime.resume_clarification(
            records_db,
            enterprise_id=10,
            conversation_id=30,
            run_public_id="clarify-run",
            request_id=request_id,
            user_id=3,
            expected_token_version=0,
            expected_token_expires_at=int(datetime.now(timezone.utc).timestamp()) + 60,
        )
    )
    assert result["status"] == "completed"
    assert len(created) == 1 and created[0]["call_id"] == "team-1" and created[0]["payload"] == {"title": "团队"}


@pytest.mark.asyncio
async def test_closed_clarification_stops_remaining_calls_and_model():
    from test_deepseek_responses_runtime import RecordingExecutor, ScriptedTransport, _function_response, _runtime

    from app.services.deepseek_responses_runtime import ToolExecutionResult

    class Executor(RecordingExecutor):
        async def execute(self, call, *, approved=False):
            self.calls.append((call, approved))
            if len(self.calls) == 1:
                return ToolExecutionResult(status="clarification_required")
            return ToolExecutionResult.failure("stale", failure_kind="clarification_closed")

    transport = ScriptedTransport([_function_response(("first", "team", {}), ("later", "other", {}))])
    executor = Executor()
    machine = _runtime(transport, executor)
    assert (await machine.start("执行", run_id="closed")).status == WAITING_CLARIFICATION
    assert (await machine.resume_clarification("closed", "first")).status == "failed"
    assert [call.call_id for call, _ in executor.calls] == ["first", "first"]
    assert len(transport.payloads) == 1


@pytest.mark.asyncio
async def test_retry_cannot_bypass_interrupted_clarification_resume():
    from test_deepseek_responses_runtime import RecordingExecutor, ScriptedTransport, _function_response

    from app.services.deepseek_responses_runtime import (
        DeepSeekResponsesRuntime,
        InMemoryCheckpointStore,
        InvalidRunStateError,
        ToolExecutionResult,
    )

    class Store(InMemoryCheckpointStore):
        async def save(self, checkpoint):
            await super().save(checkpoint)
            if checkpoint.pending is None and checkpoint.context_metadata.get("clarification_resume_in_progress"):
                raise RuntimeError("simulated interruption")

    class Executor(RecordingExecutor):
        async def execute(self, call, *, approved=False):
            self.calls.append((call, approved))
            return ToolExecutionResult(status="clarification_required")

    executor, store = Executor(), Store()
    transport = ScriptedTransport([_function_response(("first", "team", {}), ("later", "other", {}))])
    machine = DeepSeekResponsesRuntime(transport=transport, tool_executor=executor, checkpoint_store=store)
    assert (await machine.start("执行", run_id="interrupted")).status == WAITING_CLARIFICATION
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await machine.resume_clarification("interrupted", "first")
    with pytest.raises(InvalidRunStateError):
        await machine.retry("interrupted")
    assert [call.call_id for call, _ in executor.calls] == ["first"]
    assert len(transport.payloads) == 1


def test_token_expires_after_team_write_rolls_back_before_checkpoint(records_db, monkeypatch):
    from app.models.project import Project

    request_id = _pending(records_db, monkeypatch)
    assert _decision(records_db, request_id, user_id=1, decision="allow")["status"] == "complete"
    state = records_db.get(EnterpriseAssistantRuntimeState, 300)
    state.status = "running"
    records_db.commit()
    executor = runtime.EnterpriseAssistantToolExecutor(
        records_db,
        enterprise_id=10,
        conversation_id=30,
        run_public_id="clarify-run",
        lease_generation=0,
        actor_user_id=3,
    )
    executor._clarification_resuming = True
    executor._clarification_token = (0, int(datetime.now(timezone.utc).timestamp()) + 60)
    monkeypatch.setattr(executor, "_current", lambda: (state, {}))

    def create(db, **_kwargs):
        db.add(Project(user_id=3, project_name="must-rollback", status="active"))
        db.flush()
        executor._clarification_token = (0, 0)
        return {"team_id": 1}

    monkeypatch.setattr(runtime.enterprise_agent_team_service, "create_team_from_assistant", create)
    call = ToolCall(
        call_id="team-1",
        name="create_registered_enterprise_agent_team",
        arguments={"title": "团队"},
        raw_arguments="{}",
    )
    result = asyncio.run(executor.execute(call))
    assert result.failure_kind == "clarification_closed"
    assert records_db.query(Project).filter_by(project_name="must-rollback").count() == 0
