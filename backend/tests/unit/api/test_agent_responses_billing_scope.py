import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.agent_responses import (
    _freeze_interrupted_admin_attempts,
    _response_action_billing_scope,
    _trusted_response_billing_scope,
)
from app.core.config import settings
from app.core.database import Base
from app.models.agent_response_run import AgentResponseRun
from app.models.billing_ledger import BillingWallet
from app.models.platform_model_cost_attempt import PlatformModelCostAttempt
from app.models.user import User
from app.services import billing_runtime_gate
from app.services.billing_ledger_service import BillingError


@pytest.fixture
def wallet_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'response-billing.sqlite'}")
    Base.metadata.create_all(engine, tables=[BillingWallet.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add(BillingWallet(owner_type="user", owner_id=27))
    yield factory
    engine.dispose()


@pytest.fixture
def platform_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'platform-response-billing.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        actor = User(username="response-platform-admin", password="x", role="admin", status=1)
        db.add(actor)
        db.flush()
        actor_id = int(actor.id)
    yield factory, actor_id
    engine.dispose()


def test_unready_metering_keeps_existing_agent_path_unscoped(monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", False)
    with _trusted_response_billing_scope(None, SimpleNamespace(id=27), "user"):
        assert billing_runtime_gate._scope.get() is None


def test_ready_user_scope_uses_only_authenticated_user_wallet(wallet_sessions, monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 8192)
    monkeypatch.setattr(settings, "deepseek_max_output_tokens", 2048)

    with _response_action_billing_scope(wallet_sessions, SimpleNamespace(id=27), "user", "reject"):
        scope = billing_runtime_gate._scope.get()
        assert scope["user_id"] == 27
        assert scope["personal_wallet_id"] == 1
        assert scope["enterprise_wallet_id"] is None
        assert scope["enterprise_id"] is None
        assert scope["group_id"] is None
        assert scope["allow_personal"] is False
        assert scope["max_input_tokens"] == 8192
        assert scope["max_output_tokens"] == 2048
    assert billing_runtime_gate._scope.get() is None


def test_cancel_does_not_require_a_wallet_or_billable_model_scope(monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    with _response_action_billing_scope(None, SimpleNamespace(id=27), "admin", "cancel"):
        assert billing_runtime_gate._scope.get() is None


def test_admin_surface_uses_platform_cost_not_user_or_enterprise_wallet(platform_sessions, monkeypatch):
    from app.models.billing_ledger import BillingReservation

    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(settings, "billing_allow_unmetered_external_calls", False)
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 8192)
    monkeypatch.setattr(settings, "deepseek_max_output_tokens", 2048)
    factory, actor_id = platform_sessions
    with _trusted_response_billing_scope(factory, SimpleNamespace(id=actor_id), "admin", run_id="admin-run"):
        scope = billing_runtime_gate._scope.get()
        assert scope["platform_cost"] is True
        assert scope["platform_actor_user_id"] == actor_id
        assert "personal_wallet_id" not in scope
        assert "enterprise_wallet_id" not in scope
        ticket = billing_runtime_gate.begin_attempt(
            model_name="deepseek-v4-pro",
            payload={"model": "deepseek-v4-pro", "input": "管理端查询", "max_output_tokens": 16},
            provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
        )
        billing_runtime_gate.before_stream_send(ticket)
        billing_runtime_gate.finish_attempt(ticket, {"input_tokens": 5, "output_tokens": 2})
    with factory() as db:
        row = db.query(PlatformModelCostAttempt).one()
        assert row.actor_user_id == actor_id and row.status == "settled"
        assert db.query(BillingReservation).count() == 0
        assert db.query(BillingWallet).count() == 0


def test_manual_admin_retry_freezes_interrupted_attempt_before_new_call(platform_sessions, monkeypatch):
    factory, actor_id = platform_sessions
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 8192)
    monkeypatch.setattr(settings, "deepseek_max_output_tokens", 2048)
    with factory() as db:
        actor = db.get(User, actor_id)
        db.add(AgentResponseRun(
            run_id="manual-retry-run",
            user_id=actor_id,
            surface="admin",
            session_key="admin-session",
            status="failed",
            checkpoint_json=json.dumps({"status": "failed", "recovery_requested": False}),
            version=1,
        ))
        db.commit()
    with _trusted_response_billing_scope(factory, actor, "admin", run_id="manual-retry-run"):
        ticket = billing_runtime_gate.begin_attempt(
            model_name="deepseek-v4-pro",
            payload={"model": "deepseek-v4-pro", "input": "原请求发送后中断", "max_output_tokens": 16},
            provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
        )
        billing_runtime_gate.before_stream_send(ticket)

    assert _freeze_interrupted_admin_attempts(
        factory, user_id=actor_id, session_key="admin-session", run_id="manual-retry-run",
    ) == 1
    with factory() as db:
        row = db.query(PlatformModelCostAttempt).one()
        assert row.status == "unknown"
        with _trusted_response_billing_scope(factory, db.get(User, actor_id), "admin", run_id="manual-retry-run"):
            with pytest.raises(BillingError, match="结果未知"):
                billing_runtime_gate.begin_attempt(
                    model_name="deepseek-v4-pro",
                    payload={"model": "deepseek-v4-pro", "input": "禁止重复发送", "max_output_tokens": 16},
                    provider_name="deepseek", base_url="https://api.deepseek.com/v1", credential_source="system",
                )
        assert db.query(BillingWallet).count() == 0


@pytest.mark.parametrize("action", ["start", "approve", "reject", "answer", "retry"])
def test_every_admin_model_action_enters_platform_cost_scope(platform_sessions, monkeypatch, action):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 8192)
    monkeypatch.setattr(settings, "deepseek_max_output_tokens", 2048)
    factory, actor_id = platform_sessions
    with factory() as db:
        actor = db.get(User, actor_id)
    with _response_action_billing_scope(
        factory, actor, "admin", action, run_id=f"admin-action-{action}",
    ):
        assert billing_runtime_gate._scope.get()["platform_cost"] is True


def test_ready_user_without_paid_wallet_fails_before_model_scope(wallet_sessions, monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    with pytest.raises(BillingError, match="个人积分账户"):
        with _trusted_response_billing_scope(wallet_sessions, SimpleNamespace(id=28), "user"):
            pytest.fail("缺少钱包时不得进入模型调用")


def test_ready_metering_rejects_fake_session_without_unmetered_fallback(monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    with pytest.raises(BillingError, match="账号或数据库会话"):
        with _trusted_response_billing_scope(None, SimpleNamespace(id=27), "user"):
            pytest.fail("缺少真实数据库时不得进入模型调用")
