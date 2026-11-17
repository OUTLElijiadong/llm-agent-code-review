"""管理员小菱平台成本作用域：单独计量、权限重验和未知结果冻结。"""

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.platform_model_cost_attempt import PlatformModelCostAttempt
from app.models.user import User
from app.services import billing_runtime_gate
from app.services.agent_response_billing_service import authorize_platform_admin
from app.services.agent_responses_service import NativeResponsesTransport
from app.services.billing_ledger_service import BillingError
from app.services.billing_runtime_gate import (
    before_stream_send,
    finish_attempt,
    platform_cost_scope,
    unknown_attempt,
)
from app.utils.api_resolver import ApiConfig


def _platform_scope(db, user, *, run_id="admin-run-platform-cost"):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    def guard(scope_db, _key):
        authorize_platform_admin(scope_db, int(user.id))

    return factory, platform_cost_scope(
        session_factory=factory,
        actor_user_id=int(user.id),
        run_id=run_id,
        max_input_tokens=20_000,
        max_output_tokens=1_000,
        authorization_guard=guard,
        request_key_prefix=f"platform-test:{user.id}:{run_id}",
    )


def test_platform_admin_responses_reserves_and_settles_separate_provider_cost(db, monkeypatch):
    user = User(username="platform-cost-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    factory, scope = _platform_scope(db, user)
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", False)

    with scope:
        ticket = billing_runtime_gate.begin_attempt(
            model_name="deepseek-v4-pro",
            payload={"model": "deepseek-v4-pro", "input": "管理员查询状态", "max_output_tokens": 50},
            provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
        )
        assert ticket["platform_cost"] is True
        before_stream_send(ticket)
        finish_attempt(ticket, {"input_tokens": 10, "output_tokens": 3})

    row = db.query(PlatformModelCostAttempt).one()
    assert row.status == "settled"
    assert row.actor_user_id == user.id and row.run_id == "admin-run-platform-cost"
    assert row.model_name == "deepseek-v4-pro"
    assert row.pricing_version == "deepseek-cny-peak-cache-miss-2026-11-17-v1"
    assert row.pricing_source_url == "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
    assert row.reserved_cost_cny_micros >= 171
    assert row.actual_cost_cny_micros == 171  # 10 × ¥9/M + 3 × ¥27/M
    assert row.actual_input_tokens == 10 and row.actual_output_tokens == 3
    assert db.query(BillingReservation).count() == 0
    assert db.query(BillingWallet).count() == 0


@pytest.mark.asyncio
async def test_native_admin_responses_transport_calls_pro_and_settles_platform_cost(db, monkeypatch):
    user = User(username="native-platform-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="native-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", False)
    transport = NativeResponsesTransport(ApiConfig(
        api_key="platform-managed-key", base_url="https://api.deepseek.com/v1", model="deepseek-v4-pro",
        provider="deepseek", source="system",
    ))
    sent = []

    async def completed(_payload):
        sent.append(True)
        yield {"type": "response.completed", "response": {
            "model": "deepseek-v4-pro", "usage": {"input_tokens": 11, "output_tokens": 4},
        }}

    monkeypatch.setattr(transport, "_stream_without_billing", completed)
    with scope:
        events = [event async for event in transport._stream({
            "model": "deepseek-v4-pro", "input": "管理员端小菱", "max_output_tokens": 64,
        })]
    row = db.query(PlatformModelCostAttempt).one()
    assert sent == [True] and events[-1]["type"] == "response.completed"
    assert row.status == "settled" and row.actual_cost_cny_micros == 207
    assert db.query(BillingReservation).count() == 0
    assert db.query(BillingWallet).count() == 0


@pytest.mark.asyncio
async def test_admin_responses_api_binds_real_start_to_platform_scope(db, monkeypatch):
    from app.api.v1 import agent_responses as api_module
    from app.services.deepseek_responses_runtime import RuntimeResult

    user = User(username="admin-api-scope", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", False)

    class ScopedService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self, _messages, *, run_id, event_sink, on_created, **_kwargs):
            assert billing_runtime_gate._scope.get()["platform_cost"] is True
            await on_created()
            ticket = billing_runtime_gate.begin_attempt(
                model_name="deepseek-v4-pro",
                payload={"model": "deepseek-v4-pro", "input": "管理员入口调用", "max_output_tokens": 16},
                provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
            )
            billing_runtime_gate.before_stream_send(ticket)
            billing_runtime_gate.finish_attempt(ticket, {"input_tokens": 8, "output_tokens": 2})
            return RuntimeResult(run_id=run_id, status="completed", output_text="已处理", rounds=1)

    monkeypatch.setattr(api_module, "AgentResponsesService", ScopedService)
    response = await api_module.stream_agent_response(
        api_module.AgentResponsesRequest(
            surface="admin", session_id="admin-api-scope-session",
            messages=[{"role": "user", "content": "检查当前状态"}],
        ),
        db=db,
        user=user,
    )
    body = "".join([
        chunk.decode() if isinstance(chunk, bytes) else chunk
        async for chunk in response.body_iterator
    ])

    row = db.query(PlatformModelCostAttempt).one()
    assert "event: response.completed" in body
    assert row.run_id and row.actor_user_id == user.id and row.status == "settled"
    assert row.actual_cost_cny_micros == 126  # 8 × ¥9/M + 2 × ¥27/M
    assert db.query(BillingReservation).count() == 0
    assert db.query(BillingWallet).count() == 0


@pytest.mark.asyncio
async def test_native_byok_transport_error_does_not_claim_platform_cost_is_pending(db, monkeypatch):
    import httpx

    user = User(username="native-platform-byok", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="native-byok-run")
    transport = NativeResponsesTransport(ApiConfig(
        api_key="caller-key", base_url="https://provider.example/v1", model="custom-model",
        provider="custom", source="user",
    ))

    async def disconnected(_payload):
        raise httpx.ConnectError(
            "connection lost", request=httpx.Request("POST", "https://provider.example/v1/responses"),
        )
        yield {}

    monkeypatch.setattr(transport, "_stream_without_billing", disconnected)
    with scope, pytest.raises(BillingError, match="用户自有 Key 调用结果未知") as exc:
        _ = [event async for event in transport._stream({
            "model": "custom-model", "input": "检查设置", "max_output_tokens": 16,
        })]

    assert "预占待对账" not in str(exc.value)
    assert db.query(PlatformModelCostAttempt).count() == 0
    assert db.query(BillingReservation).count() == 0
    assert db.query(BillingWallet).count() == 0


def test_platform_buffered_request_uses_same_metering_path(db, monkeypatch):
    user = User(username="platform-buffered-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="buffered-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", False)
    import httpx

    with scope:
        response, duration = billing_runtime_gate.metered_response_request(
            model_name="deepseek-v4-pro",
            payload={"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "查看状态"}],
                     "max_tokens": 20},
            provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
            send=lambda: (httpx.Response(200, json={"usage": {"prompt_tokens": 7, "completion_tokens": 2}}), 5),
        )
    row = db.query(PlatformModelCostAttempt).one()
    assert (response.status_code, duration) == (200, 5)
    assert row.status == "settled" and row.actual_cost_cny_micros == 117
    assert db.query(BillingReservation).count() == 0
    assert db.query(BillingWallet).count() == 0


def test_unknown_platform_attempt_blocks_same_run_retry(db, monkeypatch):
    user = User(username="platform-unknown-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="unknown-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    kwargs = {
        "model_name": "deepseek-v4-pro",
        "payload": {"model": "deepseek-v4-pro", "input": "查看", "max_output_tokens": 20},
        "provider_name": "deepseek", "base_url": "https://api.deepseek.com/v1", "credential_source": "system",
    }
    with scope:
        ticket = billing_runtime_gate.begin_attempt(**kwargs)
        before_stream_send(ticket)
        unknown_attempt(ticket)
        with pytest.raises(BillingError, match="结果未知"):
            billing_runtime_gate.begin_attempt(**kwargs)
    assert db.query(PlatformModelCostAttempt).one().status == "unknown"
    assert db.query(BillingReservation).count() == 0


def test_same_run_can_hold_parallel_model_attempts(db, monkeypatch):
    user = User(username="platform-fence-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="multi-round-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    kwargs = {
        "model_name": "deepseek-v4-pro",
        "payload": {"model": "deepseek-v4-pro", "input": "下一轮", "max_output_tokens": 20},
        "provider_name": "deepseek", "base_url": "https://api.deepseek.com/v1", "credential_source": "system",
    }
    with scope:
        first = billing_runtime_gate.begin_attempt(**kwargs)
        second = billing_runtime_gate.begin_attempt(**kwargs)
        before_stream_send(first)
        before_stream_send(second)
        finish_attempt(first, {"input_tokens": 4, "output_tokens": 2})
        finish_attempt(second, {"input_tokens": 6, "output_tokens": 3})

    rows = db.query(PlatformModelCostAttempt).order_by(PlatformModelCostAttempt.id).all()
    assert len(rows) == 2
    assert all(row.status == "settled" for row in rows)


def test_unknown_parallel_attempt_blocks_other_reserved_attempt_before_send(db, monkeypatch):
    user = User(username="platform-unknown-sibling", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="unknown-sibling-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    kwargs = {
        "model_name": "deepseek-v4-pro",
        "payload": {"model": "deepseek-v4-pro", "input": "并发保护", "max_output_tokens": 20},
        "provider_name": "deepseek", "base_url": "https://api.deepseek.com/v1", "credential_source": "system",
    }
    with scope:
        first = billing_runtime_gate.begin_attempt(**kwargs)
        second = billing_runtime_gate.begin_attempt(**kwargs)
        before_stream_send(first)
        unknown_attempt(first)
        with pytest.raises(BillingError, match="结果未知"):
            before_stream_send(second)

    rows = db.query(PlatformModelCostAttempt).order_by(PlatformModelCostAttempt.id).all()
    assert [row.status for row in rows] == ["unknown", "not_sent"]
    assert rows[0].provider_attempt_started_at is not None
    assert rows[1].provider_attempt_started_at is None


def test_revoked_admin_before_send_marks_not_sent_and_releases_run_fence(db, monkeypatch):
    user = User(username="platform-revoked-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="revoked-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    kwargs = {
        "model_name": "deepseek-v4-pro",
        "payload": {"model": "deepseek-v4-pro", "input": "权限复核", "max_output_tokens": 20},
        "provider_name": "deepseek", "base_url": "https://api.deepseek.com/v1", "credential_source": "system",
    }
    with scope:
        ticket = billing_runtime_gate.begin_attempt(**kwargs)
        user.role = "user"
        db.commit()
        with pytest.raises(BillingError, match="管理员权限已失效"):
            before_stream_send(ticket)

    row = db.query(PlatformModelCostAttempt).one()
    assert row.status == "not_sent"
    assert row.provider_attempt_started_at is None


def test_non_admin_platform_scope_is_rejected_before_provider_send(db):
    user = User(username="ordinary-platform-scope", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="forged-admin-run")
    with scope:
        with pytest.raises(BillingError, match="管理员权限已失效"):
            billing_runtime_gate.begin_attempt(
                model_name="deepseek-v4-pro",
                payload={"model": "deepseek-v4-pro", "input": "越权", "max_output_tokens": 20},
                provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
            )
    assert db.query(PlatformModelCostAttempt).count() == 0


def test_platform_scope_does_not_relabel_byok_as_platform_spend(db, monkeypatch):
    user = User(username="platform-byok-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="byok-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    calls = []

    with scope:
        result = billing_runtime_gate.metered_response_request(
            model_name="custom-model",
            payload={"model": "custom-model", "messages": [{"role": "user", "content": "BYOK"}],
                     "max_tokens": 10},
            provider_name="custom", base_url="https://provider.example/v1", credential_source="user",
            send=lambda: calls.append("sent") or "caller-paid",
        )
    assert result == "caller-paid" and calls == ["sent"]
    assert db.query(PlatformModelCostAttempt).count() == 0
    assert db.query(BillingReservation).count() == 0
    assert db.query(BillingWallet).count() == 0


def test_byok_rechecks_admin_authorization_before_sending(db, monkeypatch):
    user = User(username="platform-revoked-byok-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="revoked-byok-admin-run")
    calls = []

    with scope:
        user.role = "user"
        db.commit()
        with pytest.raises(BillingError, match="管理员权限已失效"):
            billing_runtime_gate.metered_response_request(
                model_name="custom-model",
                payload={"model": "custom-model", "messages": [{"role": "user", "content": "BYOK"}],
                         "max_tokens": 10},
                provider_name="custom", base_url="https://provider.example/v1", credential_source="user",
                send=lambda: calls.append("sent") or "caller-paid",
            )

    assert calls == []
    assert db.query(PlatformModelCostAttempt).count() == 0
    assert db.query(BillingReservation).count() == 0
    assert db.query(BillingWallet).count() == 0


def test_provider_attempt_start_is_one_shot(db, monkeypatch):
    from app.services import platform_model_cost_service

    user = User(username="platform-start-once-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id="start-once-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    kwargs = {
        "model_name": "deepseek-v4-pro",
        "payload": {"model": "deepseek-v4-pro", "input": "一次发送", "max_output_tokens": 20},
        "provider_name": "deepseek", "base_url": "https://api.deepseek.com/v1", "credential_source": "system",
    }
    with scope:
        ticket = billing_runtime_gate.begin_attempt(**kwargs)
        billing_runtime_gate.before_stream_send(ticket)
        started_at = db.query(PlatformModelCostAttempt).one().provider_attempt_started_at
        with pytest.raises(BillingError, match="预占状态无效"):
            platform_model_cost_service.mark_started(db, ticket["key"])

    row = db.query(PlatformModelCostAttempt).one()
    assert row.provider_attempt_started_at == started_at
    assert row.status == "reserved"


def test_interrupted_run_freezes_started_calls_and_releases_unsent_calls(db, monkeypatch):
    from app.services import platform_model_cost_service

    user = User(username="platform-interrupted-admin", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    factory, scope = _platform_scope(db, user, run_id="interrupted-admin-run")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    kwargs = {
        "model_name": "deepseek-v4-pro",
        "payload": {"model": "deepseek-v4-pro", "input": "恢复前核对", "max_output_tokens": 20},
        "provider_name": "deepseek", "base_url": "https://api.deepseek.com/v1", "credential_source": "system",
    }
    with scope:
        started = billing_runtime_gate.begin_attempt(**kwargs)
        billing_runtime_gate.begin_attempt(**kwargs)
        billing_runtime_gate.before_stream_send(started)
        with factory.begin() as recovery_db:
            assert platform_model_cost_service.mark_interrupted_run_attempts(
                recovery_db, "interrupted-admin-run",
            ) == 2
        with pytest.raises(BillingError, match="结果未知"):
            billing_runtime_gate.begin_attempt(**kwargs)

    rows = db.query(PlatformModelCostAttempt).order_by(PlatformModelCostAttempt.id).all()
    assert [row.status for row in rows] == ["unknown", "not_sent"]
    assert rows[0].provider_attempt_started_at is not None
    assert rows[1].provider_attempt_started_at is None


@pytest.mark.parametrize("column, value", [
    ("actual_input_tokens", -1),
    ("actual_output_tokens", -1),
    ("actual_cost_cny_micros", -1),
])
def test_database_rejects_negative_settled_usage_and_cost(db, monkeypatch, column, value):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    user = User(username=f"platform-negative-{column}", password="x", role="admin", status=1)
    db.add(user)
    db.commit()
    _factory, scope = _platform_scope(db, user, run_id=f"negative-admin-run-{column}")
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    with scope:
        ticket = billing_runtime_gate.begin_attempt(
            model_name="deepseek-v4-pro",
            payload={"model": "deepseek-v4-pro", "input": "DB 约束", "max_output_tokens": 20},
            provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
        )
        billing_runtime_gate.before_stream_send(ticket)
        billing_runtime_gate.finish_attempt(ticket, {"input_tokens": 4, "output_tokens": 2})

    with pytest.raises(IntegrityError):
        db.execute(update(PlatformModelCostAttempt).values({column: value}))
        db.flush()
    db.rollback()
