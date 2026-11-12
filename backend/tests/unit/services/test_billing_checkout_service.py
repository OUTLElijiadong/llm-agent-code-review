import base64
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.exceptions import ConflictError, ForbiddenError
from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingPaymentEvent,
    BillingPermanentReviewer,
    BillingSubscriptionPeriod,
)
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services import billing_entitlement_service as entitlements
from app.services.billing_ledger_service import (
    BillingProviderIdentity,
    available_milli,
    get_or_create_wallet,
)
from app.services.billing_payment_verification import PaymentVerificationError
from app.services.billing_products import CREDIT_PACK_CREDITS, CREDIT_PACK_MILLI

NOW = datetime(2026, 10, 21, 0, 0)
_TEST_PROVIDER_IDENTITY = BillingProviderIdentity(
    identity_version="deepseek-official-v1", provider_name="deepseek",
    endpoint_host="api.deepseek.com", credential_source="system",
)


def reserve_for_test(db, **kwargs):
    from app.services.billing_ledger_service import reserve as ledger_reserve

    return ledger_reserve(db, provider_identity=_TEST_PROVIDER_IDENTITY, **kwargs)


@pytest.fixture
def signed_provider():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def params(order, trade_no):
        amount = f"{order.amount_fen // 100}.{order.amount_fen % 100:02d}"
        data = {"app_id": "app", "seller_id": "seller", "out_trade_no": order.order_no, "trade_no": trade_no,
                "trade_status": "TRADE_SUCCESS", "total_amount": amount,
                "sign_type": "RSA2"}
        raw = "&".join(f"{k}={data[k]}" for k in sorted(data) if k not in {"sign", "sign_type"})
        data["sign"] = base64.b64encode(key.sign(raw.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
        return data

    return public, params


@pytest.fixture
def buyer(db):
    row = User(username="billing-buyer", password="x", role="user", status=1)
    db.add_all([row,
                Role(name="普通用户", code="user", status="active", sort=10, is_builtin=1),
                Role(name="审查员", code="reviewer", status="active", sort=20, is_builtin=1)])
    db.commit()
    return row


def _apply(db, order, public, params, *, now=NOW):
    return checkout.apply_alipay(db, order_no=order.order_no, params=params, public_key_pem=public,
                                 expected_appid="app", expected_seller="seller", now=now)


def _query_refund(db, refund, result, *, now=NOW):
    refund.updated_at = now - timedelta(seconds=16)
    db.flush()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=now)
    assert token
    db.flush()
    return checkout.update_refund_result(db, refund_no=refund.refund_no, result=result,
                                         verified_provider_query=True, reconciliation_token=token, now=now)


@pytest.mark.parametrize(
    ("status", "provider", "creation", "query", "pending"),
    [
        ("approved", "FAILED", None, None, True),
        ("approved", "FAILED", 1, None, True),
        ("approved", "FAILED", 0, 1, True),
        ("approved", "NOT_FOUND", 1, 1, True),
        ("failed", "FAILED", None, None, True),
        ("failed", "FAILED", 1, 0, True),
        ("failed", "NOT_FOUND", 1, 1, True),
        ("approved", "FAILED", 1, 1, False),
        ("failed", "FAILED", 1, 1, False),
    ],
)
def test_legacy_uncertain_refund_is_not_a_paid_reviewer_entitlement(
    db, buyer, signed_provider, status, provider, creation, query, pending,
):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "legacy-reviewer-evidence"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=777,
                                  decision="approve", reason="核验", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1777)
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    hold_before = batch.refund_reserved_milli
    refund.status, refund.provider_status = status, provider
    refund.refund_creation_attempt, refund.reconciliation_query_attempt = creation, query
    db.commit()

    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW) is not pending
    assert batch.refund_reserved_milli == hold_before


@pytest.mark.parametrize(
    ("status", "provider", "creation", "query", "pending"),
    [("approved", "FAILED", None, None, True), ("failed", "FAILED", 0, 1, True),
     ("approved", "NOT_FOUND", 1, 1, True), ("failed", "FAILED", 1, 1, False)],
)
def test_legacy_uncertain_enterprise_refund_blocks_only_its_own_members(
    db, buyer, signed_provider, status, provider, creation, query, pending,
):
    public, sign = signed_provider
    now = checkout._now()
    companies = []
    employee = User(username="refund-evidence-employee", password="x", role="user", status=1)
    db.add(employee)
    db.flush()
    for index in range(2):
        order = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=2,
                                      channel="alipay", enterprise_name=f"退款证据企业{index}")
        _apply(db, order, public, sign(order, f"refund-evidence-company-{index}"), now=now)
        company = db.query(Enterprise).filter_by(paid_order_id=order.order_no).one()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=employee.id, role="member", status="active"))
        companies.append((company, order))
    db.commit()
    company, order = companies[0]
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=777,
                                  decision="approve", reason="核验", now=now)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1777)
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    hold_before = batch.refund_reserved_milli
    refund.status, refund.provider_status = status, provider
    refund.refund_creation_attempt, refund.reconciliation_query_attempt = creation, query
    db.commit()

    assert checkout.enterprise_subscription_refund_pending(db, company.id) is pending
    from app.services import enterprise_org_service as org
    if pending:
        with pytest.raises(ForbiddenError, match="退款处理中"):
            org.require_enterprise_member(db, company.id, employee.id)
    else:
        assert org.require_enterprise_member(db, company.id, employee.id)
    assert org.require_enterprise_member(db, companies[1][0].id, employee.id)
    assert batch.refund_reserved_milli == hold_before


def test_unproven_manual_review_failed_refund_cannot_be_decided_before_reconciliation(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "manual-review-missing-evidence"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=777,
                                  decision="approve", reason="核验", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1777)
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    batch.refund_reserved_milli = 0  # A legacy transaction may already have released it.
    refund.status, refund.provider_status = "manual_review", "FAILED"
    refund.refund_creation_attempt = refund.reconciliation_query_attempt = None
    refund.updated_at = NOW
    db.commit()

    assert checkout.refund_requires_reconciliation(refund)
    assert not entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)
    with pytest.raises(ValueError, match="查单|核对"):
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=778,
                                      decision="reject", reason="核验", now=NOW)
    db.rollback()
    assert checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))


def test_unresolved_paid_refund_does_not_remove_independent_permanent_reviewer_entitlement(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "permanent-reviewer-refund"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=777,
                                  decision="approve", reason="核验", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1777)
    refund.status, refund.provider_status = "approved", "FAILED"
    refund.refund_creation_attempt = refund.reconciliation_query_attempt = None
    db.add(BillingPermanentReviewer(user_id=buyer.id, status="active", granted_by=99, granted_at=NOW))
    db.commit()
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)


def test_payment_verifies_before_grant_and_replay_is_idempotent(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=2, channel="alipay")
    from app.services.billing_rate_catalog_service import active_catalog

    assert order.amount_fen == 2 * active_catalog(db).credit_pack_price_fen
    db.commit()
    params = sign(order, "ali-one")
    with pytest.raises(PaymentVerificationError):
        _apply(db, order, public, {**params, "total_amount": "0.01"})
    db.rollback()
    assert db.query(BillingPaymentEvent).count() == 0
    assert db.query(BillingCreditBatch).count() == 0
    _apply(db, order, public, params)
    db.commit()
    _apply(db, order, public, params)
    db.commit()
    assert db.query(BillingPaymentEvent).count() == 1
    wallet = get_or_create_wallet(db, "user", buyer.id)
    assert available_milli(db, wallet.id, NOW) == 2_000_000
    assert db.query(BillingCreditBatch).one().issued_milli == 2 * CREDIT_PACK_MILLI
    with pytest.raises(ValueError, match="不一致"):
        _apply(db, order, public, sign(order, "ali-different"))
    db.rollback()
    assert db.query(BillingPaymentEvent).count() == 1


def test_signed_paid_notification_wins_over_racing_local_cancel(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    order.status = "cancelled"
    order.payment_status = "closed"
    db.commit()

    _apply(db, order, public, sign(order, "paid-close-race"))
    db.commit()

    db.refresh(order)
    wallet = get_or_create_wallet(db, "user", buyer.id)
    assert (order.status, order.payment_status) == ("paid", "paid")
    assert available_milli(db, wallet.id, NOW) == 1_000_000
    assert db.query(BillingCreditBatch).count() == 1


def test_callback_cannot_use_a_different_channel(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="wechat")
    db.commit()
    with pytest.raises(ValueError, match="渠道不匹配"):
        _apply(db, order, public, sign(order, "ali-wrong-channel"))
    db.rollback()
    assert db.query(BillingPaymentEvent).count() == 0
    assert db.query(BillingCreditBatch).count() == 0


def test_order_creation_is_idempotent_and_rejects_request_key_reuse(db, buyer):
    first = checkout.create_order(
        db, user_id=buyer.id, product="personal_credits", quantity=2,
        channel="alipay", client_request_id="checkout-req-1",
    )
    db.commit()
    replay = checkout.create_order(
        db, user_id=buyer.id, product="personal_credits", quantity=2,
        channel="alipay", client_request_id="checkout-req-1",
    )
    assert replay.id == first.id and replay.order_no == first.order_no
    with pytest.raises(ValueError, match="编号已用于其他商品或金额"):
        checkout.create_order(
            db, user_id=buyer.id, product="personal_credits", quantity=3,
            channel="alipay", client_request_id="checkout-req-1",
        )


def test_credit_checkout_fails_closed_when_credit_grant_and_catalog_basis_disagree(db, buyer, monkeypatch):
    from app.services import billing_rate_catalog_service

    monkeypatch.setattr(billing_rate_catalog_service, "CREDIT_PACK_CREDITS", CREDIT_PACK_CREDITS + 1)

    with pytest.raises(ValueError, match="积分商品与费率目录未对齐"):
        checkout.create_order(
            db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay",
        )


def test_reviewer_renewal_not_granted_early(db, buyer, signed_provider):
    public, sign = signed_provider
    first = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, first, public, sign(first, "ali-first"))
    second = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, second, public, sign(second, "ali-second"), now=NOW+timedelta(days=1))
    db.commit()
    periods = db.query(BillingSubscriptionPeriod).order_by(BillingSubscriptionPeriod.starts_at).all()
    assert len(periods) == 2 and periods[0].granted_at and not periods[1].granted_at
    assert periods[0].ends_at == periods[1].starts_at
    assert not checkout.activate_due_period(db, periods[1].id, now=NOW+timedelta(days=2))
    assert checkout.activate_due_period(db, periods[1].id, now=periods[1].starts_at)
    assert not checkout.activate_due_period(db, periods[1].id, now=periods[1].starts_at)
    db.commit()
    assert db.query(BillingCreditBatch).count() == 2
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=periods[1].ends_at+timedelta(days=6))
    assert not entitlements.has_reviewer_entitlement(db, buyer.id, now=periods[1].ends_at+timedelta(days=7))


def test_reviewer_role_sync_uses_the_same_future_payment_timestamp_as_period_activation(db, buyer, signed_provider):
    public, sign = signed_provider
    paid_at = checkout._now() + timedelta(days=3)
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "reviewer-future-paid-at"), now=paid_at)
    db.commit()

    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    db.refresh(buyer)
    assert period.granted_at == paid_at
    assert buyer.role == "reviewer"
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=paid_at)


def test_future_reviewer_period_restores_rbac_after_current_period_refund(db, buyer, signed_provider):
    public, sign = signed_provider
    first = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, first, public, sign(first, "reviewer-first-refunded"))
    second = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, second, public, sign(second, "reviewer-second-prepaid"), now=NOW + timedelta(days=1))
    db.commit()

    periods = db.query(BillingSubscriptionPeriod).order_by(BillingSubscriptionPeriod.starts_at).all()
    assert len(periods) == 2 and periods[1].granted_at is None
    refund = checkout.request_refund(db, order_no=first.order_no, requester_id=buyer.id, reason="当前周期退款复核")
    db.commit()
    checkout.review_manual_refund(
        db, refund_no=refund.refund_no, admin_id=901, decision="approve", reason="核验通过", now=NOW,
    )
    db.commit()
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1901)
    _query_refund(db, refund, {"state": "SUCCESS", "provider_refund_id": "refund-current-period"})
    db.commit()
    db.refresh(buyer)
    assert buyer.role == "user"
    assert not entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)

    assert checkout.activate_due_period(db, periods[1].id, now=periods[1].starts_at)
    db.commit()
    db.refresh(buyer)
    assert buyer.role == "reviewer"
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=periods[1].starts_at)


def test_paid_reviewer_subscription_upgrades_role_and_expiry_revokes_permissions(
    db, buyer, signed_provider, monkeypatch
):
    from app.core.config import settings
    from app.services import rbac_service

    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    # Provider callback and RBAC must observe the same simulated wall clock;
    # otherwise this historical subscription depends on the day the test runs.
    monkeypatch.setattr(entitlements, "_now", lambda: NOW)
    role = db.query(Role).filter_by(code="reviewer", status="active").one()
    permission = Permission(code="review:start", name="启动审查", module="review", type="api")
    db.add(permission)
    db.flush()
    db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()

    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "ali-reviewer"))
    db.commit()

    db.refresh(buyer)
    assert buyer.role == "reviewer"
    assert rbac_service.has_effective_role(db, buyer.id, "reviewer")
    assert rbac_service.check_permission(db, buyer.id, "review:start")

    period = db.query(BillingSubscriptionPeriod).one()
    after_grace = period.ends_at + timedelta(days=7)
    monkeypatch.setattr(entitlements, "_now", lambda: after_grace)
    assert not rbac_service.has_effective_role(db, buyer.id, "reviewer")
    assert not rbac_service.check_permission(db, buyer.id, "review:start")
    assert entitlements.sync_reviewer_role(db, buyer.id)
    db.refresh(buyer)
    assert buyer.role == "user"


def test_reviewer_role_assignment_requires_entitlement_when_enforcement_is_enabled(db, buyer, monkeypatch):
    from app.core.config import settings
    from app.core.exceptions import ForbiddenError
    from app.services import rbac_service

    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    role = db.query(Role).filter_by(code="reviewer", status="active").one()

    with pytest.raises(ForbiddenError, match="有效审查员权益"):
        rbac_service.assign_roles_to_user(db, buyer.id, [role.id])

    from app.services import user_service

    with pytest.raises(ForbiddenError, match="有效审查员权益"):
        user_service.set_role(db, buyer.id, "reviewer", admin_id=1)


def test_enterprise_is_created_only_after_verified_payment(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=3,
                                  channel="alipay", enterprise_name="测试公司")
    db.commit()
    assert db.query(Enterprise).count() == 0
    _apply(db, order, public, sign(order, "ali-company"))
    db.commit()
    company = db.query(Enterprise).one()
    assert (company.owner_id, company.seat_limit) == (buyer.id, 3)
    assert db.query(EnterpriseMember).filter_by(enterprise_id=company.id, user_id=buyer.id, role="owner").count() == 1
    wallet = get_or_create_wallet(db, "enterprise", company.id)
    assert available_milli(db, wallet.id, NOW) == 15_000_000


def test_enterprise_renewal_requires_paid_callback_and_grants_at_next_cycle(db, buyer, signed_provider):
    public, sign = signed_provider
    create = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=3,
                                   channel="alipay", enterprise_name="续费公司")
    _apply(db, create, public, sign(create, "company-paid"))
    db.commit()
    company = db.query(Enterprise).one()
    order = checkout.create_order(db, user_id=buyer.id, product="enterprise_renew", quantity=5,
                                  enterprise_id=company.id, channel="alipay")
    db.commit()
    assert order.amount_fen == 34500
    assert db.query(BillingSubscriptionPeriod).count() == 1
    with pytest.raises(PaymentVerificationError):
        _apply(db, order, public, {**sign(order, "renew-paid"), "total_amount": "1.00"})
    db.rollback()
    assert db.query(BillingSubscriptionPeriod).count() == 1
    params = sign(order, "renew-paid")
    _apply(db, order, public, params, now=NOW + timedelta(days=1))
    db.commit()
    _apply(db, order, public, params, now=NOW + timedelta(days=1))
    db.commit()
    first, second = db.query(BillingSubscriptionPeriod).order_by(BillingSubscriptionPeriod.starts_at).all()
    assert first.ends_at == second.starts_at and second.granted_at is None
    assert second.credit_milli == 25_000_000 and second.seats == 5
    assert company.seat_limit == 3  # 已付续费的席位只在新周期开始时生效。
    wallet = get_or_create_wallet(db, "enterprise", company.id)
    assert available_milli(db, wallet.id, NOW) == 15_000_000
    assert checkout.enterprise_subscription_status(db, company.id, now=first.starts_at) == "active"
    assert checkout.activate_due_periods(db, now=second.starts_at) == [second.id]
    assert checkout.activate_due_periods(db, now=second.starts_at) == []
    db.commit()
    assert company.seat_limit == 5
    assert available_milli(db, wallet.id, second.starts_at) == 25_000_000
    assert checkout.enterprise_subscription_status(db, company.id, now=second.ends_at + timedelta(days=6)) == "grace"
    assert checkout.enterprise_subscription_status(db, company.id, now=second.ends_at + timedelta(days=7)) == "expired"


@pytest.mark.parametrize("after_days, expected_anchor", [(6, "old"), (7, "payment")])
def test_enterprise_renewal_grace_and_restart(db, buyer, signed_provider, after_days, expected_anchor):
    public, sign = signed_provider
    create = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=2,
                                   channel="alipay", enterprise_name="宽限公司")
    _apply(db, create, public, sign(create, "company"))
    db.commit()
    company = db.query(Enterprise).one()
    first = db.query(BillingSubscriptionPeriod).one()
    paid_at = first.ends_at + timedelta(days=after_days)
    order = checkout.create_order(db, user_id=buyer.id, product="enterprise_renew", quantity=4,
                                  enterprise_id=company.id, channel="alipay")
    assert order.amount_fen == 27600
    assert company.seat_limit == 2
    _apply(db, order, public, sign(order, "renew"), now=paid_at)
    db.commit()
    second = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    assert second.starts_at == (first.ends_at if expected_anchor == "old" else paid_at)
    assert second.granted_at == paid_at and second.seats == 4
    assert company.seat_limit == 4
    assert checkout.enterprise_subscription_status(db, company.id, now=paid_at) == "active"


def test_enterprise_renewal_allows_future_seat_change_but_rejects_unauthorized_admin(db, buyer, signed_provider):
    public, sign = signed_provider
    create = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=2,
                                   channel="alipay", enterprise_name="权限公司")
    _apply(db, create, public, sign(create, "company"))
    db.commit()
    company = db.query(Enterprise).one()
    order = checkout.create_order(db, user_id=buyer.id, product="enterprise_renew", quantity=3,
                                  enterprise_id=company.id, channel="alipay")
    assert order.amount_fen == 20700
    assert company.seat_limit == 2
    outsider = User(username="outside-renew", password="x", role="user", status=1)
    db.add(outsider)
    db.commit()
    with pytest.raises(ForbiddenError):
        checkout.create_order(db, user_id=outsider.id, product="enterprise_renew", quantity=2,
                              enterprise_id=company.id, channel="alipay")
    db.rollback()


def test_paid_renewal_applies_new_seat_count_only_at_period_start(db, buyer, signed_provider):
    public, sign = signed_provider
    create = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=2,
                                   channel="alipay", enterprise_name="席位异常")
    _apply(db, create, public, sign(create, "company"))
    db.commit()
    company = db.query(Enterprise).one()
    order = checkout.create_order(db, user_id=buyer.id, product="enterprise_renew", quantity=2,
                                  enterprise_id=company.id, channel="alipay")
    company.seat_limit = 4  # 模拟当前席位与下期订单生成时不同。
    db.commit()
    _apply(db, order, public, sign(order, "renew"), now=NOW + timedelta(days=1))
    db.commit()
    assert order.status == "paid"
    assert db.query(BillingPaymentEvent).count() == 2
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    assert period.seats == 2 and period.granted_at is None
    assert company.seat_limit == 4
    checkout.activate_due_period(db, period.id, now=period.starts_at)
    db.commit()
    assert company.seat_limit == 2


def test_expired_enterprise_blocks_members_but_keeps_admin_renewal_access(db, buyer, signed_provider, monkeypatch):
    from app.services import enterprise_org_service as org

    public, sign = signed_provider
    create = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=2,
                                   channel="alipay", enterprise_name="到期公司")
    _apply(db, create, public, sign(create, "expiry-company"))
    db.commit()
    company = db.query(Enterprise).one()
    first = db.query(BillingSubscriptionPeriod).one()
    employee = User(username="expiry-employee", password="x", role="user", status=1)
    db.add(employee)
    db.flush()
    db.add(EnterpriseMember(enterprise_id=company.id, user_id=employee.id, role="member", status="active"))
    db.commit()

    grace_at = first.ends_at + timedelta(days=6)
    assert checkout.enterprise_subscription_status(db, company.id, now=grace_at) == "grace"
    monkeypatch.setattr(checkout, "_now", lambda: grace_at)
    assert org.require_enterprise_member(db, company.id, employee.id)
    expired_at = first.ends_at + timedelta(days=7)
    monkeypatch.setattr(checkout, "_now", lambda: expired_at)
    assert checkout.enterprise_subscription_status(db, company.id, now=expired_at) == "expired"
    with pytest.raises(ForbiddenError, match="企业订阅已过期"):
        org.require_enterprise_member(db, company.id, employee.id)
    assert org.require_enterprise_admin(db, company.id, buyer.id).role == "owner"
    with pytest.raises(ConflictError, match="订阅已过期"):
        org.create_invitation(db, company.id, buyer.id)
    db.rollback()

    renewal = checkout.create_order(db, user_id=buyer.id, product="enterprise_renew", quantity=3,
                                    enterprise_id=company.id, channel="alipay")
    paid_at = expired_at + timedelta(days=1)
    _apply(db, renewal, public, sign(renewal, "restart-company"), now=paid_at)
    db.commit()
    monkeypatch.setattr(checkout, "_now", lambda: paid_at)
    assert company.seat_limit == 3
    assert checkout.enterprise_subscription_status(db, company.id, now=paid_at) == "active"
    assert org.require_enterprise_member(db, company.id, employee.id).role == "member"


def test_lowered_seat_limit_preserves_members_and_blocks_new_invitations(db, buyer, signed_provider):
    from app.services import enterprise_org_service as org

    public, sign = signed_provider
    create = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=3,
                                   channel="alipay", enterprise_name="保留成员公司")
    _apply(db, create, public, sign(create, "keep-members-company"))
    db.commit()
    company = db.query(Enterprise).one()
    employees = [User(username=f"seat-employee-{index}", password="x", role="user", status=1)
                 for index in range(2)]
    db.add_all(employees)
    db.flush()
    db.add_all([EnterpriseMember(enterprise_id=company.id, user_id=employee.id, role="member", status="active")
                for employee in employees])
    db.commit()

    renewal = checkout.create_order(db, user_id=buyer.id, product="enterprise_renew", quantity=1,
                                    enterprise_id=company.id, channel="alipay")
    _apply(db, renewal, public, sign(renewal, "reduce-seats"))
    db.commit()
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=renewal.order_no).one()
    assert company.seat_limit == 3
    checkout.activate_due_period(db, period.id, now=period.starts_at)
    db.commit()

    assert company.seat_limit == 1
    assert db.query(EnterpriseMember).filter_by(enterprise_id=company.id, status="active").count() == 3
    with pytest.raises(ConflictError, match="席位不足"):
        org.create_invitation(db, company.id, buyer.id)


def test_due_batch_skips_unpaid_and_expired_periods(db, buyer, signed_provider):
    public, sign = signed_provider
    create = checkout.create_order(db, user_id=buyer.id, product="enterprise_create", quantity=1,
                                   channel="alipay", enterprise_name="周期防护")
    _apply(db, create, public, sign(create, "company"))
    db.commit()
    first = db.query(BillingSubscriptionPeriod).one()
    company = db.query(Enterprise).one()
    unpaid = checkout.create_order(db, user_id=buyer.id, product="enterprise_renew", quantity=1,
                                   enterprise_id=company.id, channel="alipay")
    forged_period = BillingSubscriptionPeriod(subscription_id=first.subscription_id, order_no=unpaid.order_no,
                                              starts_at=NOW, ends_at=NOW + timedelta(days=1),
                                              credit_milli=5_000_000, seats=1)
    db.add(forged_period)
    db.flush()
    assert checkout.activate_due_periods(db, now=NOW) == []
    with pytest.raises(ValueError, match="已核验"):
        checkout.activate_due_period(db, forged_period.id, now=NOW)
    with pytest.raises(ValueError):
        checkout.activate_due_periods(db, now=NOW, limit=0)
    assert not checkout.activate_due_period(db, forged_period.id, now=NOW + timedelta(days=1))
    db.rollback()


def test_permanent_grant_requires_unique_super_admin_and_can_stack_with_paid(db, buyer, signed_provider):
    normal_admin = User(username="operator", password="x", role="admin", status=1)
    super_admin = User(username="admin", password="x", role="super_admin", status=1)
    role = Role(name="最高管理员", code="super_admin", status="active")
    db.add_all([normal_admin, super_admin, role])
    db.flush()
    db.add(UserRole(user_id=super_admin.id, role_id=role.id))
    db.commit()
    with pytest.raises(ValueError):
        entitlements.set_permanent(db, actor_id=normal_admin.id, user_id=buyer.id,
                                   active=True, reason="不允许", now=NOW)
    entitlements.set_permanent(db, actor_id=super_admin.id, user_id=buyer.id, active=True, reason="历史用户", now=NOW)
    assert entitlements.issue_permanent_month(db, buyer.id, now=NOW)
    assert entitlements.issue_permanent_month(db, buyer.id, now=NOW)
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "stacked"))
    db.commit()
    wallet = get_or_create_wallet(db, "user", buyer.id)
    assert available_milli(db, wallet.id, NOW) == 10_000_000
    entitlements.set_permanent(db, actor_id=super_admin.id, user_id=buyer.id, active=False, reason="已撤销", now=NOW)
    assert not entitlements.issue_permanent_month(db, buyer.id, now=NOW+timedelta(days=1))
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW+timedelta(days=1))
    assert available_milli(db, wallet.id, NOW) == 10_000_000  # already-issued credits are retained


def test_refund_freezes_unused_credits_until_provider_confirms_and_releases_on_unknown(db, buyer, signed_provider):
    from app.services.billing_ledger_service import InsufficientCredits

    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "refund-source"))
    db.commit()
    wallet = get_or_create_wallet(db, "user", buyer.id)

    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="未使用的充值")
    db.commit()
    assert refund.status == "requested" and refund.amount_fen == 400
    assert available_milli(db, wallet.id, NOW) == 0
    replay = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="未使用的充值")
    assert replay.id == refund.id

    with pytest.raises(InsufficientCredits):
        reserve_for_test(db, request_key="refund-lock-test", model_name="deepseek-flash", user_id=buyer.id,
                         max_milli=1, personal_wallet_id=wallet.id, now=NOW)
    db.rollback()

    refund, order = checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=77)
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "UNKNOWN"}, now=NOW)
    db.commit()
    assert refund.status == "unknown" and available_milli(db, wallet.id, NOW) == 0

    _query_refund(db, refund, {"state": "SUCCESS", "provider_refund_id": "ali-refund"})
    db.commit()
    db.refresh(order)
    db.refresh(refund)
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    assert (refund.status, refund.provider_refund_id, order.status, order.payment_status) == (
        "succeeded", "ali-refund", "refunded", "refunded")
    assert (batch.remaining_milli, batch.refund_reserved_milli) == (0, 0)
    assert available_milli(db, wallet.id, NOW) == 0
    checkout.update_refund_result(db, refund_no=refund.refund_no,
                                  result={"state": "SUCCESS", "provider_refund_id": "ali-refund"}, now=NOW,
                                  attempt_no=1)
    db.commit()
    db.refresh(refund)
    assert refund.status == "succeeded" and batch.remaining_milli == 0
    # Replayed paid notification after refund cannot grant a second batch.
    _apply(db, order, public, sign(order, "refund-source"))
    db.commit()
    assert db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").count() == 1


def test_refund_only_allows_full_unused_credit_pack_and_routes_other_products_to_review(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "partly-used-source"))
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    batch.remaining_milli -= 1
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="部分已使用")
    db.commit()
    assert refund.status == "manual_review" and "已消费" in refund.review_note
    with pytest.raises(ValueError, match="当前不能发起渠道退款"):
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=77)
    db.rollback()

    subscription = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, subscription, public, sign(subscription, "subscription-refund-source"))
    db.commit()
    manual = checkout.request_refund(db, order_no=subscription.order_no, requester_id=buyer.id, reason="会员退款申请")
    db.commit()
    assert manual.status == "manual_review" and "人工复核" in manual.review_note


def test_manual_subscription_refund_approval_freezes_entitlement_until_final_provider_result(
    db, buyer, signed_provider,
):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "reviewer-manual-refund"))
    db.commit()
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    wallet = get_or_create_wallet(db, "user", buyer.id)
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="会员退款复核")
    db.commit()
    assert refund.status == "manual_review"
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)

    reviewed = checkout.review_manual_refund(
        db, refund_no=refund.refund_no, admin_id=777, decision="approve", reason="已核验，全额退款",
        now=NOW,
    )
    db.commit()
    assert (reviewed.status, reviewed.reviewed_by, reviewed.review_note) == (
        "approved", 777, "已核验，全额退款")
    assert batch.refund_reserved_milli == batch.remaining_milli
    assert not entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)

    same_refund_no = reviewed.refund_no
    _, prepared_order = checkout.prepare_refund(db, refund_no=same_refund_no, admin_id=1777)
    db.commit()
    assert prepared_order.order_no == order.order_no
    checkout.update_refund_result(db, refund_no=same_refund_no, result={"state": "UNKNOWN"}, now=NOW)
    db.commit()
    assert reviewed.status == "unknown" and batch.refund_reserved_milli == batch.remaining_milli
    with pytest.raises(ValueError, match="未知状态"):
        checkout.prepare_refund(db, refund_no=same_refund_no, admin_id=1777)
    db.rollback()

    _query_refund(db, refund, {"state": "SUCCESS", "provider_refund_id": "manual-reviewer-refund"})
    db.commit()
    db.refresh(order)
    db.refresh(refund)
    db.refresh(batch)
    assert (refund.status, order.status, batch.remaining_milli, batch.refund_reserved_milli) == (
        "succeeded", "refunded", 0, 0)
    assert not entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)
    assert wallet.id == batch.wallet_id


@pytest.mark.parametrize("provider_state", ["FAILED"])
def test_manual_refund_provider_failure_restores_reviewer_and_retries_same_refund_number(
    db, buyer, signed_provider, provider_state,
):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "reviewer-failed-refund"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="失败后重试")
    db.commit()
    checkout.review_manual_refund(
        db, refund_no=refund.refund_no, admin_id=781, decision="approve", reason="同意全额退款", now=NOW,
    )
    db.commit()
    original_refund_no = refund.refund_no

    checkout.prepare_refund(db, refund_no=original_refund_no, admin_id=1781)
    checkout.update_refund_result(db, refund_no=original_refund_no, result={"state": "UNKNOWN"}, now=NOW,
                                  attempt_no=1, create_response_verified=True)
    _query_refund(db, refund, {"state": provider_state})
    db.commit()
    db.refresh(refund)
    period = db.query(BillingSubscriptionPeriod).one()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    assert refund.status == "approved"
    assert refund.reviewed_by == 781 and refund.review_note == "同意全额退款"
    assert batch.refund_reserved_milli == batch.remaining_milli
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)
    db.refresh(buyer)
    assert buyer.role == "reviewer"

    checkout.prepare_refund(db, refund_no=original_refund_no, admin_id=1781)
    db.commit()
    assert refund.refund_no == original_refund_no and refund.status == "processing"
    assert not entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)
    db.refresh(buyer)
    assert buyer.role == "user"


@pytest.mark.parametrize("product", ["reviewer_month", "personal_credits"])
def test_refund_query_not_found_keeps_frozen_until_late_success(db, buyer, signed_provider, product):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"late-not-found-{product}"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    if product == "reviewer_month":
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                      decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    refund.updated_at = NOW
    db.commit()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "NOT_FOUND"},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=17))
    db.commit()
    assert refund.status == "unknown" and refund.provider_status == "NOT_FOUND"
    assert order.status == "paid" and refund.attempt_count == 1
    with pytest.raises(ValueError, match="当前不能发起渠道退款"):
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    db.rollback()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                  now=NOW + timedelta(seconds=18), attempt_no=1)
    db.commit()
    assert refund.status == "unknown" and order.status == "paid"
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=33))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=34))
    db.commit()
    assert refund.status == "succeeded" and order.status == "refunded"


@pytest.mark.parametrize("state", ["FAILED", "UNKNOWN", "SUCCESS"])
def test_stale_refund_attempt_cannot_override_new_attempt(db, buyer, signed_provider, state):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"old-attempt-{state}"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                  decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    refund.attempt_count = 2  # Existing historical retry, or another attempt already started.
    db.commit()
    with pytest.raises(ValueError, match="代次缺失"):
        checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": state}, now=NOW)
    db.rollback()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": state},
                                  now=NOW, attempt_no=1)
    db.commit()
    if state == "SUCCESS":
        assert (refund.status, refund.provider_status, order.status) == ("manual_review", "SUCCESS", "paid")
        with pytest.raises(ValueError, match="必须通过已验签查单"):
            checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                          now=NOW, attempt_no=2)
        db.rollback()
    else:
        assert refund.status == "processing" and order.status == "paid"


@pytest.mark.parametrize("legacy_status", ["approved", "failed"])
@pytest.mark.parametrize("missing", ["creation", "query", "both"])
def test_legacy_failed_or_approved_without_attempt_evidence_requires_reconciliation(
    db, buyer, signed_provider, legacy_status, missing,
):
    public, sign = signed_provider
    product = "reviewer_month" if legacy_status == "approved" else "personal_credits"
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"legacy-missing-evidence-{legacy_status}-{missing}"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    if legacy_status == "approved":
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                      decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    if legacy_status == "failed":
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
        batch.refund_reserved_milli = 0  # Old code may have released the hold.
    refund.status = legacy_status
    refund.provider_status = "FAILED"
    refund.refund_creation_attempt = None if missing in {"creation", "both"} else refund.attempt_count
    refund.reconciliation_query_attempt = None if missing in {"query", "both"} else refund.attempt_count
    refund.updated_at = NOW
    db.commit()
    with pytest.raises(ValueError, match="核对"):
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    db.rollback()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=17))
    db.commit()
    assert refund.status == "succeeded" and order.status == "refunded"


@pytest.mark.parametrize("legacy_status", ["approved", "failed"])
@pytest.mark.parametrize("provider_status", [None, "UNKNOWN", "PROCESSING", "SUCCESS", "NOT_FOUND"])
def test_inconsistent_retryable_status_must_reconcile_instead_of_resending(
    db, buyer, signed_provider, legacy_status, provider_status,
):
    public, sign = signed_provider
    product = "reviewer_month" if legacy_status == "approved" else "personal_credits"
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"inconsistent-retry-{legacy_status}-{provider_status}"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    if legacy_status == "approved":
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                      decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    refund.status = legacy_status
    refund.provider_status = provider_status
    refund.refund_creation_attempt = refund.attempt_count
    refund.reconciliation_query_attempt = refund.attempt_count
    refund.updated_at = NOW
    db.commit()
    with pytest.raises(ValueError, match="核对"):
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    db.rollback()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token


@pytest.mark.parametrize("legacy_status", ["approved", "failed"])
def test_legacy_missing_creation_evidence_failed_query_does_not_invent_hold_or_retry(
    db, buyer, signed_provider, legacy_status,
):
    public, sign = signed_provider
    product = "reviewer_month" if legacy_status == "approved" else "personal_credits"
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"legacy-unproved-create-{legacy_status}"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    if legacy_status == "approved":
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                      decision="approve", reason="通过", now=NOW)
        period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    else:
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    if legacy_status == "failed":
        batch.refund_reserved_milli = 0
    expected_hold = batch.refund_reserved_milli
    refund.status = legacy_status
    refund.provider_status = "FAILED"
    refund.refund_creation_attempt = None
    refund.reconciliation_query_attempt = None
    refund.updated_at = NOW
    db.commit()
    _query_refund(db, refund, {"state": "FAILED"}, now=NOW + timedelta(seconds=16))
    db.commit()
    assert refund.status == "unknown" and order.status == "paid"
    assert batch.refund_reserved_milli == expected_hold
    with pytest.raises(ValueError, match="未知状态"):
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)


@pytest.mark.parametrize("legacy_status", ["approved", "failed"])
def test_legacy_not_found_refund_requires_requery_before_any_retry(db, buyer, signed_provider, legacy_status):
    public, sign = signed_provider
    product = "reviewer_month" if legacy_status == "approved" else "personal_credits"
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"legacy-not-found-{legacy_status}"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    if legacy_status == "approved":
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                      decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    if legacy_status == "failed":
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
        batch.refund_reserved_milli = 0  # Old code released this after NOT_FOUND.
    refund.status = legacy_status
    refund.provider_status = "NOT_FOUND"
    refund.updated_at = NOW
    db.commit()
    with pytest.raises(ValueError, match="必须先核对"):
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    db.rollback()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=17))
    db.commit()
    assert refund.status == "succeeded" and order.status == "refunded"


@pytest.mark.parametrize("legacy_status", ["approved", "failed"])
@pytest.mark.parametrize("queried_state", ["FAILED", "NOT_FOUND"])
def test_legacy_not_found_query_failure_never_marks_refunded(db, buyer, signed_provider, legacy_status, queried_state):
    public, sign = signed_provider
    product = "reviewer_month" if legacy_status == "approved" else "personal_credits"
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"old-refund-{legacy_status}-{queried_state}"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    if legacy_status == "approved":
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                      decision="approve", reason="通过", now=NOW)
        period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    else:
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    if legacy_status == "failed":
        batch.refund_reserved_milli = 0
    refund.status = legacy_status
    refund.provider_status = "NOT_FOUND"
    refund.updated_at = NOW
    db.commit()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": queried_state},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=17))
    db.commit()
    assert order.status == "paid" and batch.remaining_milli == batch.issued_milli
    assert refund.status != "succeeded"
    if legacy_status == "approved":
        assert batch.refund_reserved_milli == batch.remaining_milli
    else:
        assert batch.refund_reserved_milli == 0


def test_starting_refund_attempt_fences_old_reconciliation_claim(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "refund-attempt-fence"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                  decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "UNKNOWN"}, now=NOW,
                                  attempt_no=1, create_response_verified=True)
    _query_refund(db, refund, {"state": "FAILED"})
    refund.reconciliation_claim_token = "old-claim"
    refund.reconciliation_claimed_until = NOW + timedelta(minutes=1)
    db.commit()
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    db.commit()
    with pytest.raises(ValueError, match="认领已过期"):
        checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "FAILED"},
                                      verified_provider_query=True, reconciliation_token="old-claim", now=NOW)
    db.rollback()
    assert refund.attempt_count == 2 and refund.status == "processing"


@pytest.mark.parametrize("queried_state,late_state", [("PROCESSING", "FAILED"), ("FAILED", "SUCCESS")])
@pytest.mark.parametrize("product", ["reviewer_month", "personal_credits"])
def test_same_attempt_late_create_cannot_override_verified_query(
    db, buyer, signed_provider, queried_state, late_state, product,
):
    from app.models.audit_log import AuditLog

    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product=product, quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"same-attempt-{queried_state}"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    if product == "reviewer_month":
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                      decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    if product == "reviewer_month":
        period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
        grant_key = f"period:{period.id}"
    else:
        grant_key = f"order:{order.order_no}"
    batch = db.query(BillingCreditBatch).filter_by(grant_key=grant_key).one()
    refund.updated_at = NOW
    db.commit()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": queried_state},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=17))
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": late_state},
                                  now=NOW + timedelta(seconds=18), attempt_no=1)
    db.commit()
    assert order.status == "paid" and batch.remaining_milli == batch.issued_milli
    assert batch.refund_reserved_milli == batch.remaining_milli
    if queried_state == "PROCESSING":
        assert (refund.status, refund.provider_status) == ("processing", "PROCESSING")
    else:
        assert (refund.status, refund.provider_status) == ("manual_review", "UNKNOWN")
    assert db.query(AuditLog).filter_by(
        action="billing.refund.late_create_conflict", target_id=refund.refund_no,
    ).count() == 1


def test_legacy_null_query_attempt_requires_signed_query(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "legacy-null-query-attempt"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=781,
                                  decision="approve", reason="通过", now=NOW)
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    refund.reconciliation_query_attempt = None
    refund.updated_at = NOW
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                  now=NOW + timedelta(seconds=1), attempt_no=1)
    db.commit()
    assert refund.status == "processing" and order.status == "paid"
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=17))
    db.commit()
    assert refund.status == "succeeded" and order.status == "refunded"


def test_alipay_create_success_requires_verified_query_before_reclaim(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "alipay-refund-create-needs-query"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=778)
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"}, now=NOW, attempt_no=1)
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    assert refund.status == "processing" and order.status == "paid"
    assert batch.remaining_milli == batch.refund_reserved_milli == batch.issued_milli
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=16))
    assert token
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "SUCCESS"},
                                  verified_provider_query=True, reconciliation_token=token,
                                  now=NOW + timedelta(seconds=17))
    db.commit()
    assert refund.status == "succeeded" and order.status == "refunded" and batch.remaining_milli == 0


def test_create_timeout_then_failed_query_does_not_release_refund_lock(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "refund-create-timeout"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "UNKNOWN"},
                                  now=NOW, attempt_no=1, create_response_verified=False)
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    _query_refund(db, refund, {"state": "FAILED"}, now=NOW + timedelta(seconds=16))
    db.commit()
    assert refund.refund_creation_attempt != 1
    assert refund.status == "unknown" and batch.refund_reserved_milli == batch.remaining_milli


def test_verified_create_after_query_allows_later_failed_query_to_release(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "refund-create-late-response"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    _query_refund(db, refund, {"state": "PROCESSING"}, now=NOW + timedelta(seconds=16))
    db.commit()
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "UNKNOWN"},
                                  now=NOW + timedelta(seconds=17), attempt_no=1, create_response_verified=True)
    db.commit()
    assert refund.refund_creation_attempt == 1
    assert refund.status == "processing" and refund.provider_status == "PROCESSING"
    _query_refund(db, refund, {"state": "FAILED"}, now=NOW + timedelta(seconds=33))
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    assert refund.status == "failed" and batch.refund_reserved_milli == 0


def test_unversioned_create_cannot_override_verified_query(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "refund-unversioned-late"))
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1781)
    _query_refund(db, refund, {"state": "PROCESSING"}, now=NOW + timedelta(seconds=16))
    db.commit()
    with pytest.raises(ValueError, match="代次缺失"):
        checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "FAILED"},
                                      now=NOW + timedelta(seconds=17))
    db.rollback()
    db.refresh(refund)
    assert refund.status == "processing" and refund.provider_status == "PROCESSING"


def test_refunding_current_enterprise_renewal_restores_prior_valid_seats(db, buyer, signed_provider):
    public, sign = signed_provider
    create = checkout.create_order(
        db, user_id=buyer.id, product="enterprise_create", quantity=2, channel="alipay", enterprise_name="旧周期席位",
    )
    _apply(db, create, public, sign(create, "enterprise-prior-seat-cycle"), now=NOW)
    db.commit()
    company = db.query(Enterprise).filter_by(paid_order_id=create.order_no).one()
    previous = db.query(BillingSubscriptionPeriod).filter_by(order_no=create.order_no).one()
    renewal = checkout.create_order(
        db, user_id=buyer.id, product="enterprise_renew", quantity=5, enterprise_id=company.id, channel="alipay",
    )
    _apply(db, renewal, public, sign(renewal, "enterprise-refunded-seat-cycle"), now=previous.ends_at)
    db.commit()
    current = db.query(BillingSubscriptionPeriod).filter_by(order_no=renewal.order_no).one()
    assert current.granted_at is not None and company.seat_limit == 5

    refund = checkout.request_refund(
        db, order_no=renewal.order_no, requester_id=buyer.id, reason="新周期撤销",
    )
    db.commit()
    checkout.review_manual_refund(
        db, refund_no=refund.refund_no, admin_id=782, decision="approve", reason="核验后同意", now=previous.ends_at,
    )
    db.commit()
    assert checkout.enterprise_subscription_period(db, company.id, now=previous.ends_at).order_no == create.order_no

    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1782)
    _query_refund(db, refund, {"state": "SUCCESS", "provider_refund_id": "enterprise-refund"}, now=previous.ends_at)
    db.commit()
    db.refresh(company)
    db.refresh(renewal)
    db.refresh(refund)
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{current.id}").one()
    assert (refund.status, renewal.status, batch.remaining_milli, batch.refund_reserved_milli) == (
        "succeeded", "refunded", 0, 0,
    )
    assert company.seat_limit == 2 and company.billing_suspended is False
    assert checkout.enterprise_subscription_period(db, company.id, now=previous.ends_at).order_no == create.order_no


def test_manual_refund_rejection_records_admin_and_keeps_paid_entitlement(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "reviewer-manual-reject"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="待复核退款")
    db.commit()

    rejected = checkout.review_manual_refund(
        db, refund_no=refund.refund_no, admin_id=778, decision="reject", reason="周期已使用，不予退款",
        now=NOW,
    )
    db.commit()
    assert (rejected.status, rejected.reviewed_by, rejected.review_note) == (
        "rejected", 778, "周期已使用，不予退款")
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)
    with pytest.raises(ValueError, match="只能审核待人工复核"):
        checkout.review_manual_refund(
            db, refund_no=refund.refund_no, admin_id=778, decision="approve", reason="再次审批", now=NOW,
        )


def test_manual_refund_reviewer_cannot_execute_own_approval(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "independent-refund-review"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="申请退款")
    checkout.review_manual_refund(
        db, refund_no=refund.refund_no, admin_id=778, decision="approve", reason="核对通过", now=NOW,
    )
    db.commit()

    with pytest.raises(ValueError, match="审核人.*执行"):
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=778)
    db.rollback()
    db.refresh(refund)
    assert refund.status == "approved"


def test_manual_refund_applicant_cannot_review_own_request(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "self-refund-review"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="申请退款")
    db.commit()
    with pytest.raises(ValueError, match="申请人不能审核"):
        checkout.review_manual_refund(
            db, refund_no=refund.refund_no, admin_id=buyer.id, decision="approve", reason="自审", now=NOW,
        )
    db.rollback()
    assert refund.status == "manual_review"


@pytest.mark.parametrize("changed", ["attempted", "provider_result", "identity", "locked_amount"])
def test_legacy_approved_refund_recheck_fails_closed_without_proof(db, buyer, signed_provider, changed):
    owner = User(username="admin", password="x", role="super_admin", status=1)
    db.add(owner)
    db.commit()
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"legacy-refund-{changed}"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id,
                                  decision="approve", reason="历史审核", now=NOW)
    db.commit()
    if changed == "attempted":
        refund.attempt_count = 1
    elif changed == "provider_result":
        refund.provider_status = "NOT_FOUND"
    elif changed == "identity":
        owner.role = "admin"
    else:
        period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
        batch.refund_reserved_milli -= 1
    db.commit()
    with pytest.raises(ValueError):
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id + 100,
                                      decision="approve", reason="独立复核", now=NOW)
    db.rollback()
    assert refund.status == "approved" and refund.reviewed_by == owner.id


@pytest.mark.parametrize("refund_lock", [0, 1])
def test_legacy_refund_rejection_rejects_inconsistent_freeze_and_rolls_back_audit(
    db, buyer, signed_provider, refund_lock,
):
    from app.models.audit_log import AuditLog

    owner = User(username="admin", password="x", role="super_admin", status=1)
    db.add(owner)
    db.commit()
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, f"legacy-reject-inconsistent-{refund_lock}"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id,
                                  decision="approve", reason="原审核", now=NOW)
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    batch.refund_reserved_milli = refund_lock
    db.commit()
    old_audit_count = db.query(AuditLog).filter_by(target_id=refund.refund_no).count()
    with pytest.raises(ValueError, match="冻结额.*不一致"):
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id + 100,
                                      decision="reject", reason="独立复核拒绝", now=NOW)
    db.rollback()
    db.refresh(refund)
    db.refresh(batch)
    assert (refund.status, refund.reviewed_by, refund.review_note) == ("approved", owner.id, "原审核")
    assert batch.refund_reserved_milli == refund_lock and batch.remaining_milli == 5_000_000
    assert db.query(AuditLog).filter_by(target_id=refund.refund_no).count() == old_audit_count


def test_legacy_refund_rejection_releases_proven_freeze_and_audits(db, buyer, signed_provider):
    from app.models.audit_log import AuditLog

    owner = User(username="admin", password="x", role="super_admin", status=1)
    db.add(owner)
    db.commit()
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "legacy-reject-valid"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id,
                                  decision="approve", reason="原审核", now=NOW)
    db.commit()
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    assert batch.refund_reserved_milli == batch.remaining_milli == 5_000_000
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id + 100,
                                  decision="reject", reason="独立复核拒绝", now=NOW)
    db.commit()
    assert (refund.status, refund.reviewed_by, refund.review_note) == ("rejected", owner.id + 100, "独立复核拒绝")
    assert batch.refund_reserved_milli == 0 and batch.remaining_milli == 5_000_000
    assert db.query(AuditLog).filter_by(target_id=refund.refund_no, action="billing.refund.legacy_review").count() == 1
    assert entitlements.has_reviewer_entitlement(db, buyer.id, now=NOW)


def test_unapproved_manual_refund_rejection_refuses_unexpected_freeze(db, buyer, signed_provider):
    from app.models.audit_log import AuditLog

    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "manual-reject-unexpected-freeze"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款")
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    batch.refund_reserved_milli = 1
    db.commit()
    with pytest.raises(ValueError):
        checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=778,
                                      decision="reject", reason="拒绝", now=NOW)
    db.rollback()
    assert refund.status == "manual_review" and refund.reviewed_by is None
    assert batch.refund_reserved_milli == 1
    assert db.query(AuditLog).filter_by(target_id=refund.refund_no).count() == 0


def test_manual_subscription_refund_rejects_full_refund_after_settled_usage(db, buyer, signed_provider):
    from app.services.billing_ledger_service import settle

    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "reviewer-partly-used-refund"))
    db.commit()
    wallet = get_or_create_wallet(db, "user", buyer.id)
    reserve_for_test(db, request_key="reviewer-partly-used-call", model_name="deepseek-flash", user_id=buyer.id,
                     max_milli=2_000, personal_wallet_id=wallet.id, now=NOW)
    settle(db, request_key="reviewer-partly-used-call", actual_milli=1_000,
           input_tokens=1_000, output_tokens=0, now=NOW)
    db.commit()

    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="部分周期退款")
    db.commit()
    with pytest.raises(ValueError, match="已有已结算用量，不能批准全额退款"):
        checkout.review_manual_refund(
            db, refund_no=refund.refund_no, admin_id=780, decision="approve", reason="拒绝全额退款", now=NOW,
        )
    db.rollback()
    db.refresh(refund)
    assert refund.status == "manual_review" and refund.reviewed_by is None


def test_provider_success_manual_review_can_only_retry_local_finalization_after_success_recheck(
    db, buyer, signed_provider,
):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "refund-success-local-recovery"))
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="本地入账复核")
    db.commit()
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1901)
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    batch.remaining_milli -= 1
    db.commit()

    held = _query_refund(db, refund, {"state": "SUCCESS", "provider_refund_id": "provider-success"})
    db.commit()
    assert (held.status, held.provider_status, order.status) == ("manual_review", "SUCCESS", "paid")
    assert "本地权益处理未完成" in held.review_note
    with pytest.raises(ValueError, match="代次缺失"):
        checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "UNKNOWN"}, now=NOW)

    batch.remaining_milli += 1  # Simulate the independently repaired ledger invariant.
    refund.updated_at = NOW
    db.commit()
    token = checkout.claim_refund_reconciliation(db, refund_no=refund.refund_no, now=NOW + timedelta(seconds=20))
    assert token
    db.commit()
    completed = checkout.update_refund_result(
        db, refund_no=refund.refund_no, result={"state": "SUCCESS", "provider_refund_id": "provider-success"},
        now=NOW + timedelta(seconds=21), verified_provider_query=True, reconciliation_token=token,
    )
    db.commit()
    assert completed.status == "succeeded" and order.status == "refunded"
    assert (batch.remaining_milli, batch.refund_reserved_milli) == (0, 0)


def test_manual_refund_approval_fails_closed_on_unsettled_or_inconsistent_ledger(db, buyer, signed_provider):
    public, sign = signed_provider
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    _apply(db, order, public, sign(order, "reviewer-unsettled-refund"))
    db.commit()
    period = db.query(BillingSubscriptionPeriod).filter_by(order_no=order.order_no).one()
    wallet = get_or_create_wallet(db, "user", buyer.id)
    reserve_for_test(db, request_key="refund-pending-usage", model_name="deepseek-flash", user_id=buyer.id,
                     max_milli=100, personal_wallet_id=wallet.id, now=NOW)
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="有未结算用量")
    db.commit()

    with pytest.raises(ValueError, match="未结算"):
        checkout.review_manual_refund(
            db, refund_no=refund.refund_no, admin_id=779, decision="approve", reason="错误审批", now=NOW,
        )
    db.rollback()
    db.refresh(refund)
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period.id}").one()
    assert refund.status == "manual_review" and batch.refund_reserved_milli == 0

    batch.reserved_milli += 1
    db.commit()
    with pytest.raises(ValueError, match="账本不一致"):
        checkout.review_manual_refund(
            db, refund_no=refund.refund_no, admin_id=779, decision="approve", reason="错误账本", now=NOW,
        )
    db.rollback()
    db.refresh(refund)
    assert refund.status == "manual_review"


def test_refunded_initial_enterprise_is_billing_suspended_and_can_renew(db, buyer, signed_provider):
    public, sign = signed_provider
    create = checkout.create_order(
        db, user_id=buyer.id, product="enterprise_create", quantity=2, channel="alipay", enterprise_name="退款企业",
    )
    paid_at = checkout._now()
    _apply(db, create, public, sign(create, "enterprise-create-refund"), now=paid_at)
    db.commit()
    company = db.query(Enterprise).filter_by(paid_order_id=create.order_no).one()
    employee = User(username="refund-enterprise-member", password="x", role="user", status=1)
    db.add(employee)
    db.flush()
    db.add(EnterpriseMember(enterprise_id=company.id, user_id=employee.id, role="member", status="active"))
    db.commit()
    refund = checkout.request_refund(
        db, order_no=create.order_no, requester_id=buyer.id, reason="企业创建退款",
    )
    db.commit()

    checkout.review_manual_refund(
        db, refund_no=refund.refund_no, admin_id=780, decision="approve", reason="新企业退款通过", now=paid_at,
    )
    db.commit()
    from app.core.exceptions import ForbiddenError
    from app.services import enterprise_org_service as org

    with pytest.raises(ForbiddenError, match="退款处理中"):
        org.require_enterprise_member(db, company.id, employee.id)

    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=1780)
    checkout.update_refund_result(db, refund_no=refund.refund_no, result={"state": "UNKNOWN"}, now=paid_at)
    db.commit()
    assert company.billing_suspended is False  # pending state is enforced from the refund ledger

    _query_refund(db, refund, {"state": "SUCCESS", "provider_refund_id": "enterprise-create-refund"}, now=paid_at)
    db.commit()
    db.refresh(company)
    assert company.billing_suspended is True
    assert org.require_enterprise_admin(db, company.id, buyer.id)
    with pytest.raises(ForbiddenError, match="订阅"):
        org.require_enterprise_member(db, company.id, employee.id)

    renewal = checkout.create_order(
        db, user_id=buyer.id, product="enterprise_renew", quantity=2, channel="alipay", enterprise_id=company.id,
    )
    renewed_at = checkout._now()
    _apply(db, renewal, public, sign(renewal, "enterprise-create-renew"), now=renewed_at)
    db.commit()
    db.refresh(company)
    assert company.billing_suspended is True  # 续费周期尚未开始；旧有效订阅权益不能凭未来订单提前恢复。
    assert company.seat_limit == 2
    renewal_period = db.query(BillingSubscriptionPeriod).filter_by(order_no=renewal.order_no).one()
    assert not renewal_period.granted_at
    assert checkout.activate_due_period(db, renewal_period.id, now=renewal_period.starts_at)
    db.commit()
    db.refresh(company)
    assert company.billing_suspended is False
    assert company.seat_limit == 2
    assert org.require_enterprise_member(db, company.id, employee.id)
