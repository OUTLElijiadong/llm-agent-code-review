from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.agent_responses import _response_action_billing_scope, _trusted_response_billing_scope
from app.core.config import settings
from app.core.database import Base
from app.models.billing_ledger import BillingWallet
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


def test_ready_admin_surface_is_rejected_without_platform_cost_center(wallet_sessions, monkeypatch):
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    with pytest.raises(BillingError, match="可信计费归属"):
        with _trusted_response_billing_scope(wallet_sessions, SimpleNamespace(id=27), "admin"):
            pytest.fail("管理员入口不得沿用个人钱包")


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
