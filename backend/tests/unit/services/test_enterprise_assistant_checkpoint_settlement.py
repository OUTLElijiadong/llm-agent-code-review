"""企业小菱已结算响应必须先落检查点，才能恢复同一运行。"""

import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.exceptions import ConflictError, ForbiddenError, PaymentRequiredError
from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingLimit,
    BillingRateCatalog,
    BillingRateCatalogEvent,
    BillingRateCatalogItem,
    BillingReservation,
    BillingReservationLimit,
    BillingReservationPart,
    BillingReservationReconciliationEvent,
    BillingWallet,
)
from app.models.enterprise_asset import EnterpriseAssistantAssetInvocation
from app.models.enterprise_assistant_runtime import EnterpriseAssistantApproval, EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage
from app.models.enterprise_org import EnterpriseAuditEvent
from app.services import billing_reservation_reconciliation_service as reconciliation
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.billing_ledger_service import available_milli, get_or_create_wallet, grant
from app.services.billing_runtime_gate import begin_attempt, finish_attempt, unknown_attempt
from app.services.deepseek_responses_runtime import PendingAction, RunCheckpoint, RuntimeResult, ToolCall


def _runtime_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = (
        EnterpriseAssistantRun.__table__,
        EnterpriseAssistantAssetInvocation.__table__,
        EnterpriseAssistantApproval.__table__,
        EnterpriseAssistantRuntimeState.__table__,
        EnterpriseChatMessage.__table__,
        EnterpriseAuditEvent.__table__,
        BillingWallet.__table__,
        BillingCreditBatch.__table__,
        BillingLimit.__table__,
        BillingReservation.__table__,
        BillingReservationPart.__table__,
        BillingReservationLimit.__table__,
        BillingReservationReconciliationEvent.__table__,
        BillingRateCatalog.__table__,
        BillingRateCatalogItem.__table__,
        BillingRateCatalogEvent.__table__,
    )
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    checkpoint = RunCheckpoint(
        run_id="ear_settlement_checkpoint",
        model="deepseek-flash",
        transcript=[{"role": "user", "content": "帮我总结"}],
        tools=[],
        status="running",
    )
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        wallet = get_or_create_wallet(db, "enterprise", 10)
        grant(db, wallet_id=wallet.id, grant_key="checkpoint-test", kind="purchase", amount_milli=100_000)
        run = EnterpriseAssistantRun(
            public_id=checkpoint.run_id,
            request_key="enterprise-assistant:10:20:30",
            enterprise_id=10,
            conversation_id=20,
            initiator_user_id=2,
            source_message_id=30,
            status="running",
            attempt_count=1,
            model_name="deepseek-flash",
            prompt_sha256=hashlib.sha256(b"prompt").hexdigest(),
        )
        db.add(run)
        db.flush()
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id,
            enterprise_id=10,
            conversation_id=20,
            initiator_user_id=2,
            source_message_id=30,
            status="running",
            checkpoint_json=runtime._json(checkpoint.to_dict()),
            permission_snapshot_json="{}",
            permission_snapshot_sha256=hashlib.sha256(b"{}").hexdigest(),
            lease_generation=1,
            lease_expires_at=runtime._now() + timedelta(minutes=10),
            billing_invocation_count=1,
            version=1,
        )
        db.add(state)
        db.flush()
        wallet_id = wallet.id
        run_id = run.id
        state_id = state.id
    return engine, factory, wallet_id, run_id, state_id, checkpoint


def _allow_runtime_guard(monkeypatch):
    monkeypatch.setattr(runtime, "_require_enterprise_active", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
    # This suite isolates persisted billing checkpoints without organization/chat tables.
    monkeypatch.setattr(runtime, "_require_model_send_clearance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_validate_checkpoint_attachments", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime.billing_checkout_service,
        "enterprise_subscription_status",
        lambda *_args, **_kwargs: "active",
    )
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)


def _runtime_snapshots(factory, run_id):
    with factory() as db:
        run = db.get(EnterpriseAssistantRun, run_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one()
        state_snapshot = SimpleNamespace(**{
            key: getattr(state, key)
            for key in (
                "id", "assistant_run_id", "enterprise_id", "conversation_id", "group_id",
                "initiator_user_id", "source_message_id", "status", "checkpoint_json",
                "permission_snapshot_json", "permission_snapshot_sha256", "stop_requested",
                "lease_generation", "lease_expires_at", "billing_invocation_count", "version", "error_code",
            )
        })
        run_snapshot = SimpleNamespace(**{
            key: getattr(run, key)
            for key in (
                "id", "public_id", "enterprise_id", "conversation_id", "group_id",
                "initiator_user_id", "source_message_id", "status", "attempt_count", "model_name",
            )
        })
    return run_snapshot, state_snapshot


def _model_attempt(factory, *, run_id, wallet_id, monkeypatch, settle=True):
    _allow_runtime_guard(monkeypatch)
    # This module isolates settlement/recovery behavior and has no enterprise
    # conversation tables; model-send authority is covered by its own suite.
    monkeypatch.setattr(runtime, "_require_model_send_clearance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.settings, "billing_commercial_metering_ready", True)
    run_snapshot, state_snapshot = _runtime_snapshots(factory, run_id)

    with runtime._billing_scope(factory, run=run_snapshot, state=state_snapshot, wallet_id=wallet_id):
        ticket = begin_attempt(
            model_name="deepseek-flash",
            payload={"model": "deepseek-flash", "input": "帮我总结", "max_output_tokens": 10},
            provider_name="deepseek",
            base_url="https://api.deepseek.com/v1",
            credential_source="system",
        )
        if settle:
            finish_attempt(ticket, {"input_tokens": 4, "output_tokens": 2})
    return ticket


def test_legacy_team_result_blocks_new_charge_and_publication_but_preserves_settlement(monkeypatch):
    engine, factory, wallet_id, run_id, state_id, checkpoint = _runtime_db()
    try:
        _model_attempt(factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch)
        checkpoint.transcript.extend([
            {"type": "function_call", "name": "get_enterprise_agent_team", "call_id": "old", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "old", "output": runtime._json({
                "status": "success", "output": {"team_id": 1, "objective": "历史私人结果"},
            })},
        ])
        with factory.begin() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            state.error_code = None
            state.checkpoint_json = runtime._json(checkpoint.to_dict())
            db.get(EnterpriseAssistantRun, run_id).error_code = None

        run_snapshot, state_snapshot = _runtime_snapshots(factory, run_id)
        with runtime._billing_scope(factory, run=run_snapshot, state=state_snapshot, wallet_id=wallet_id):
            with pytest.raises(ForbiddenError, match="旧版团队结果"):
                begin_attempt(
                    model_name="deepseek-flash",
                    payload={"model": "deepseek-flash", "input": "next", "max_output_tokens": 10},
                    provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
                )
        with factory.begin() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            checkpoint.status = "completed"
            checkpoint.output_text = "模型可能复述的历史私人结果"
            state.status = "completed"
            state.checkpoint_json = runtime._json(checkpoint.to_dict())
        monkeypatch.setattr(
            runtime.enterprise_org_service, "_company", lambda *_a, **_kw: SimpleNamespace(status="active")
        )
        monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_a: None)
        with pytest.raises(ForbiddenError):
            runtime._finalize_runtime(factory, enterprise_id=10, conversation_id=20, run_public_id=checkpoint.run_id)
        with factory() as db:
            assert db.get(EnterpriseAssistantRun, run_id).status == "suppressed"
            assert db.get(EnterpriseAssistantRun, run_id).response_text is None
            assert "历史私人结果" not in db.query(EnterpriseChatMessage).one().content
            assert db.get(EnterpriseAssistantRun, run_id).error_code == "legacy_team_result_unshareable"
            reservations = db.query(BillingReservation).all()
            assert len(reservations) == 1
            assert reservations[0].status == "settled"
    finally:
        engine.dispose()


def test_clarification_checkpoint_persists_and_releases_lease():
    engine, factory, _wallet_id, run_id, state_id, checkpoint = _runtime_db()
    try:
        checkpoint.status = "waiting_clarification"
        checkpoint.pending = PendingAction(
            kind="clarification", call=ToolCall(call_id="write", name="write", arguments={}, raw_arguments="{}"),
            remaining_calls=[ToolCall(call_id="later", name="later", arguments={}, raw_arguments="{}")],
        )
        with factory() as db:
            store = runtime.EnterpriseAssistantCheckpointStore(
                db, enterprise_id=10, conversation_id=20, run_public_id=checkpoint.run_id,
                lease_generation=1, permission_snapshot={},
            )
            asyncio.run(store.save(checkpoint))
        with factory() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            assert state.status == "waiting_clarification"
            assert state.lease_expires_at is None
            assert db.get(EnterpriseAssistantRun, run_id).status == "awaiting_clarification"
            store = runtime.EnterpriseAssistantCheckpointStore(
                db, enterprise_id=10, conversation_id=20, run_public_id=checkpoint.run_id,
                lease_generation=1, permission_snapshot={},
            )
            restored = asyncio.run(store.load(checkpoint.run_id))
            assert restored.pending.call.call_id == "write"
            assert restored.pending.remaining_calls[0].call_id == "later"
    finally:
        engine.dispose()


def test_authority_change_after_approval_is_not_reported_as_billing_error():
    engine, factory, _wallet_id, _run_id, state_id, checkpoint = _runtime_db()
    try:
        checkpoint.status = "failed"
        checkpoint.context_metadata["non_retryable_tool_response"] = {"failure_kind": "clarification_after_approval"}
        with factory.begin() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            state.checkpoint_json = runtime._json(checkpoint.to_dict())
        result = runtime._result_for_runtime(
            factory, enterprise_id=10, conversation_id=20, run_public_id=checkpoint.run_id,
            result=RuntimeResult(run_id=checkpoint.run_id, status="failed"),
        )
        assert "权限需要重新澄清" in result["message"].content
        assert "账单" not in result["message"].content
    finally:
        engine.dispose()


def test_successfully_checkpointed_provider_result_clears_pending_marker(monkeypatch):
    engine, factory, wallet_id, run_id, _state_id, checkpoint = _runtime_db()
    try:
        _model_attempt(factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch)
        with factory() as db:
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one()
            reservation = db.query(BillingReservation).one()
            assert reservation.status == "settled"
            assert state.error_code == "provider_attempt_pending_reconciliation"

        checkpoint.output_text = "已经持久化的答复"
        checkpoint.last_response = {"id": "response-safe", "status": "completed"}
        checkpoint.status = "completed"
        with factory() as db:
            store = runtime.EnterpriseAssistantCheckpointStore(
                db,
                enterprise_id=10,
                conversation_id=20,
                run_public_id=checkpoint.run_id,
                lease_generation=1,
                permission_snapshot={},
            )
            asyncio.run(store.save(checkpoint))

        with factory() as db:
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one()
            assert state.status == "completed"
            assert state.error_code is None
            assert db.query(BillingReservation).one().status == "settled"
            assert available_milli(db, wallet_id) < 100_000
    finally:
        engine.dispose()


def test_expired_settled_but_uncheckpointed_run_cannot_be_retried(monkeypatch):
    engine, factory, wallet_id, run_id, state_id, _checkpoint = _runtime_db()
    try:
        _model_attempt(factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch)
        with factory.begin() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            state.lease_expires_at = runtime._now() - timedelta(seconds=1)

        monkeypatch.setattr(
            runtime.enterprise_chat_service,
            "get_conversation",
            lambda *_args, **_kwargs: SimpleNamespace(kind="company", group_id=None),
        )
        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)
        monkeypatch.setattr(runtime, "_require_run_retry_controller", lambda *_args, **_kwargs: None)
        with factory() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run = db.get(EnterpriseAssistantRun, run_id)
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "awaiting_reconciliation"
            assert payload["recoverable"] is False
            assert payload["error"] == "provider_result_missing_after_settlement"
        try:
            asyncio.run(runtime.retry_run(
                factory(), enterprise_id=10, user_id=2, conversation_id=20,
                run_public_id="ear_settlement_checkpoint",
            ))
            raise AssertionError("已结算但未持久化响应的运行不得重新请求模型")
        except PaymentRequiredError as exc:
            assert "检查点" in str(exc) or "核对" in str(exc)

        with factory() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run = db.get(EnterpriseAssistantRun, run_id)
            assert state.error_code == "provider_attempt_pending_reconciliation"
            assert db.query(BillingReservation).count() == 1
            assert db.query(BillingReservation).one().status == "settled"
            assert run.attempt_count == 1
            assert available_milli(db, wallet_id) < 100_000
    finally:
        engine.dispose()


def test_reserved_but_unconfirmed_provider_attempt_cannot_be_retried(monkeypatch):
    engine, factory, wallet_id, run_id, state_id, _checkpoint = _runtime_db()
    try:
        ticket = _model_attempt(
            factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch, settle=False,
        )
        assert ticket is not None
        with factory.begin() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            state.lease_expires_at = runtime._now() - timedelta(seconds=1)
        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)
        with factory() as db:
            reservation = db.query(BillingReservation).one()
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run = db.get(EnterpriseAssistantRun, run_id)
            assert reservation.status == "reserved"
            assert state.error_code == "provider_attempt_pending_reconciliation"
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "awaiting_reconciliation"
            assert payload["recoverable"] is False

        monkeypatch.setattr(
            runtime.enterprise_chat_service,
            "get_conversation",
            lambda *_args, **_kwargs: SimpleNamespace(kind="company", group_id=None),
        )
        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)
        monkeypatch.setattr(runtime, "_require_run_retry_controller", lambda *_args, **_kwargs: None)
        try:
            asyncio.run(runtime.retry_run(
                factory(), enterprise_id=10, user_id=2, conversation_id=20,
                run_public_id="ear_settlement_checkpoint",
            ))
            raise AssertionError("发送状态未知的模型请求不得重试")
        except PaymentRequiredError as exc:
            assert "发送" in str(exc) or "核对" in str(exc)

        with factory() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run = db.get(EnterpriseAssistantRun, run_id)
            reservation = db.query(BillingReservation).one()
            assert state.error_code == "provider_attempt_pending_reconciliation"
            assert run.attempt_count == 1
            assert reservation.status == "reserved"
            assert db.query(BillingReservation).count() == 1
    finally:
        engine.dispose()


def test_active_provider_attempt_is_not_presented_as_reconciliation(monkeypatch):
    engine, factory, _wallet_id, run_id, state_id, _checkpoint = _runtime_db()
    try:
        _model_attempt(factory, run_id=run_id, wallet_id=_wallet_id, monkeypatch=monkeypatch, settle=False)
        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)
        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "running"
            assert payload["error"] is None
            assert payload["recoverable"] is False
    finally:
        engine.dispose()


def test_transport_failure_keeps_unknown_billing_non_recoverable(monkeypatch):
    engine, factory, wallet_id, run_id, state_id, checkpoint = _runtime_db()
    try:
        ticket = _model_attempt(
            factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch, settle=False,
        )
        unknown_attempt(ticket)
        checkpoint.status = "failed"
        checkpoint.error = "Responses transport 调用失败: upstream unavailable"
        with factory() as db:
            store = runtime.EnterpriseAssistantCheckpointStore(
                db,
                enterprise_id=10,
                conversation_id=20,
                run_public_id=checkpoint.run_id,
                lease_generation=1,
                permission_snapshot={},
            )
            asyncio.run(store.save(checkpoint))

        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)
        with factory() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run = db.get(EnterpriseAssistantRun, run_id)
            reservation = db.query(BillingReservation).one()
            payload = runtime._status_payload(db, run, state)
            assert reservation.status == "unknown"
            assert state.status == "failed"
            assert state.error_code == "provider_attempt_pending_reconciliation"
            assert payload["status"] == "awaiting_reconciliation"
            assert payload["recoverable"] is False
            assert payload["error"] == "provider_attempt_pending_reconciliation"
    finally:
        engine.dispose()


def test_unresolved_prior_reservation_blocks_next_provider_attempt(monkeypatch):
    engine, factory, wallet_id, run_id, state_id, _checkpoint = _runtime_db()
    try:
        ticket = _model_attempt(
            factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch, settle=False,
        )
        unknown_attempt(ticket)
        with factory.begin() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            state.error_code = None
        run_snapshot, state_snapshot = _runtime_snapshots(factory, run_id)

        with runtime._billing_scope(factory, run=run_snapshot, state=state_snapshot, wallet_id=wallet_id):
            try:
                begin_attempt(
                    model_name="deepseek-flash",
                    payload={"model": "deepseek-flash", "input": "再次请求", "max_output_tokens": 10},
                    provider_name="deepseek",
                    base_url="https://api.deepseek.com/v1",
                    credential_source="system",
                )
                raise AssertionError("未知账单存在时不得开始新的供应商请求")
            except PaymentRequiredError as exc:
                assert "未结算" in str(exc) or "核对" in str(exc)

        with factory() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            reservations = db.query(BillingReservation).all()
            assert state.error_code is None
            assert len(reservations) == 1
            assert reservations[0].status == "unknown"
    finally:
        engine.dispose()


def test_queued_worker_keeps_unknown_attempt_in_reconciliation(monkeypatch):
    engine, factory, _wallet_id, run_id, state_id, checkpoint = _runtime_db()
    try:
        checkpoint.status = "queued"
        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run.status = "queued"
            state.status = "queued"
            state.checkpoint_json = runtime._json(checkpoint.to_dict())
            state.lease_expires_at = runtime._now() - timedelta(seconds=1)

        _allow_runtime_guard(monkeypatch)
        monkeypatch.setattr(runtime.settings, "billing_commercial_metering_ready", True)
        monkeypatch.setattr(runtime.assistant_service, "_check_scope", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(runtime, "_ensure_message", lambda *_args, **_kwargs: None)

        class FailingRuntime:
            async def drive_persisted(self, _run_public_id):
                ticket = begin_attempt(
                    model_name="deepseek-flash",
                    payload={"model": "deepseek-flash", "input": "帮我总结", "max_output_tokens": 10},
                    provider_name="deepseek",
                    base_url="https://api.deepseek.com/v1",
                    credential_source="system",
                )
                unknown_attempt(ticket)
                raise RuntimeError("模拟响应检查点写入路径失败")

        monkeypatch.setattr(
            runtime,
            "_build_runtime",
            lambda *_args, **_kwargs: (FailingRuntime(), None, None, None),
        )
        asyncio.run(runtime.execute_queued_run(
            engine, enterprise_id=10, conversation_id=20,
            run_public_id="ear_settlement_checkpoint",
        ))

        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            reservation = db.query(BillingReservation).one()
            assert state.status == "awaiting_reconciliation"
            assert state.lease_expires_at is None
            assert state.error_code == "provider_attempt_pending_reconciliation"
            assert run.status == "awaiting_reconciliation"
            assert run.error_code == "provider_attempt_pending_reconciliation"
            assert reservation.status == "unknown"
            assert RunCheckpoint.from_dict(runtime._parse_json(
                state.checkpoint_json, message="检查点解析失败",
            )).status == "running"
    finally:
        engine.dispose()


def test_cancel_response_exposes_pending_billing_marker_to_content_reader(monkeypatch):
    engine, factory, _wallet_id, _run_id, state_id, _checkpoint = _runtime_db()
    try:
        with factory.begin() as db:
            db.get(EnterpriseAssistantRuntimeState, state_id).error_code = (
                "provider_attempt_pending_reconciliation"
            )
        monkeypatch.setattr(
            runtime, "_run_control_room", lambda *_args: SimpleNamespace(kind="company", group_id=None),
        )
        monkeypatch.setattr(runtime, "_require_run_controller", lambda *_args: None)
        monkeypatch.setattr(runtime, "_can_read_run_content", lambda *_args: True)

        payload = runtime.cancel_run(
            factory(), enterprise_id=10, user_id=2, conversation_id=20,
            run_public_id="ear_settlement_checkpoint", reason="会话成员主动停止",
        )

        assert payload["status"] == "cancelled"
        assert payload["error"] == "provider_attempt_pending_reconciliation"
    finally:
        engine.dispose()


def test_cancel_response_hides_pending_billing_marker_from_non_reader(monkeypatch):
    engine, factory, _wallet_id, _run_id, state_id, _checkpoint = _runtime_db()
    try:
        with factory.begin() as db:
            db.get(EnterpriseAssistantRuntimeState, state_id).error_code = (
                "provider_attempt_pending_reconciliation"
            )
        monkeypatch.setattr(
            runtime, "_run_control_room", lambda *_args: SimpleNamespace(kind="group", group_id=7),
        )
        monkeypatch.setattr(runtime, "_require_run_controller", lambda *_args: None)
        monkeypatch.setattr(runtime, "_can_read_run_content", lambda *_args: False)

        payload = runtime.cancel_run(
            factory(), enterprise_id=10, user_id=9, conversation_id=20,
            run_public_id="ear_settlement_checkpoint", reason="管理员停止",
        )

        assert payload["status"] == "cancelled"
        assert "error" not in payload
        assert "message" not in payload
    finally:
        engine.dispose()


def test_retryable_status_waits_for_lease_expiry_before_advertising_recovery(monkeypatch):
    engine, factory, _wallet_id, run_id, state_id, checkpoint = _runtime_db()
    try:
        checkpoint.status = "failed"
        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run.status = "retryable"
            state.status = "failed"
            state.checkpoint_json = runtime._json(checkpoint.to_dict())
            state.lease_expires_at = runtime._now() + timedelta(minutes=1)
        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)

        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            assert runtime._status_payload(db, run, state)["recoverable"] is False

        with factory.begin() as db:
            db.get(EnterpriseAssistantRuntimeState, state_id).lease_expires_at = (
                runtime._now() - timedelta(seconds=1)
            )
        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            assert runtime._status_payload(db, run, state)["recoverable"] is True
    finally:
        engine.dispose()


def test_external_tool_halt_is_nonrecoverable_and_service_rejects_retry_before_claim(monkeypatch):
    engine, factory, _wallet_id, run_id, state_id, checkpoint = _runtime_db()
    try:
        checkpoint.status = "failed"
        checkpoint.context_metadata["non_retryable_tool_response"] = {
            "response_id": "resp-halted",
            "failed_call_id": "call_search",
            "failure_kind": "tool_result_missing",
            "discarded_call_ids": ["call_write"],
        }
        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run.status = "retryable"
            state.status = "failed"
            state.checkpoint_json = runtime._json(checkpoint.to_dict())
            state.lease_expires_at = runtime._now() - timedelta(seconds=1)
        monkeypatch.setattr(
            runtime.enterprise_chat_service,
            "get_conversation",
            lambda *_args, **_kwargs: SimpleNamespace(kind="company", group_id=None),
        )
        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)
        monkeypatch.setattr(runtime, "_require_run_retry_controller", lambda *_args, **_kwargs: None)

        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            visible = runtime._status_payload(db, run, state)
            assert visible["status"] == "failed"
            assert visible["recoverable"] is False
            before = (state.lease_generation, state.billing_invocation_count, state.version, run.attempt_count)

        with pytest.raises(ConflictError, match="发送新消息"):
            asyncio.run(runtime.retry_run(
                factory(), enterprise_id=10, user_id=2, conversation_id=20,
                run_public_id="ear_settlement_checkpoint",
            ))

        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            after = (state.lease_generation, state.billing_invocation_count, state.version, run.attempt_count)
            assert after == before
            assert state.status == "failed" and run.status == "retryable"
            assert state.lease_expires_at <= runtime._now()
    finally:
        engine.dispose()


def test_approved_not_sent_reconciliation_enables_only_explicit_fenced_retry(monkeypatch):
    engine, factory, wallet_id, run_id, state_id, _checkpoint = _runtime_db()
    try:
        ticket = _model_attempt(
            factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch, settle=False,
        )
        unknown_attempt(ticket)
        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run.status = "awaiting_reconciliation"
            state.status = "awaiting_reconciliation"
            state.lease_expires_at = None
            reservation = db.query(BillingReservation).one()
            proposal = reconciliation.propose_reconciliation(
                db,
                reservation_id=reservation.id,
                actor_id=101,
                resolution="release_not_sent",
                evidence_reference="ops-case-101",
                evidence_sha256="a" * 64,
                note="供应商日志证明没有创建请求",
                provider_request_id=None,
                input_tokens=None,
                output_tokens=None,
            )
            released, approval = reconciliation.decide_reconciliation(
                db,
                reservation_id=reservation.id,
                actor_id=102,
                decision="approve",
                note="复核供应商原始日志，确认请求未送达",
            )
            assert released.status == "released"
            assert proposal.decision == "proposed"
            assert approval.decision == "approved"

        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "retryable"
            assert payload["error"] == "provider_attempt_released_not_sent"
            assert payload["recoverable"] is True
        old_run, old_state = _runtime_snapshots(factory, run_id)

        monkeypatch.setattr(runtime, "_require_enterprise_active", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            runtime.enterprise_chat_service, "get_conversation",
            lambda *_args, **_kwargs: SimpleNamespace(kind="company"),
        )
        monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
        with factory.begin() as db:
            runtime._claim_new_invocation(
                db,
                enterprise_id=10,
                conversation_id=20,
                run_public_id="ear_settlement_checkpoint",
                allow_statuses=runtime._RETRYABLE | {"queued"},
                actor_user_id=2,
            )
            current_state = db.get(EnterpriseAssistantRuntimeState, state_id)
            assert current_state.error_code is None
            assert current_state.lease_generation == 2
            assert current_state.billing_invocation_count == 2

        with runtime._billing_scope(
            factory, run=old_run, state=old_state, wallet_id=wallet_id,
        ):
            try:
                begin_attempt(
                    model_name="deepseek-flash",
                    payload={"model": "deepseek-flash", "input": "旧 worker 不得继续", "max_output_tokens": 10},
                    provider_name="deepseek",
                    base_url="https://api.deepseek.com/v1",
                    credential_source="system",
                )
                raise AssertionError("C4 释放后的旧运行代际不得再次发出 provider 请求")
            except ForbiddenError as exc:
                assert "租约" in str(exc) or "停止" in str(exc)

        with factory() as db:
            assert db.query(BillingReservation).count() == 1
    finally:
        engine.dispose()


def test_settled_attempt_without_checkpoint_remains_non_recoverable(monkeypatch):
    engine, factory, wallet_id, run_id, state_id, _checkpoint = _runtime_db()
    try:
        _model_attempt(factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch, settle=True)
        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run.status = "awaiting_reconciliation"
            state.status = "awaiting_reconciliation"
            state.lease_expires_at = None

        monkeypatch.setattr(runtime.assistant_service, "_result_message", lambda *_args: None)
        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "awaiting_reconciliation"
            assert payload["error"] == "provider_result_missing_after_settlement"
            assert payload["recoverable"] is False
    finally:
        engine.dispose()


def test_suppressed_run_cannot_be_revived_by_not_sent_reconciliation(monkeypatch):
    engine, factory, _wallet_id, run_id, state_id, _checkpoint = _runtime_db()
    try:
        monkeypatch.setattr(runtime, "_require_enterprise_active", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            runtime.enterprise_chat_service, "get_conversation",
            lambda *_args, **_kwargs: SimpleNamespace(kind="company"),
        )
        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run.status = "suppressed"
            state.status = "suppressed"
            state.error_code = "provider_attempt_pending_reconciliation"
            db.flush()
            try:
                runtime._claim_new_invocation(
                    db,
                    enterprise_id=10,
                    conversation_id=20,
                    run_public_id="ear_settlement_checkpoint",
                    allow_statuses=runtime._RETRYABLE | {"queued"},
                    actor_user_id=2,
                )
                raise AssertionError("被权限优先级规则抑制的运行不得被账单释放恢复")
            except ConflictError as exc:
                assert "抑制" in str(exc)
            assert state.status == "suppressed"
            assert state.error_code == "provider_attempt_pending_reconciliation"
    finally:
        engine.dispose()


@pytest.mark.parametrize("team_child", [False, True], ids=["direct-asset", "team-derived-child"])
def test_approved_not_sent_asset_child_allows_only_explicit_fenced_parent_retry(monkeypatch, team_child):
    engine, factory, wallet_id, run_id, state_id, checkpoint = _runtime_db()
    try:
        _model_attempt(factory, run_id=run_id, wallet_id=wallet_id, monkeypatch=monkeypatch, settle=True)
        parent_call_id = "asset-team-c4-tool-call" if team_child else "asset-c4-tool-call"
        if team_child:
            arguments = {
                "objective": "整理本次测试计划",
                "steps": [{"publication_id": 1, "task": "梳理验收条件"}],
            }
            child_tool_call_id = "team_" + hashlib.sha256(
                f"{checkpoint.run_id}:{parent_call_id}:1".encode("utf-8")
            ).hexdigest()[:48]
            tool_name = "run_approved_enterprise_asset_team"
        else:
            arguments = {"publication_id": 1, "task": "整理本次测试计划"}
            child_tool_call_id = parent_call_id
            tool_name = "invoke_published_enterprise_asset"
        call = {
            "type": "function_call",
            "call_id": parent_call_id,
            "name": tool_name,
            "arguments": runtime._json(arguments),
        }
        checkpoint.status = "running"
        checkpoint.last_response = {"status": "completed", "output": [call]}
        checkpoint.transcript = [{"role": "user", "content": "调用已发布资产"}, call]
        with factory() as db:
            store = runtime.EnterpriseAssistantCheckpointStore(
                db,
                enterprise_id=10,
                conversation_id=20,
                run_public_id=checkpoint.run_id,
                lease_generation=1,
                permission_snapshot={},
            )
            asyncio.run(store.save(checkpoint))

        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            child = EnterpriseAssistantAssetInvocation(
                enterprise_id=10,
                conversation_id=20,
                assistant_run_id=run.id,
                initiator_user_id=2,
                tool_call_id=child_tool_call_id,
                invocation_id="asset-c4-invocation",
                publication_id=1,
                publication_version_id=1,
                publication_sha256="b" * 64,
                input_sha256=hashlib.sha256("整理本次测试计划".encode()).hexdigest(),
                request_key_prefix="enterprise-asset:asset-c4-invocation",
                status="running",
                execution_token="asset-c4-old-token",
                lease_expires_at=runtime._now() + timedelta(minutes=5),
            )
            db.add(child)
            db.flush()
            child_id = int(child.id)

        _allow_runtime_guard(monkeypatch)
        monkeypatch.setattr(runtime.settings, "billing_commercial_metering_ready", True)
        run_snapshot, state_snapshot = _runtime_snapshots(factory, run_id)
        with runtime._billing_scope(
            factory,
            run=run_snapshot,
            state=state_snapshot,
            wallet_id=wallet_id,
            request_key_prefix="enterprise-asset:asset-c4-invocation",
            asset_invocation_id=child_id,
            asset_execution_token="asset-c4-old-token",
        ):
            child_ticket = begin_attempt(
                model_name="deepseek-flash",
                payload={"model": "deepseek-flash", "input": "子调用", "max_output_tokens": 10},
                provider_name="deepseek",
                base_url="https://api.deepseek.com/v1",
                credential_source="system",
            )
            unknown_attempt(child_ticket)

        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            run.status = "awaiting_reconciliation"
            state.status = "awaiting_reconciliation"
            state.lease_expires_at = None
            child = db.query(EnterpriseAssistantAssetInvocation).one()
            child.lease_expires_at = runtime._now() + timedelta(minutes=1)

        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "awaiting_reconciliation"
            assert payload["recoverable"] is False

        with factory.begin() as db:
            child = db.query(EnterpriseAssistantAssetInvocation).one()
            child.lease_expires_at = runtime._now() - timedelta(minutes=1)

        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "awaiting_reconciliation"
            assert payload["recoverable"] is False

        with factory.begin() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            reservation = db.query(BillingReservation).filter(
                BillingReservation.request_key.startswith("enterprise-asset:asset-c4-invocation:")
            ).one()
            proposal = reconciliation.propose_reconciliation(
                db,
                reservation_id=reservation.id,
                actor_id=101,
                resolution="release_not_sent",
                evidence_reference="provider-child-no-request",
                evidence_sha256="c" * 64,
                note="供应商日志证明子调用未发送",
                provider_request_id=None,
                input_tokens=None,
                output_tokens=None,
            )
            released, approval = reconciliation.decide_reconciliation(
                db,
                reservation_id=reservation.id,
                actor_id=102,
                decision="approve",
                note="复核原始日志，确认子调用请求未送达",
            )
            assert released.status == "released"
            assert proposal.decision == "proposed"
            assert approval.decision == "approved"

        with factory() as db:
            run = db.get(EnterpriseAssistantRun, run_id)
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            payload = runtime._status_payload(db, run, state)
            assert payload["status"] == "retryable"
            assert payload["error"] == "provider_attempt_released_not_sent"
            assert payload["recoverable"] is True

        monkeypatch.setattr(runtime, "_require_enterprise_active", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            runtime.enterprise_chat_service,
            "get_conversation",
            lambda *_args, **_kwargs: SimpleNamespace(kind="company"),
        )
        monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
        with factory.begin() as db:
            runtime._claim_new_invocation(
                db,
                enterprise_id=10,
                conversation_id=20,
                run_public_id="ear_settlement_checkpoint",
                allow_statuses=runtime._RETRYABLE | {"queued"},
                actor_user_id=2,
            )
            current_state = db.get(EnterpriseAssistantRuntimeState, state_id)
            assert current_state.error_code is None
            assert current_state.lease_generation == 2
            assert current_state.billing_invocation_count == 2
    finally:
        engine.dispose()
