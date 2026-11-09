from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.core.exceptions import ConflictError, ForbiddenError, ValidationError
from app.models.billing_ledger import BillingCreditBatch, BillingLimit, BillingReservationLimit
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import billing_quota_service as quotas
from app.services.billing_ledger_service import (
    BillingProviderIdentity,
    settle,
)
from app.services.billing_ledger_service import (
    reserve as _reserve,
)

_TEST_PROVIDER_IDENTITY = BillingProviderIdentity(
    identity_version="deepseek-official-v1", provider_name="deepseek",
    endpoint_host="api.deepseek.com", credential_source="system",
)


def reserve(db, **kwargs):
    return _reserve(db, provider_identity=_TEST_PROVIDER_IDENTITY, **kwargs)


@pytest.fixture
def org_scope(db):
    users = {
        name: User(username=f"quota-{name}", password="x", role="user", status=1)
        for name in ("owner", "leader", "member", "second_leader", "outside")
    }
    db.add_all(users.values())
    db.flush()
    enterprise = Enterprise(name="额度测试企业", owner_id=users["owner"].id, seat_limit=8, status="active",
                            paid_order_id=f"quota-enterprise-{users['owner'].id}")
    db.add(enterprise)
    db.flush()
    db.add_all([
        EnterpriseMember(enterprise_id=enterprise.id, user_id=users["owner"].id, role="owner", status="active"),
        EnterpriseMember(enterprise_id=enterprise.id, user_id=users["leader"].id, role="member", status="active"),
        EnterpriseMember(enterprise_id=enterprise.id, user_id=users["member"].id, role="member", status="active"),
        EnterpriseMember(enterprise_id=enterprise.id, user_id=users["second_leader"].id,
                         role="member", status="active"),
    ])
    group = EnterpriseGroup(enterprise_id=enterprise.id, name="小组 A", status="active")
    db.add(group)
    db.flush()
    db.add_all([
        EnterpriseGroupMember(enterprise_id=enterprise.id, group_id=group.id, user_id=users["leader"].id,
                             role="leader", status="active"),
        EnterpriseGroupMember(enterprise_id=enterprise.id, group_id=group.id, user_id=users["member"].id,
                             role="member", status="active"),
        EnterpriseGroupMember(enterprise_id=enterprise.id, group_id=group.id, user_id=users["second_leader"].id,
                             role="leader", status="active"),
    ])
    db.commit()
    return db, enterprise, group, users


def test_admin_and_group_leader_limits_are_tenant_and_group_scoped(org_scope):
    db, enterprise, group, users = org_scope
    company = quotas.set_limit(db, enterprise.id, users["owner"].id, period="2026-12", scope="enterprise",
                               limit_credits=100)
    team = quotas.set_limit(db, enterprise.id, users["leader"].id, period="2026-12", scope="group",
                            group_id=group.id, limit_credits=80)
    person = quotas.set_limit(db, enterprise.id, users["leader"].id, period="2026-12", scope="member",
                              group_id=group.id, member_id=users["member"].id, limit_credits=50)
    db.commit()
    assert company["scope"] == "enterprise" and company["limit_milli"] == 100_000
    assert team["scope"] == "group" and team["limit_milli"] == 80_000
    assert person["scope"] == "member" and person["limit_milli"] == 50_000
    assert team["period_start"] == datetime(2026, 11, 30, 16)
    assert {row["scope"] for row in quotas.list_limits(db, enterprise.id, users["owner"].id, "2026-12")} == {
        "enterprise", "group", "member",
    }
    assert {row["scope"] for row in quotas.list_limits(
        db, enterprise.id, users["owner"].id, "2026-12", group_id=group.id,
    )} == {"group", "member"}
    scoped = quotas.list_limits(db, enterprise.id, users["leader"].id, "2026-12", group_id=group.id)
    assert {row["scope"] for row in scoped} == {"group", "member"}
    with pytest.raises(ForbiddenError):
        quotas.list_limits(db, enterprise.id, users["leader"].id, "2026-12")
    with pytest.raises(ForbiddenError, match="其他组长"):
        quotas.set_limit(db, enterprise.id, users["leader"].id, period="2026-12", scope="member",
                         group_id=group.id, member_id=users["second_leader"].id, limit_credits=20)


def test_limits_are_enforced_during_pre_authorization_and_track_actual_charge(org_scope):
    db, enterprise, group, users = org_scope
    quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(), scope="enterprise",
                     limit_credits=100)
    quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(), scope="group",
                     group_id=group.id, limit_credits=80)
    quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                     scope="member", group_id=group.id, member_id=users["member"].id, limit_credits=50)
    batch = BillingCreditBatch(wallet_id=1, grant_key="fixture", kind="purchase", issued_milli=500_000,
                               remaining_milli=500_000, reserved_milli=0)
    from app.services.billing_ledger_service import get_or_create_wallet

    wallet = get_or_create_wallet(db, "enterprise", enterprise.id)
    batch.wallet_id = wallet.id
    db.add(batch)
    db.commit()
    # The cap is 50,000 milli-credit. A 60,000 hold is rejected before any provider request.
    with pytest.raises(ValueError, match="积分或额度不足"):
        reserve(db, request_key="too-large", model_name="deepseek-v4-flash", user_id=users["member"].id,
                max_milli=60_000, enterprise_wallet_id=wallet.id, enterprise_id=enterprise.id,
                group_id=group.id, now=datetime.now())
    db.rollback()
    reservation = reserve(db, request_key="within-cap", model_name="deepseek-v4-flash", user_id=users["member"].id,
                         max_milli=40_000, enterprise_wallet_id=wallet.id, enterprise_id=enterprise.id,
                         group_id=group.id, now=datetime.now())
    settle(db, request_key=reservation.request_key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
    db.commit()
    refreshed = db.query(BillingLimit).filter(BillingLimit.user_id == users["member"].id).one()
    assert refreshed.used_milli == 30_000 and refreshed.reserved_milli == 0
    with pytest.raises(ConflictError, match="新额度低于"):
        quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(), scope="member",
                         group_id=group.id, member_id=users["member"].id, limit_credits=20)


def test_zero_limit_blocks_new_usage_and_past_month_is_immutable(org_scope):
    db, enterprise, _, users = org_scope
    row = quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                           scope="enterprise", limit_credits=0)
    assert row["remaining_milli"] == 0
    with pytest.raises(ValidationError, match="已开始月份之前"):
        quotas.set_limit(db, enterprise.id, users["owner"].id, period="2020-01", scope="enterprise",
                         limit_credits=1)
    with pytest.raises(ValidationError, match="可用日期范围"):
        quotas.set_limit(db, enterprise.id, users["owner"].id, period="9999-12", scope="enterprise",
                         limit_credits=1)


@pytest.mark.parametrize("scope", ["enterprise", "group", "member"])
def test_new_limit_counts_preexisting_settled_and_inflight_usage(org_scope, scope):
    from app.services.billing_ledger_service import get_or_create_wallet, grant, mark_unknown, release_verified_not_sent

    db, enterprise, group, users = org_scope
    wallet = get_or_create_wallet(db, "enterprise", enterprise.id)
    grant(db, wallet_id=wallet.id, grant_key="prior-usage", kind="purchase", amount_milli=200_000)
    db.commit()
    kwargs = dict(model_name="deepseek-flash", user_id=users["member"].id,
                  enterprise_wallet_id=wallet.id, enterprise_id=enterprise.id, group_id=group.id)
    done = reserve(db, request_key="prior-done", max_milli=40_000, **kwargs)
    settle(db, request_key=done.request_key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
    pending = reserve(db, request_key="prior-pending", max_milli=20_000, **kwargs)
    mark_unknown(db, pending.request_key)
    db.commit()
    targets = {"enterprise": {}, "group": {"group_id": group.id},
               "member": {"group_id": group.id, "member_id": users["member"].id}}
    with pytest.raises(ConflictError, match="已用和预占"):
        quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(), scope=scope,
                         limit_credits=49, **targets[scope])
    db.rollback()
    result = quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(), scope=scope,
                              limit_credits=50, **targets[scope])
    db.commit()
    assert (result["used_milli"], result["reserved_milli"], result["remaining_milli"]) == (30_000, 20_000, 0)
    limit = db.query(BillingLimit).filter_by(enterprise_id=enterprise.id, group_id=targets[scope].get("group_id"),
                                              user_id=targets[scope].get("member_id")).one()
    assert db.query(BillingReservationLimit).filter_by(reservation_id=pending.id, limit_id=limit.id).count() == 1
    release_verified_not_sent(db, pending.request_key)
    db.commit()
    db.refresh(limit)
    assert (limit.used_milli, limit.reserved_milli) == (30_000, 0)


def test_limit_reconciles_earlier_unlinked_reservation_on_settle_and_repeated_save(org_scope):
    from app.services.billing_ledger_service import get_or_create_wallet, grant

    db, enterprise, group, users = org_scope
    wallet = get_or_create_wallet(db, "enterprise", enterprise.id)
    grant(db, wallet_id=wallet.id, grant_key="prior-inflight", kind="purchase", amount_milli=100_000)
    db.commit()
    pending = reserve(db, request_key="prior-settle", model_name="deepseek-flash", user_id=users["member"].id,
                      max_milli=40_000, enterprise_wallet_id=wallet.id, enterprise_id=enterprise.id,
                      group_id=group.id)
    db.commit()
    args = dict(period=quotas.current_period(), scope="enterprise", limit_credits=100)
    quotas.set_limit(db, enterprise.id, users["owner"].id, **args)
    quotas.set_limit(db, enterprise.id, users["owner"].id, **args)
    db.commit()
    limit = db.query(BillingLimit).filter_by(enterprise_id=enterprise.id, group_id=None, user_id=None).one()
    assert limit.reserved_milli == 40_000
    assert db.query(BillingReservationLimit).filter_by(reservation_id=pending.id, limit_id=limit.id).count() == 1
    settle(db, request_key=pending.request_key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
    db.commit()
    db.refresh(limit)
    assert (limit.used_milli, limit.reserved_milli) == (30_000, 0)


def test_personal_fallback_with_zero_enterprise_funding_has_no_company_usage(org_scope):
    from app.services.billing_ledger_service import get_or_create_wallet, grant

    db, enterprise, group, users = org_scope
    company = get_or_create_wallet(db, "enterprise", enterprise.id)
    personal = get_or_create_wallet(db, "user", users["member"].id)
    grant(db, wallet_id=personal.id, grant_key="fallback-funds", kind="purchase", amount_milli=100_000)
    db.commit()
    quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                     scope="enterprise", limit_credits=100)
    pending = reserve(db, request_key="zero-enterprise", model_name="deepseek-flash",
                      user_id=users["member"].id, max_milli=40_000,
                      enterprise_wallet_id=company.id, personal_wallet_id=personal.id,
                      enterprise_id=enterprise.id, group_id=group.id, allow_personal=True)
    db.commit()
    limit = db.query(BillingLimit).filter_by(enterprise_id=enterprise.id, group_id=None, user_id=None).one()
    link = db.query(BillingReservationLimit).filter_by(reservation_id=pending.id, limit_id=limit.id).one()
    assert link.reserved_milli == 0
    result = quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                              scope="enterprise", limit_credits=100)
    assert (result["used_milli"], result["reserved_milli"]) == (0, 0)
    settle(db, request_key=pending.request_key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
    db.commit()
    db.refresh(limit)
    assert (limit.used_milli, limit.reserved_milli) == (0, 0)


def test_corrupt_zero_company_limit_link_rejects_limit_save(org_scope):
    from app.services.billing_ledger_service import get_or_create_wallet, grant

    db, enterprise, _, users = org_scope
    company = get_or_create_wallet(db, "enterprise", enterprise.id)
    personal = get_or_create_wallet(db, "user", users["member"].id)
    grant(db, wallet_id=personal.id, grant_key="bad-link-funds", kind="purchase", amount_milli=100_000)
    quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                     scope="enterprise", limit_credits=100)
    pending = reserve(db, request_key="bad-link", model_name="deepseek-flash", user_id=users["member"].id,
                      max_milli=40_000, enterprise_wallet_id=company.id, personal_wallet_id=personal.id,
                      enterprise_id=enterprise.id, allow_personal=True)
    link = db.query(BillingReservationLimit).filter_by(reservation_id=pending.id).one()
    link.reserved_milli = 100
    db.commit()
    with pytest.raises(ConflictError, match="额度预占明细无法核验"):
        quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                         scope="enterprise", limit_credits=100)


def test_legacy_limit_is_accurate_in_list_and_rebuilt_before_new_hold(org_scope):
    from app.services.billing_ledger_service import get_or_create_wallet, grant

    db, enterprise, group, users = org_scope
    wallet = get_or_create_wallet(db, "enterprise", enterprise.id)
    grant(db, wallet_id=wallet.id, grant_key="legacy-quota-funds", kind="purchase", amount_milli=100_000)
    db.commit()
    args = dict(model_name="deepseek-flash", user_id=users["member"].id,
                enterprise_wallet_id=wallet.id, enterprise_id=enterprise.id, group_id=group.id)
    previous = reserve(db, request_key="legacy-prior", max_milli=40_000, **args)
    start, end = quotas._period_bounds(quotas.current_period())
    legacy = BillingLimit(limit_key=quotas._limit_key(enterprise.id, quotas.current_period(), None, None),
                          enterprise_id=enterprise.id, category="normal", group_id=None, user_id=None,
                          period_start=start, period_end=end, limit_milli=50_000,
                          used_milli=0, reserved_milli=0, accounting_version=0)
    db.add(legacy)
    db.commit()
    listed = quotas.list_limits(db, enterprise.id, users["owner"].id, quotas.current_period())
    assert (listed[0]["used_milli"], listed[0]["reserved_milli"], listed[0]["remaining_milli"]) == (0, 40_000, 10_000)
    assert db.query(BillingReservationLimit).filter_by(reservation_id=previous.id, limit_id=legacy.id).count() == 0
    with pytest.raises(ValueError, match="积分或额度不足"):
        reserve(db, request_key="legacy-too-large", max_milli=20_000, **args)
    db.rollback()
    reserve(db, request_key="legacy-within", max_milli=10_000, **args)
    db.commit()
    db.refresh(legacy)
    assert (legacy.accounting_version, legacy.reserved_milli) == (1, 50_000)
    assert db.query(BillingReservationLimit).filter_by(reservation_id=previous.id, limit_id=legacy.id).count() == 1
    settle(db, request_key=previous.request_key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
    db.commit()
    db.refresh(legacy)
    assert (legacy.used_milli, legacy.reserved_milli) == (30_000, 10_000)


def test_previous_month_hold_settles_only_against_its_original_period(org_scope):
    from app.services.billing_ledger_service import get_or_create_wallet, grant

    db, enterprise, group, users = org_scope
    wallet = get_or_create_wallet(db, "enterprise", enterprise.id)
    grant(db, wallet_id=wallet.id, grant_key="cross-month-funds", kind="purchase", amount_milli=100_000)
    pending = reserve(db, request_key="cross-month", model_name="deepseek-flash", user_id=users["member"].id,
                      max_milli=40_000, enterprise_wallet_id=wallet.id, enterprise_id=enterprise.id,
                      group_id=group.id)
    current_start, current_end = quotas._period_bounds(quotas.current_period())
    previous_moment = current_start - timedelta(seconds=1)
    previous_key = previous_moment.replace(tzinfo=timezone.utc).astimezone(quotas.SHANGHAI).strftime("%Y-%m")
    previous_period, _ = quotas._period_bounds(previous_key)
    pending.created_at = previous_moment
    old_limit = BillingLimit(limit_key="previous-month-test", enterprise_id=enterprise.id,
                             period_start=previous_period, period_end=current_start,
                             limit_milli=50_000, used_milli=0, reserved_milli=40_000,
                             accounting_version=1)
    db.add(old_limit)
    db.flush()
    db.add(BillingReservationLimit(reservation_id=pending.id, limit_id=old_limit.id, reserved_milli=40_000))
    db.commit()
    current = quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                               scope="enterprise", limit_credits=20)
    assert (current["used_milli"], current["reserved_milli"]) == (0, 0)
    settle(db, request_key=pending.request_key, actual_milli=30_000, input_tokens=30_000, output_tokens=0)
    db.commit()
    db.refresh(old_limit)
    current_limit = db.query(BillingLimit).filter_by(enterprise_id=enterprise.id, period_start=current_start,
                                                     period_end=current_end).one()
    assert (old_limit.used_milli, old_limit.reserved_milli) == (30_000, 0)
    assert (current_limit.used_milli, current_limit.reserved_milli) == (0, 0)


def test_quota_history_fetches_related_rows_in_bounded_batches(org_scope):
    from app.services.billing_ledger_service import get_or_create_wallet, grant

    db, enterprise, group, users = org_scope
    wallet = get_or_create_wallet(db, "enterprise", enterprise.id)
    grant(db, wallet_id=wallet.id, grant_key="batched-history", kind="purchase", amount_milli=100_000)
    for index in range(20):
        reserve(db, request_key=f"batched-{index}", model_name="deepseek-flash", user_id=users["member"].id,
                max_milli=1_000, enterprise_wallet_id=wallet.id, enterprise_id=enterprise.id, group_id=group.id)
    db.commit()
    related_reads = []

    def collect(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and any(
            name in statement for name in ("billing_reservation_part", "billing_reservation_limit")
        ):
            related_reads.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", collect)
    try:
        result = quotas.set_limit(db, enterprise.id, users["owner"].id, period=quotas.current_period(),
                                  scope="enterprise", limit_credits=50)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", collect)
    assert result["reserved_milli"] == 20_000
    assert len(related_reads) <= 3, "额度重建不应为每笔调用发起独立关联查询"
