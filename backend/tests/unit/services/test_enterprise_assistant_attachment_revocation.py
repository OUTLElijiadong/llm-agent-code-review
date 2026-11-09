"""已删除或改版附件不能经持久小菱检查点继续进入新模型请求。"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import (
    BillingRateCatalog,
    BillingRateCatalogEvent,
    BillingRateCatalogItem,
    BillingReservation,
)
from app.models.enterprise_asset import EnterpriseAssistantAssetInvocation
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.models.enterprise_knowledge import EnterpriseKnowledgeAttachment
from app.models.enterprise_org import Enterprise
from app.services import billing_runtime_gate
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import RunCheckpoint


@pytest.mark.parametrize("source", ["initial_context", "knowledge_tool_result"])
@pytest.mark.parametrize("current_status,current_version", [("deleted", 2), ("active", 2)])
def test_revoked_attachment_reference_blocks_paid_provider_attempt(
    monkeypatch, source, current_status, current_version,
):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        Enterprise.__table__, EnterpriseAssistantRun.__table__, EnterpriseAssistantRuntimeState.__table__,
        EnterpriseKnowledgeAttachment.__table__, EnterpriseAssistantAssetInvocation.__table__,
        BillingReservation.__table__, BillingRateCatalog.__table__, BillingRateCatalogItem.__table__,
        BillingRateCatalogEvent.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        db.add(Enterprise(
            id=10, name="附件撤權企業", owner_id=2, seat_limit=3,
            status="active", paid_order_id="attachment-revocation-order",
        ))
        attachment = EnterpriseKnowledgeAttachment(
            id=7, enterprise_id=10, author_id=2, updated_by=2,
            client_request_id="attachment-revocation", create_hash="a" * 64,
            file_name="policy.txt", file_ext=".txt", mime="text/plain", file_size=10,
            chunk_count=1, source_sha256="b" * 64, original_data=None, extracted_text=None,
            version=current_version, status=current_status,
        )
        db.add(attachment)
        run = EnterpriseAssistantRun(
            public_id="ear_attachment_revocation", request_key="attachment-revocation-run",
            enterprise_id=10, conversation_id=20, initiator_user_id=2, source_message_id=30,
            status="running", attempt_count=1, model_name="deepseek-v4-flash", prompt_sha256="c" * 64,
        )
        db.add(run)
        db.flush()
        if source == "initial_context":
            checkpoint = RunCheckpoint(
                run_id=run.public_id, model="deepseek-v4-flash", transcript=[], tools=[],
                context_metadata={"enterprise_knowledge_attachment_refs": [{"attachment_id": 7, "version": 1}]},
                status=runtime.RUNNING,
            )
        else:
            checkpoint = RunCheckpoint(
                run_id=run.public_id, model="deepseek-v4-flash", transcript=[{
                    "type": "function_call_output", "call_id": "knowledge-search",
                    "output": json.dumps({"status": "success", "output": {
                        "attachment_items": [{"attachment_id": 7, "version": 1}],
                    }}),
                }], tools=[], status=runtime.RUNNING,
            )
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id, enterprise_id=10, conversation_id=20, initiator_user_id=2,
            source_message_id=30, status=runtime.RUNNING, checkpoint_json=json.dumps(checkpoint.to_dict()),
            permission_snapshot_json="{}", permission_snapshot_sha256="d" * 64,
            stop_requested=False, cancel_reason="", lease_generation=4,
            billing_invocation_count=1, version=1,
        )
        db.add(state)

    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.services.billing_checkout_service.enterprise_subscription_status",
                        lambda *_args, **_kwargs: "active")
    monkeypatch.setattr(billing_runtime_gate.ledger, "quote_milli", lambda *_args, **_kwargs: 100)
    reservations = []
    monkeypatch.setattr(billing_runtime_gate.ledger, "reserve",
                        lambda *_args, **_kwargs: reservations.append("reserve"))
    with factory() as db:
        persisted_state = db.query(EnterpriseAssistantRuntimeState).one()
        persisted_run = db.query(EnterpriseAssistantRun).one()
        run_record = SimpleNamespace(
            id=int(persisted_run.id), public_id="ear_attachment_revocation", enterprise_id=10,
            conversation_id=20,
            initiator_user_id=2,
        )
        scope = runtime._billing_scope(
            factory, run=run_record, state=persisted_state, wallet_id=1,
        )
    provider_calls = []
    with scope:
        with pytest.raises(ForbiddenError, match="附件已删除或更新"):
            billing_runtime_gate.begin_attempt(
                model_name="deepseek-v4-flash",
                payload={"model": "deepseek-v4-flash", "input": "请继续处理", "max_output_tokens": 20},
                provider_name="deepseek", base_url="https://api.deepseek.com/v1",
                credential_source="system",
            )
    assert provider_calls == []
    assert reservations == []
    engine.dispose()
