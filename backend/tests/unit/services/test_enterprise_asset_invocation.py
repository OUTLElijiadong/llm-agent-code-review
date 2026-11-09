"""Published prompt assets run only as metered, idempotent enterprise child calls."""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingLimit,
    BillingOrder,
    BillingPaymentEvent,
    BillingRateCatalog,
    BillingRateCatalogEvent,
    BillingRateCatalogItem,
    BillingRefund,
    BillingReservation,
    BillingReservationLimit,
    BillingReservationPart,
    BillingReservationReconciliationEvent,
    BillingSubscription,
    BillingSubscriptionPeriod,
    BillingWallet,
)
from app.models.enterprise_asset import (
    EnterpriseAssetDraft,
    EnterpriseAssetDraftVersion,
    EnterpriseAssetPublication,
    EnterpriseAssistantAssetInvocation,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseAuditEvent, EnterpriseMember
from app.models.user import User
from app.services import billing_reservation_reconciliation_service as reconciliation
from app.services import enterprise_asset_service as assets
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import ToolCall


@pytest.fixture
def asset_runtime(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        User.__table__, Enterprise.__table__, EnterpriseMember.__table__, EnterpriseAuditEvent.__table__,
        EnterpriseConversation.__table__, EnterpriseAssistantRun.__table__,
        EnterpriseAssistantRuntimeState.__table__, EnterpriseAssetDraft.__table__,
        EnterpriseAssetDraftVersion.__table__, EnterpriseAssetPublication.__table__,
        EnterpriseAssistantAssetInvocation.__table__, BillingWallet.__table__,
        BillingCreditBatch.__table__, BillingLimit.__table__, BillingReservation.__table__,
        BillingReservationPart.__table__, BillingReservationLimit.__table__, BillingOrder.__table__,
        BillingPaymentEvent.__table__, BillingRefund.__table__, BillingReservationReconciliationEvent.__table__,
        BillingSubscription.__table__,
        BillingSubscriptionPeriod.__table__,
        BillingRateCatalog.__table__, BillingRateCatalogItem.__table__, BillingRateCatalogEvent.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snapshot = {"enterprise_id": 10, "conversation_id": 20, "initiator_user_id": 2, "messages": []}
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        db.add_all([
            User(id=1, username="asset-owner", password="x", role="user", status=1),
            User(id=2, username="asset-member", password="x", role="user", status=1),
        ])
        db.add(Enterprise(id=10, name="资产企业", owner_id=1, seat_limit=5,
                          status="active", paid_order_id="asset-enterprise"))
        db.add_all([
            EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
        ])
        db.add(EnterpriseConversation(
            id=20, enterprise_id=10, kind="company", conversation_key="company", created_by=1,
        ))
        run = EnterpriseAssistantRun(
            public_id="ear_asset_invocation", request_key="asset-invocation-run", enterprise_id=10,
            conversation_id=20, initiator_user_id=2, source_message_id=30,
            status="running", attempt_count=1, prompt_sha256="c" * 64,
        )
        db.add(run)
        db.flush()
        db.add(EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id, enterprise_id=10, conversation_id=20, initiator_user_id=2,
            source_message_id=30, status="running", checkpoint_json="{}",
            permission_snapshot_json="{}", permission_snapshot_sha256=runtime._digest(snapshot),
            stop_requested=False, cancel_reason="", lease_generation=4,
            lease_expires_at=now + timedelta(minutes=5), billing_invocation_count=1, version=1,
        ))
        wallet = BillingWallet(owner_type="enterprise", owner_id=10)
        db.add(wallet)
        db.flush()
        db.add(BillingCreditBatch(
            wallet_id=wallet.id, grant_key="asset-invocation-credit", kind="purchase",
            issued_milli=1_000_000, remaining_milli=1_000_000, reserved_milli=0,
            refund_reserved_milli=0, expires_at=None,
        ))
        db.add(BillingLimit(
            limit_key="normal:10:asset-invocation", enterprise_id=10, category="normal",
            group_id=None, user_id=None, period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=30), limit_milli=1_000_000, used_milli=0, reserved_milli=0,
        ))

    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime.assistant_service, "_model_config", lambda _db: (
        SimpleNamespace(api_key="platform-key", base_url="https://api.deepseek.com/v1",
                        model="deepseek-v4-flash", provider="deepseek", source="system",
                        timeout_seconds=5),
        "deepseek-v4-flash",
    ))
    monkeypatch.setattr("app.services.billing_checkout_service.enterprise_subscription_status",
                        lambda *_args, **_kwargs: "active")

    with factory() as db:
        draft = assets.create_draft(
            db, 10, 2, kind="agent", title="只读归纳",
            definition={"schema_version": 1, "runtime": "prompt", "system_prompt": "用简洁中文归纳。"},
            request_key="asset-invocation-draft",
        )
        publication = assets.request_publication(
            db, 10, 2, draft.id, scope="enterprise", expected_version=1,
            request_key="asset-invocation-publication",
        )
        publication = assets.decide_enterprise_publication(
            db, 10, publication.id, 1, approve=True, reason="运行测试批准",
        )
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=10, conversation_id=20, run_public_id="ear_asset_invocation",
            lease_generation=4, actor_user_id=2,
        )
    # Asset execution/billing tests use minimal synthetic run rows. The durable
    # model-send authority contract is exercised with real snapshots elsewhere.
    monkeypatch.setattr(runtime, "_require_model_send_clearance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime.EnterpriseAssistantToolExecutor, "_action_clarification", lambda *_args: None,
    )

    yield engine, factory, publication, executor
    engine.dispose()


def _call(publication_id: int, *, task="整理本次测试计划", call_id="asset-tool-call-1"):
    import json

    arguments = {"publication_id": publication_id, "task": task}
    return ToolCall(call_id=call_id, name="invoke_published_enterprise_asset",
                    arguments=arguments, raw_arguments=json.dumps(arguments, ensure_ascii=False))


def test_approved_asset_team_runs_distinct_steps_in_order_and_marks_prior_output_untrusted(
    asset_runtime, monkeypatch,
):
    engine, _factory, _publication, executor = asset_runtime
    seen = []

    async def fake_invoke(call, _state, arguments):
        seen.append((call.call_id, dict(arguments)))
        step = len(seen)
        return runtime.ToolExecutionResult.success({
            "publication_id": arguments["publication_id"], "title": f"资产 {step}",
            "output": f"结果 {step}",
        })

    monkeypatch.setattr(executor, "_invoke_published_asset", fake_invoke)
    arguments = {
        "objective": "形成一份审慎结论",
        "steps": [
            {"publication_id": 101, "task": "先整理证据"},
            {"publication_id": 202, "task": "复核前一步并找反例"},
        ],
    }
    result = asyncio.run(executor.execute(ToolCall(
        call_id="approved-team-call", name="run_approved_enterprise_asset_team",
        arguments=arguments, raw_arguments=runtime._json(arguments),
    )))

    assert result.status == "success"
    assert result.output["status"] == "completed"
    assert [item[1]["publication_id"] for item in seen] == [101, 202]
    assert seen[0][0] != seen[1][0]
    assert "结果 1" in seen[1][1]["task"]
    assert "不可信引用材料" in seen[1][1]["task"]
    assert [item["output"] for item in result.output["steps"]] == ["结果 1", "结果 2"]
    schemas = asyncio.run(executor.tool_schemas())
    team_schema = next(item for item in schemas if item["name"] == "run_approved_enterprise_asset_team")
    assert team_schema["parameters"]["properties"]["steps"]["maxItems"] == 3
    engine.dispose()


def test_approved_asset_team_sends_each_step_through_enterprise_metering(asset_runtime, monkeypatch):
    engine, factory, first_publication, executor = asset_runtime
    with factory() as db:
        draft = assets.create_draft(
            db, 10, 2, kind="agent", title="独立复核",
            definition={"schema_version": 1, "runtime": "prompt", "system_prompt": "找出薄弱证据。"},
            request_key="asset-team-second-draft",
        )
        second_publication = assets.request_publication(
            db, 10, 2, draft.id, scope="enterprise", expected_version=1,
            request_key="asset-team-second-publication",
        )
        second_publication = assets.decide_enterprise_publication(
            db, 10, second_publication.id, 1, approve=True, reason="复核 Agent 测试批准",
        )

    provider_calls = []

    async def fake_stream(self, payload):
        provider_calls.append(payload)
        index = len(provider_calls)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": f"子 Agent {index} 的复核结果"},
                ]}],
                "usage": {"input_tokens": 18, "output_tokens": 9},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    arguments = {
        "objective": "形成经过复核的简短结论",
        "steps": [
            {"publication_id": first_publication.id, "task": "整理现有事实"},
            {"publication_id": second_publication.id, "task": "独立寻找反例"},
        ],
    }
    result = asyncio.run(executor.execute(ToolCall(
        call_id="metered-approved-team-call", name="run_approved_enterprise_asset_team",
        arguments=arguments, raw_arguments=runtime._json(arguments),
    )))

    assert result.status == "success" and result.output["status"] == "completed"
    assert len(provider_calls) == 2
    assert "子 Agent 1 的复核结果" in provider_calls[1]["input"][0]["content"]
    assert "不可信引用材料" in provider_calls[1]["input"][0]["content"]
    with factory() as db:
        reservations = db.query(BillingReservation).order_by(BillingReservation.id).all()
        invocations = db.query(EnterpriseAssistantAssetInvocation).order_by(
            EnterpriseAssistantAssetInvocation.id,
        ).all()
        assert len(reservations) == len(invocations) == 2
        assert all(row.enterprise_id == 10 and row.user_id == 2 for row in reservations)
        assert all(row.personal_wallet_id is None and row.status == "settled" for row in reservations)
        assert len({row.request_key for row in reservations}) == 2
        assert all(row.status == "completed" for row in invocations)
    engine.dispose()


def test_approved_asset_team_rejects_duplicate_assets_before_any_child_call(asset_runtime, monkeypatch):
    engine, _factory, _publication, executor = asset_runtime
    invoked = []

    async def fake_invoke(*_args):
        invoked.append(True)
        return runtime.ToolExecutionResult.success({"output": "unexpected"})

    monkeypatch.setattr(executor, "_invoke_published_asset", fake_invoke)
    arguments = {
        "objective": "形成结论",
        "steps": [
            {"publication_id": 101, "task": "分析"},
            {"publication_id": 101, "task": "复核"},
        ],
    }
    result = asyncio.run(executor.execute(ToolCall(
        call_id="duplicate-approved-team-call", name="run_approved_enterprise_asset_team",
        arguments=arguments, raw_arguments=runtime._json(arguments),
    )))

    assert result.status == "error"
    assert "不同的已发布" in result.error
    assert invoked == []
    engine.dispose()


def test_approved_asset_team_stops_after_a_child_failure(asset_runtime, monkeypatch):
    engine, _factory, _publication, executor = asset_runtime
    seen = []

    async def fake_invoke(call, _state, arguments):
        seen.append(arguments["publication_id"])
        if arguments["publication_id"] == 202:
            return runtime.ToolExecutionResult.failure("子资产用量未知，禁止重试", failure_kind="usage_unknown")
        return runtime.ToolExecutionResult.success({"output": "第一步结果"})

    monkeypatch.setattr(executor, "_invoke_published_asset", fake_invoke)
    arguments = {
        "objective": "验证并总结",
        "steps": [
            {"publication_id": 101, "task": "先查证"},
            {"publication_id": 202, "task": "复核"},
            {"publication_id": 303, "task": "最后总结"},
        ],
    }
    result = asyncio.run(executor.execute(ToolCall(
        call_id="failed-approved-team-call", name="run_approved_enterprise_asset_team",
        arguments=arguments, raw_arguments=runtime._json(arguments),
    )))

    assert result.status == "success"
    assert result.output["status"] == "partial"
    assert result.output["stopped_at_step"] == 2
    assert result.output["reason_code"] == "usage_unknown"
    assert result.output["retry_requires_explicit_user_request"] is True
    assert result.output["reason"] == "子资产用量未知，禁止重试"
    assert [item["output"] for item in result.output["completed_steps"]] == ["第一步结果"]
    assert seen == [101, 202]
    engine.dispose()


def test_utf8_task_limit_fails_before_creating_child_invocation_or_billing_hold(asset_runtime):
    engine, factory, publication, executor = asset_runtime
    result = asyncio.run(executor.execute(_call(publication.id, task="界" * 834)))

    assert result.status == "error"
    assert "2500 字节" in result.error
    with factory() as db:
        assert db.query(EnterpriseAssistantAssetInvocation).count() == 0
        assert db.query(BillingReservation).count() == 0
    engine.dispose()


def test_serialized_asset_prompt_and_task_budget_fails_before_billing_hold(asset_runtime):
    engine, factory, _publication, executor = asset_runtime
    with factory() as db:
        draft = assets.create_draft(
            db, 10, 2, kind="agent", title="转义边界提示词",
            definition={"schema_version": 1, "runtime": "prompt", "system_prompt": "\\" * 3900},
            request_key="asset-escaped-prompt-draft",
        )
        publication = assets.request_publication(
            db, 10, 2, draft.id, scope="enterprise", expected_version=1,
            request_key="asset-escaped-prompt-publication",
        )
        publication = assets.decide_enterprise_publication(
            db, 10, publication.id, 1, approve=True, reason="输入预算测试",
        )
        assert assets.xiaoling_runtime_config_compatible(publication) is True

    result = asyncio.run(executor.execute(_call(
        publication.id, task="整理本次测试计划", call_id="escaped-prompt-call",
    )))

    assert result.status == "error" and "8000 字节输入预算" in result.error
    with factory() as db:
        row = db.query(EnterpriseAssistantAssetInvocation).filter_by(
            tool_call_id="escaped-prompt-call",
        ).one()
        assert row.status == "failed" and row.error_code == "input_budget_exceeded"
        assert db.query(BillingReservation).count() == 0
    engine.dispose()


def test_published_asset_child_call_is_enterprise_metered_and_cached_on_replay(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    calls = []

    async def fake_stream(self, payload):
        calls.append(payload)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "已归纳测试计划。"},
                ]}],
                "usage": {"input_tokens": 16, "output_tokens": 8},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    tool_call = _call(publication.id)
    first = asyncio.run(executor.execute(tool_call))
    second = asyncio.run(executor.execute(tool_call))

    assert first.status == second.status == "success"
    assert first.output["output"] == second.output["output"] == "已归纳测试计划。"
    assert first.output["cached"] is False and second.output["cached"] is True
    assert len(calls) == 1
    assert calls[0]["tools"] == []
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert calls[0]["max_output_tokens"] == runtime._ASSET_MAX_OUTPUT_TOKENS
    assert calls[0]["instructions"].startswith("用简洁中文归纳。")
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        wallet = db.query(BillingWallet).filter_by(owner_type="enterprise", owner_id=10).one()
        assert reservation.enterprise_id == 10 and reservation.user_id == 2
        assert reservation.personal_wallet_id is None and reservation.status == "settled"
        assert reservation.input_tokens == 16 and reservation.output_tokens == 8
        assert wallet.owner_type == "enterprise"
        assert invocation.status == "completed"
        assert invocation.publication_sha256 == publication.content_sha256
        assert invocation.input_sha256 == hashlib.sha256("整理本次测试计划".encode()).hexdigest()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(
            assistant_run_id=invocation.assistant_run_id,
        ).one()
        assert state.error_code is None
    engine.dispose()


def test_completed_child_does_not_clear_pending_marker_while_sibling_is_running(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    inserted_sibling = {}
    original_finish = runtime._finish_asset_invocation

    async def fake_stream(self, payload):
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "第一个资产已完成。"},
                ]}],
                "usage": {"input_tokens": 16, "output_tokens": 8},
            },
        }

    def finish_with_running_sibling(factory_arg, **kwargs):
        with factory.begin() as db:
            current = db.get(EnterpriseAssistantAssetInvocation, kwargs["invocation_id"])
            sibling = EnterpriseAssistantAssetInvocation(
                enterprise_id=current.enterprise_id,
                conversation_id=current.conversation_id,
                assistant_run_id=current.assistant_run_id,
                initiator_user_id=current.initiator_user_id,
                tool_call_id="parallel-sibling-tool-call",
                invocation_id="parallel-sibling-invocation",
                publication_id=publication.id,
                publication_version_id=current.publication_version_id,
                publication_sha256=current.publication_sha256,
                input_sha256=hashlib.sha256("并行资产任务".encode()).hexdigest(),
                request_key_prefix="enterprise-asset:parallel-sibling-invocation",
                status="running",
                execution_token="parallel-sibling-execution-token",
                lease_expires_at=runtime._now() + timedelta(minutes=2),
            )
            db.add(sibling)
            db.flush()
            inserted_sibling["id"] = int(sibling.id)
        return original_finish(factory_arg, **kwargs)

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    monkeypatch.setattr(runtime, "_finish_asset_invocation", finish_with_running_sibling)
    result = asyncio.run(executor.execute(_call(publication.id, call_id="asset-with-running-sibling")))

    assert result.status == "success"
    with factory() as db:
        state = db.query(EnterpriseAssistantRuntimeState).one()
        current = db.query(EnterpriseAssistantAssetInvocation).filter_by(
            tool_call_id="asset-with-running-sibling",
        ).one()
        sibling = db.get(EnterpriseAssistantAssetInvocation, inserted_sibling["id"])
        assert current.status == "completed"
        assert sibling.status == "running"
        assert state.error_code == "provider_attempt_pending_reconciliation"

    runtime.EnterpriseAssistantToolExecutor._mark_asset_invocation(
        factory,
        inserted_sibling["id"],
        "failed",
        "provider_rejected_before_send",
        execution_token="parallel-sibling-execution-token",
        lease_generation=4,
    )
    with factory() as db:
        state = db.query(EnterpriseAssistantRuntimeState).one()
        sibling = db.get(EnterpriseAssistantAssetInvocation, inserted_sibling["id"])
        assert sibling.status == "failed"
        assert state.error_code is None
        assert db.query(BillingReservation).count() == 1
    engine.dispose()


def test_unknown_child_provider_result_is_fenced_and_never_reissued(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    provider_calls = []

    async def failed_stream(self, _payload):
        provider_calls.append(True)
        raise httpx.ReadTimeout("provider outcome unknown")
        yield  # pragma: no cover - keep this an async generator

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", failed_stream)
    tool_call = _call(publication.id, call_id="asset-tool-timeout")
    first = asyncio.run(executor.execute(tool_call))
    second = asyncio.run(executor.execute(tool_call))

    assert first.status == second.status == "error"
    assert len(provider_calls) == 1
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert reservation.status == "unknown"
        assert invocation.status == "unknown"
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(
            assistant_run_id=invocation.assistant_run_id,
        ).one()
        assert state.error_code == "provider_attempt_pending_reconciliation"
        assert "租约内" in (second.error or "") and "等待当前结果或对账" in (second.error or "")
    engine.dispose()


def test_live_child_invocation_lease_rejects_parallel_replay(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    task = "整理本次测试计划"
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id="ear_asset_invocation").one()
        db.add(EnterpriseAssistantAssetInvocation(
            enterprise_id=10, conversation_id=20, assistant_run_id=run.id,
            initiator_user_id=2, tool_call_id="asset-tool-busy", invocation_id="busy-child",
            publication_id=publication.id, publication_version_id=publication.version_id,
            publication_sha256=publication.content_sha256,
            input_sha256=hashlib.sha256(task.encode()).hexdigest(),
            request_key_prefix="enterprise-asset:busy-child", status="running",
            execution_token="current-execution",
            lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
        ))
    provider_calls = []

    async def provider_must_not_start(self, _payload):
        provider_calls.append(True)
        raise AssertionError("活跃幂等租约不能启动第二次模型调用")
        yield  # pragma: no cover - keep this an async generator

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", provider_must_not_start)
    result = asyncio.run(executor.execute(_call(publication.id, task=task, call_id="asset-tool-busy")))

    assert result.status == "error" and "租约内" in (result.error or "")
    assert provider_calls == []
    with factory() as db:
        assert db.query(BillingReservation).count() == 0
    engine.dispose()


def test_expired_child_lease_without_reservation_can_be_reclaimed(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    task = "整理本次测试计划"
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id="ear_asset_invocation").one()
        db.add(EnterpriseAssistantAssetInvocation(
            enterprise_id=10, conversation_id=20, assistant_run_id=run.id,
            initiator_user_id=2, tool_call_id="asset-expired-no-reservation", invocation_id="expired-free",
            publication_id=publication.id, publication_version_id=publication.version_id,
            publication_sha256=publication.content_sha256,
            input_sha256=hashlib.sha256(task.encode()).hexdigest(),
            request_key_prefix="enterprise-asset:expired-free", status="running",
            execution_token="expired-execution",
            lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
        ))
    provider_calls = []

    async def fake_stream(self, payload):
        provider_calls.append(payload)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "安全重新认领完成"},
                ]}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    result = asyncio.run(executor.execute(_call(publication.id, task=task, call_id="asset-expired-no-reservation")))

    assert result.status == "success" and len(provider_calls) == 1
    with factory() as db:
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert invocation.status == "completed"
        assert invocation.execution_token != "expired-execution"
        assert db.query(BillingReservation).one().request_key == (
            "enterprise-asset:expired-free:generation:4:1"
        )
    engine.dispose()


def test_expired_child_lease_with_reservation_enters_reconciliation(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    task = "整理本次测试计划"
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id="ear_asset_invocation").one()
        invocation = EnterpriseAssistantAssetInvocation(
            enterprise_id=10, conversation_id=20, assistant_run_id=run.id,
            initiator_user_id=2, tool_call_id="asset-expired-with-reservation", invocation_id="expired-held",
            publication_id=publication.id, publication_version_id=publication.version_id,
            publication_sha256=publication.content_sha256,
            input_sha256=hashlib.sha256(task.encode()).hexdigest(),
            request_key_prefix="enterprise-asset:expired-held", status="running",
            execution_token="expired-execution",
            lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
        )
        db.add(invocation)
        db.add(BillingReservation(
            request_key="enterprise-asset:expired-held:1", model_name="deepseek-v4-flash",
            enterprise_id=10, user_id=2, requested_milli=100, actual_milli=20,
            input_tokens=10, output_tokens=4, usage_state="reported", status="settled",
        ))
    provider_calls = []

    async def provider_must_not_restart(self, _payload):
        provider_calls.append(True)
        raise AssertionError("存在账单预占时不能重发过期子调用")
        yield  # pragma: no cover - keep this an async generator

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", provider_must_not_restart)
    result = asyncio.run(executor.execute(
        _call(publication.id, task=task, call_id="asset-expired-with-reservation"),
    ))

    assert result.status == "error" and "先对账" in (result.error or "")
    assert provider_calls == []
    with factory() as db:
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert invocation.status == "unknown"
        assert invocation.error_code == "asset_result_not_cached"
        assert db.query(BillingReservation).count() == 1
    engine.dispose()


def test_dual_approved_not_sent_child_recovery_uses_new_parent_generation_key(asset_runtime, monkeypatch):
    engine, factory, publication, old_executor = asset_runtime

    async def failed_stream(self, _payload):
        raise httpx.ReadTimeout("provider outcome unknown")
        yield  # pragma: no cover - keep this an async generator

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", failed_stream)
    tool_call = _call(publication.id, call_id="asset-c4-recovery")
    first = asyncio.run(old_executor.execute(tool_call))
    assert first.status == "error"

    with factory.begin() as db:
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        reservation = db.query(BillingReservation).one()
        invocation.lease_expires_at = runtime._now() - timedelta(minutes=1)
        proposal = reconciliation.propose_reconciliation(
            db,
            reservation_id=reservation.id,
            actor_id=101,
            resolution="release_not_sent",
            evidence_reference="provider-child-c4-case",
            evidence_sha256="d" * 64,
            note="供应商原始记录证明子调用未发出",
            provider_request_id=None,
            input_tokens=None,
            output_tokens=None,
        )
        released, approval = reconciliation.decide_reconciliation(
            db,
            reservation_id=reservation.id,
            actor_id=102,
            decision="approve",
            note="第二管理员复核未发送证据",
        )
        assert proposal.decision == "proposed"
        assert approval.decision == "approved"
        assert released.status == "released"
        invocation_id = invocation.id

    # The parent retry claim has already fenced generation 4 and created generation 5.
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id="ear_asset_invocation").one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(
            assistant_run_id=run.id,
        ).one()
        state.lease_generation = 5
        state.billing_invocation_count = 2
        state.status = "running"
        state.error_code = None
        state.lease_expires_at = runtime._now() + timedelta(minutes=5)

    provider_calls = []

    async def successful_stream(self, payload):
        provider_calls.append(payload)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "双人审核后安全恢复"},
                ]}],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", successful_stream)
    with factory() as db:
        new_executor = runtime.EnterpriseAssistantToolExecutor(
            db,
            enterprise_id=10,
            conversation_id=20,
            run_public_id="ear_asset_invocation",
            lease_generation=5,
            actor_user_id=2,
        )
        result = asyncio.run(new_executor.execute(tool_call))
    assert result.status == "success"
    assert len(provider_calls) == 1
    with factory() as db:
        invocation = db.get(EnterpriseAssistantAssetInvocation, invocation_id)
        reservations = db.query(BillingReservation).order_by(BillingReservation.id.asc()).all()
        assert invocation.status == "completed"
        assert invocation.output_text == "双人审核后安全恢复"
        assert len(reservations) == 2
        assert reservations[0].status == "released"
        assert reservations[1].status == "settled"
        assert reservations[1].request_key == (
            "enterprise-asset:" + invocation.invocation_id + ":generation:5:1"
        )
        assert invocation.request_key_prefix == "enterprise-asset:" + invocation.invocation_id
    engine.dispose()


def test_skill_transform_runs_without_tools_and_uses_enterprise_metering(asset_runtime, monkeypatch):
    engine, factory, _agent_publication, executor = asset_runtime
    with factory() as db:
        draft = assets.create_draft(
            db, 10, 2, kind="skill", title="格式转换",
            definition={
                "schema_version": 1, "skill_type": "llm_transform",
                "prompt": "将输入整理成简洁的项目清单。",
            },
            request_key="asset-invocation-skill-draft",
        )
        publication = assets.request_publication(
            db, 10, 2, draft.id, scope="enterprise", expected_version=1,
            request_key="asset-invocation-skill-publication",
        )
        publication = assets.decide_enterprise_publication(
            db, 10, publication.id, 1, approve=True, reason="运行测试批准",
        )

    requests = []

    async def fake_stream(self, payload):
        requests.append(payload)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "- 完成技能转换"},
                ]}],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    result = asyncio.run(executor.execute(_call(publication.id, call_id="asset-skill-transform")))

    assert result.status == "success"
    assert result.output["kind"] == "skill"
    assert requests[0]["instructions"].startswith("将输入整理成简洁的项目清单。")
    assert requests[0]["tools"] == []
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert reservation.enterprise_id == 10 and reservation.user_id == 2
        assert reservation.status == "settled"
        assert invocation.status == "completed"
    engine.dispose()


def test_permission_change_after_provider_response_suppresses_cached_output(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    provider_finished = False
    post_provider_commits = 0
    timeline = []
    original_live_snapshot = runtime._live_role_snapshot

    def live_snapshot(_db, _state):
        timeline.append(("role_snapshot", provider_finished))
        saved_snapshot = original_live_snapshot(_db, _state)
        if provider_finished:
            return {**saved_snapshot, "messages": [{"message_id": 999, "rank": 0}]}
        return saved_snapshot

    monkeypatch.setattr(runtime, "_live_role_snapshot", live_snapshot)
    original_commit = executor._db.commit

    def track_commit():
        nonlocal post_provider_commits
        timeline.append(("commit", provider_finished))
        if provider_finished:
            post_provider_commits += 1
        original_commit()

    monkeypatch.setattr(executor._db, "commit", track_commit)
    original_current = executor._current

    def require_fresh_permission_snapshot(*args, **kwargs):
        timeline.append(("current", provider_finished, post_provider_commits))
        if provider_finished:
            assert post_provider_commits >= 1
        return original_current(*args, **kwargs)

    monkeypatch.setattr(executor, "_current", require_fresh_permission_snapshot)

    async def fake_stream(self, payload):
        nonlocal provider_finished
        timeline.append(("provider_started",))
        provider_finished = True
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "不可在撤权后展示"},
                ]}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    result = asyncio.run(executor.execute(_call(publication.id, call_id="asset-revoked-in-flight")))

    assert provider_finished, timeline
    assert result.status == "error" and "已抑制" in (result.error or "")
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert reservation.status == "settled"
        assert invocation.status == "suppressed"
        assert invocation.output_text is None
    engine.dispose()


def test_publication_revoked_before_billing_authorization_never_calls_provider(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    provider_calls = []
    original_model_config = runtime.assistant_service._model_config

    def revoke_before_billing_guard(db):
        assets.revoke_enterprise_publication(
            db, 10, publication.id, 1, reason="并发撤销验收",
        )
        return original_model_config(db)

    async def fake_stream(self, payload):
        provider_calls.append(payload)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "撤销后不应发起"},
                ]}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        }

    monkeypatch.setattr(runtime.assistant_service, "_model_config", revoke_before_billing_guard)
    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)

    result = asyncio.run(executor.execute(_call(publication.id, call_id="asset-revoked-before-send")))

    assert result.status == "error" and result.failure_kind == "authorization"
    assert provider_calls == []
    with factory() as db:
        reservation = db.query(BillingReservation).one_or_none()
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        current_publication = db.query(EnterpriseAssetPublication).filter_by(id=publication.id).one()
        assert reservation is None
        assert current_publication.status == "revoked"
        assert invocation.status == "suppressed"
        assert invocation.output_text is None
    engine.dispose()


def test_publication_revoked_during_provider_attempt_suppresses_paid_output(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime
    provider_finished = False

    async def fake_stream(self, payload):
        nonlocal provider_finished
        provider_finished = True
        assets.revoke_enterprise_publication(
            executor._db, 10, publication.id, 1, reason="执行中撤销验收",
        )
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "撤销后的模型输出"},
                ]}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        }

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fake_stream)
    result = asyncio.run(executor.execute(_call(publication.id, call_id="asset-revoked-in-flight")))

    assert provider_finished
    assert result.status == "error" and "已抑制" in (result.error or "")
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert reservation.status == "settled"
        assert invocation.status == "suppressed"
        assert invocation.output_text is None
    engine.dispose()


def test_current_generation_reservation_prevents_unbilled_suppression(asset_runtime, monkeypatch):
    engine, factory, publication, executor = asset_runtime

    async def fail_after_reservation(self, _payload):
        raise RuntimeError("模拟预占后的传输异常")
        yield  # 使其保持异步生成器；异常模拟预占后结果未知。

    monkeypatch.setattr(runtime.NativeResponsesTransport, "_stream_without_billing", fail_after_reservation)
    result = asyncio.run(executor.execute(_call(publication.id, call_id="asset-suppression-has-hold")))

    assert result.status == "error" and result.failure_kind == "usage_unknown"
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert reservation.status == "unknown"
        assert invocation.status == "unknown"
        original_error_code = invocation.error_code

    executor._suppress_if_current_generation_unreserved(
        factory,
        invocation_id=invocation.id,
        request_key_prefix=invocation.request_key_prefix,
        execution_token=invocation.execution_token,
        lease_generation=4,
    )

    with factory() as db:
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert invocation.status == "unknown"
        assert invocation.error_code == original_error_code
    engine.dispose()


def test_stale_asset_worker_cannot_suppress_reclaimed_invocation(asset_runtime):
    engine, factory, publication, _executor = asset_runtime
    with factory.begin() as db:
        run = db.query(EnterpriseAssistantRun).one()
        state = db.query(EnterpriseAssistantRuntimeState).one()
        state.lease_generation = 5
        invocation = EnterpriseAssistantAssetInvocation(
            enterprise_id=10,
            conversation_id=20,
            assistant_run_id=run.id,
            initiator_user_id=2,
            tool_call_id="asset-call-reclaimed",
            invocation_id="asset-invocation-reclaimed",
            publication_id=publication.id,
            publication_version_id=publication.version_id,
            publication_sha256=publication.content_sha256,
            input_sha256=hashlib.sha256(b"same input").hexdigest(),
            request_key_prefix="enterprise-asset:reclaimed:generation:5",
            status="running",
            execution_token="current-worker-token",
            lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
        )
        db.add(invocation)
        db.flush()
        invocation_id = int(invocation.id)

    runtime.EnterpriseAssistantToolExecutor._suppress_if_current_generation_unreserved(
        factory,
        invocation_id=invocation_id,
        request_key_prefix="enterprise-asset:reclaimed",
        execution_token="expired-worker-token",
        lease_generation=4,
    )

    with factory() as db:
        invocation = db.query(EnterpriseAssistantAssetInvocation).one()
        assert invocation.status == "running"
        assert invocation.execution_token == "current-worker-token"
    engine.dispose()
