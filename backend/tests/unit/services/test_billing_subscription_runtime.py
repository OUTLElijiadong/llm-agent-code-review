"""订阅周期与永久审查员额度调度的幂等验证。"""

from datetime import datetime

from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.billing_ledger import BillingCreditBatch, BillingPermanentReviewer, BillingWallet
from app.models.user import User
from app.services.billing_ledger_service import available_milli
from app.services.billing_subscription_runtime import run_billing_cycle_once

NOW = datetime(2026, 10, 1, 0, 0)


def test_permanent_allowance_scheduler_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", False)
    user = User(username="monthly-reviewer", password="x", role="reviewer", status=1)
    db.add(user)
    db.flush()
    db.add(BillingPermanentReviewer(user_id=user.id, status="active", granted_by=0, granted_at=NOW))
    db.commit()

    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    first = run_billing_cycle_once(session_factory=factory, now=NOW)
    second = run_billing_cycle_once(session_factory=factory, now=NOW)

    assert first["permanent_allowances_processed"] == 1
    assert second["permanent_allowances_processed"] == 1
    wallet = db.query(BillingWallet).filter_by(owner_type="user", owner_id=user.id).one()
    batches = db.query(BillingCreditBatch).filter_by(wallet_id=wallet.id).all()
    assert len(batches) == 1
    assert batches[0].issued_milli == 5_000_000
    assert available_milli(db, wallet.id, NOW) == 5_000_000
