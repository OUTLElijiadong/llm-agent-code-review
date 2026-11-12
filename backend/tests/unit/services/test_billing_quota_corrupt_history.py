"""Historical quota reconstruction must reject unverifiable ledger facts."""

import pytest

from app.core.exceptions import ConflictError
from app.models.billing_ledger import BillingLimit, BillingReservationPart, BillingWallet
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_quota_service as quota

PROVIDER = ledger.BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")


def _historical_reservation(db):
    owner = User(username="quota-corruption-owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    company = Enterprise(name="额度历史坏数据测试", owner_id=owner.id, seat_limit=2, status="active",
                         paid_order_id="quota-corruption-enterprise")
    db.add(company)
    db.flush()
    db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
    wallet = ledger.get_or_create_wallet(db, "enterprise", company.id)
    ledger.grant(db, wallet_id=wallet.id, grant_key="quota-corruption-grant", kind="purchase", amount_milli=100_000)
    res = ledger.reserve(db, request_key="quota-corruption-reservation", model_name="deepseek-flash",
                         user_id=owner.id, enterprise_id=company.id, enterprise_wallet_id=wallet.id,
                         max_milli=40_000, provider_identity=PROVIDER)
    db.commit()
    return owner, company, wallet, res


@pytest.mark.parametrize("corruption", ["missing_batch", "foreign_batch", "fake_release", "released_with_charge"])
@pytest.mark.parametrize("action", ["set", "legacy_list"])
def test_quota_history_rejects_unverifiable_batch_or_release(db, corruption, action):
    owner, company, _wallet, res = _historical_reservation(db)
    part = db.query(BillingReservationPart).filter_by(reservation_id=res.id).one()
    if corruption == "missing_batch":
        part.batch_id = 999_999_999
    elif corruption == "foreign_batch":
        foreign_wallet = ledger.get_or_create_wallet(db, "enterprise", company.id + 10_000)
        foreign_batch = ledger.grant(db, wallet_id=foreign_wallet.id, grant_key="quota-foreign-batch",
                                     kind="purchase", amount_milli=100_000)
        part.batch_id = foreign_batch.id
    elif corruption == "fake_release":
        res.status = "released"  # no terminal evidence or real release
    else:
        ledger.release_not_sent(db, res.request_key)
        part.charged_milli = 1
    if action == "legacy_list":
        period = quota.current_period()
        start, end = quota._period_bounds(period)
        db.add(BillingLimit(limit_key=quota._limit_key(company.id, period, None, None),
                            enterprise_id=company.id, category="normal", period_start=start, period_end=end,
                            limit_milli=100_000, used_milli=0, reserved_milli=0, accounting_version=0))
    db.commit()
    with pytest.raises(ConflictError, match="无法核验"):
        if action == "set":
            quota.set_limit(db, company.id, owner.id, period=quota.current_period(), scope="enterprise",
                            limit_credits=100)
        else:
            quota.list_limits(db, company.id, owner.id, quota.current_period())


def test_legacy_quota_missing_wallet_does_not_hide_existing_reservation(db):
    owner, company, wallet, _res = _historical_reservation(db)
    period = quota.current_period()
    start, end = quota._period_bounds(period)
    db.add(BillingLimit(limit_key=quota._limit_key(company.id, period, None, None),
                        enterprise_id=company.id, category="normal", period_start=start, period_end=end,
                        limit_milli=100_000, used_milli=0, reserved_milli=0, accounting_version=0))
    db.query(BillingWallet).filter_by(id=wallet.id).delete()
    db.commit()
    with pytest.raises(ConflictError, match="钱包"):
        quota.list_limits(db, company.id, owner.id, period)


def test_empty_legacy_quota_without_wallet_can_be_displayed(db):
    owner = User(username="empty-quota-owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    company = Enterprise(name="无消费企业", owner_id=owner.id, seat_limit=1, status="active",
                         paid_order_id="empty-quota-company")
    db.add(company)
    db.flush()
    db.add(EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"))
    period = quota.current_period()
    start, end = quota._period_bounds(period)
    db.add(BillingLimit(limit_key=quota._limit_key(company.id, period, None, None),
                        enterprise_id=company.id, category="normal", period_start=start, period_end=end,
                        limit_milli=100_000, used_milli=0, reserved_milli=0, accounting_version=0))
    db.commit()
    result = quota.list_limits(db, company.id, owner.id, period)
    assert (result[0]["used_milli"], result[0]["reserved_milli"], result[0]["remaining_milli"]) == (0, 0, 100_000)
