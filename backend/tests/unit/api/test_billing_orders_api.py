from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.v1 import billing
from app.core.config import settings
from app.models.billing_ledger import BillingCreditBatch, BillingOrder, BillingPaymentEvent
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import billing_checkout_service, billing_ledger_service


def _enable_fake_sales(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(billing, "BILLING_SALES_CODE_GATE_OPEN", True)
    for name, value in {
        "billing_sales_enabled": True,
        "billing_commercial_metering_ready": True,
        "billing_entitlement_enforcement_ready": True,
        "alipay_app_id": "app-id",
        "alipay_seller_id": "seller-id",
        "alipay_notify_url": "https://prism.example/api/billing/payments/alipay/notify",
        "wechat_pay_app_id": "wechat-app",
        "wechat_pay_mchid": "merchant-id",
        "wechat_pay_merchant_serial": "merchant-serial",
        "wechat_pay_api_v3_key": "a" * 32,
        "wechat_pay_public_key_serial": "platform-serial",
        "wechat_pay_notify_url": "https://prism.example/api/billing/payments/wechat/notify",
    }.items():
        monkeypatch.setattr(settings, name, value)
    private_key = tmp_path / "merchant-key.pem"
    public_key = tmp_path / "provider-key.pem"
    private_key.write_bytes(b"test merchant key")
    public_key.write_bytes(b"test provider key")
    for name in (
        "alipay_private_key_path", "wechat_pay_merchant_private_key_path",
    ):
        monkeypatch.setattr(settings, name, str(private_key))
    for name in ("alipay_public_key_path", "wechat_pay_public_key_path"):
        monkeypatch.setattr(settings, name, str(public_key))


def test_sales_stays_closed_by_default_even_with_a_logged_in_user(db, monkeypatch):
    user = User(username="checkout-closed", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    monkeypatch.setattr(settings, "billing_sales_enabled", False)

    with pytest.raises(HTTPException) as error:
        billing.create_payment_order(
            billing.OrderRequest(product="personal_credits", quantity=1, channel="alipay",
                                 client_request_id="closed-req"), db=db, user=user,
        )

    assert error.value.status_code == 503
    assert db.query(BillingOrder).count() == 0


def test_checkout_uses_server_price_reuses_order_and_returns_own_code_only(db, monkeypatch, tmp_path):
    _enable_fake_sales(monkeypatch, tmp_path)
    user = User(username="checkout-open", password="x", role="user", status=1)
    outsider = User(username="checkout-outsider", password="x", role="user", status=1)
    db.add_all([user, outsider])
    db.commit()
    calls = []

    def fake_provider(order):
        calls.append(order.order_no)
        return {"payment_code_url": "https://qr.alipay.com/test", "provider_order_id": order.order_no}

    monkeypatch.setattr(billing, "_provider_order", fake_provider)
    payload = billing.OrderRequest(
        product="personal_credits", quantity=1, channel="alipay", client_request_id="checkout-request-1"
    )
    first = billing.create_payment_order(payload, db=db, user=user).data
    replay = billing.create_payment_order(payload, db=db, user=user).data

    assert first["order_no"] == replay["order_no"]
    assert first["amount_fen"] == 400
    assert first["payment_status"] == "ready"
    assert first["payment_code_url"] == "https://qr.alipay.com/test"
    assert calls == [first["order_no"]]
    with pytest.raises(HTTPException) as error:
        billing.order_detail(first["order_no"], db=db, user=outsider)
    assert error.value.status_code == 404


def test_checkout_does_not_retry_provider_order_with_same_idempotency_key(db, monkeypatch, tmp_path):
    _enable_fake_sales(monkeypatch, tmp_path)
    user = User(username="checkout-unknown", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    monkeypatch.setattr(
        billing, "_provider_order", lambda order: (_ for _ in ()).throw(TimeoutError("network state unknown"))
    )
    payload = billing.OrderRequest(
        product="personal_credits", quantity=1, channel="alipay", client_request_id="checkout-unknown-request"
    )

    with pytest.raises(HTTPException) as first_error:
        billing.create_payment_order(payload, db=db, user=user)
    order = db.query(BillingOrder).one()
    assert first_error.value.status_code == 502
    assert order.payment_status == "unknown"
    with pytest.raises(HTTPException) as retry_error:
        billing.create_payment_order(payload, db=db, user=user)
    assert retry_error.value.status_code == 409
    assert db.query(BillingOrder).count() == 1


def test_enterprise_order_history_is_company_scoped_and_usable_by_admin(db):
    owner = User(username="company-order-owner", password="x", role="user", status=1)
    admin = User(username="company-order-admin", password="x", role="user", status=1)
    employee = User(username="company-order-employee", password="x", role="user", status=1)
    outsider = User(username="company-order-outsider", password="x", role="user", status=1)
    db.add_all([owner, admin, employee, outsider])
    db.flush()
    company = Enterprise(name="企业账务", owner_id=owner.id, seat_limit=5, status="active",
                         paid_order_id="enterprise-order-history")
    other = Enterprise(name="其他企业", owner_id=outsider.id, seat_limit=2, status="active",
                       paid_order_id="other-enterprise-order-history")
    db.add_all([company, other])
    db.flush()
    db.add_all([
        EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
        EnterpriseMember(enterprise_id=company.id, user_id=admin.id, role="admin", status="active"),
        EnterpriseMember(enterprise_id=company.id, user_id=employee.id, role="member", status="active"),
        EnterpriseMember(enterprise_id=other.id, user_id=outsider.id, role="owner", status="active"),
        BillingOrder(order_no="company-created-order", user_id=owner.id, enterprise_id=company.id,
                     product="enterprise_create", amount_fen=34500, quantity=5, channel="alipay",
                     status="paid", payment_status="paid"),
        BillingOrder(order_no="admin-recharge-order", user_id=admin.id, enterprise_id=company.id,
                     product="enterprise_credits", amount_fen=400, quantity=1, channel="wechat",
                     status="paid", payment_status="paid"),
        BillingOrder(order_no="other-company-order", user_id=outsider.id, enterprise_id=other.id,
                     product="enterprise_credits", amount_fen=400, quantity=1, channel="wechat",
                     status="paid", payment_status="paid"),
    ])
    db.commit()

    rows = billing.enterprise_orders(company.id, db=db, user=admin).data

    assert {row["order_no"] for row in rows} == {"company-created-order", "admin-recharge-order"}
    assert all(row["enterprise_id"] == company.id for row in rows)
    detail = billing.order_detail("company-created-order", db=db, user=admin).data
    assert detail["enterprise_id"] == company.id
    from app.core.exceptions import ForbiddenError

    with pytest.raises(ForbiddenError):
        billing.enterprise_orders(company.id, db=db, user=employee)
    with pytest.raises(ForbiddenError):
        billing.enterprise_orders(company.id, db=db, user=outsider)


def test_status_refresh_only_grants_after_provider_query_confirms_payment(db, monkeypatch):
    user = User(username="checkout-refresh", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    order = billing_checkout_service.create_order(db, user_id=user.id, product="personal_credits",
                                                 quantity=2, channel="alipay")
    order.payment_status = "unknown"
    db.commit()
    monkeypatch.setattr(billing, "_gateway_configured", lambda channel: True)
    monkeypatch.setattr(billing, "_provider_trade_query", lambda row: {
        "state": "TRADE_SUCCESS", "order_no": row.order_no, "trade_id": "ali-refresh", "amount_fen": 800,
    })

    result = billing.refresh_order_status(order.order_no, db=db, user=user).data

    assert result["status"] == "paid" and result["payment_status"] == "paid"
    assert db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).count() == 1
    wallet = billing_ledger_service.get_or_create_wallet(db, "user", user.id)
    assert billing_ledger_service.available_milli(db, wallet.id) == 2_000_000


def test_cancel_queries_first_and_only_closes_confirmed_unpaid_order(db, monkeypatch):
    user = User(username="checkout-cancel", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    order = billing_checkout_service.create_order(db, user_id=user.id, product="personal_credits",
                                                 quantity=1, channel="wechat")
    order.payment_status = "ready"
    db.commit()
    calls = []
    monkeypatch.setattr(billing, "_gateway_configured", lambda channel: True)
    monkeypatch.setattr(billing, "_provider_trade_query", lambda row: {
        "state": "NOTPAY", "order_no": row.order_no,
    })
    monkeypatch.setattr(billing, "_provider_close_trade", lambda row: calls.append(row.order_no))

    result = billing.cancel_payment_order(order.order_no, db=db, user=user).data

    assert calls == [order.order_no]
    assert result["status"] == "cancelled" and result["payment_status"] == "closed"
    assert db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).count() == 0


def test_refund_admin_execution_keeps_credit_locked_until_reconciled_success(db, monkeypatch):
    user = User(username="refund-api-user", password="x", role="user", status=1)
    admin = User(username="refund-api-admin", password="x", role="super_admin", status=1)
    db.add_all([user, admin])
    db.commit()
    order = billing_checkout_service.create_order(db, user_id=user.id, product="personal_credits",
                                                 quantity=1, channel="alipay")
    order.status = "paid"
    order.payment_status = "paid"
    db.add(BillingPaymentEvent(channel="alipay", trade_id="ali-refund-api", order_no=order.order_no,
                               amount_fen=order.amount_fen))
    wallet = billing_ledger_service.get_or_create_wallet(db, "user", user.id)
    billing_ledger_service.grant(db, wallet_id=wallet.id, grant_key=f"order:{order.order_no}", kind="purchase",
                                 amount_milli=1_000_000)
    db.commit()
    monkeypatch.setattr(billing, "_gateway_configured", lambda channel: True)
    monkeypatch.setattr(billing, "_provider_refund", lambda order, refund: {"state": "UNKNOWN"})
    monkeypatch.setattr(billing, "_provider_refund_query", lambda order, refund: {
        "state": "SUCCESS", "provider_refund_id": "ali-refund-api-result",
    })

    requested = billing.request_order_refund(
        order.order_no, billing.RefundRequest(reason="未使用积分"), db=db, user=user,
    ).data
    assert requested["status"] == "requested"
    started = billing.execute_refund(requested["refund_no"], db=db, admin=admin).data
    assert started["status"] == "unknown"
    assert billing_ledger_service.available_milli(db, wallet.id) == 0
    refund = db.query(billing.BillingRefund).filter_by(refund_no=requested["refund_no"]).one()
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    completed = billing.reconcile_refund(requested["refund_no"], db=db, _admin=admin).data
    db.refresh(order)
    assert completed["status"] == "succeeded"
    assert order.status == "refunded" and order.payment_status == "refunded"
    assert billing_ledger_service.available_milli(db, wallet.id) == 0


def test_refund_create_snapshot_conflict_keeps_processing_and_requires_query(db, monkeypatch):
    user = User(username="refund-snapshot-user", password="x", role="user", status=1)
    admin = User(username="refund-snapshot-admin", password="x", role="super_admin", status=1)
    db.add_all([user, admin])
    db.commit()
    order = billing_checkout_service.create_order(db, user_id=user.id, product="personal_credits",
                                                  quantity=1, channel="alipay")
    order.status = order.payment_status = "paid"
    db.add(BillingPaymentEvent(channel="alipay", trade_id="refund-snapshot-trade", order_no=order.order_no,
                               amount_fen=order.amount_fen))
    wallet = billing_ledger_service.get_or_create_wallet(db, "user", user.id)
    billing_ledger_service.grant(db, wallet_id=wallet.id, grant_key=f"order:{order.order_no}", kind="purchase",
                                 amount_milli=1_000_000)
    refund = billing_checkout_service.request_refund(db, order_no=order.order_no, requester_id=user.id, reason="退款")
    db.commit()
    calls = []
    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    monkeypatch.setattr(billing, "_provider_refund", lambda *_args: calls.append("sent") or {"state": "SUCCESS"})
    monkeypatch.setattr(billing_checkout_service, "update_refund_result", lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(billing_ledger_service.RetryableWalletSnapshot("积分账本刚发生变化，请重试")))
    with pytest.raises(HTTPException) as failed:
        billing.execute_refund(refund.refund_no, db=db, admin=admin)
    assert failed.value.status_code == 409 and "查单" in failed.value.detail
    db.refresh(refund)
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    assert refund.status == "processing" and refund.attempt_count == 1
    assert batch.refund_reserved_milli == 1_000_000 and calls == ["sent"]


def test_legacy_failed_refund_with_missing_evidence_can_only_reconcile(db, monkeypatch):
    user = User(username="legacy-refund-api-user", password="x", role="user", status=1)
    admin = User(username="legacy-refund-api-admin", password="x", role="super_admin", status=1)
    db.add_all([user, admin])
    db.commit()
    order = billing_checkout_service.create_order(db, user_id=user.id, product="personal_credits",
                                                 quantity=1, channel="alipay")
    order.status = order.payment_status = "paid"
    db.add(BillingPaymentEvent(channel="alipay", trade_id="ali-legacy-refund", order_no=order.order_no,
                               amount_fen=order.amount_fen))
    wallet = billing_ledger_service.get_or_create_wallet(db, "user", user.id)
    billing_ledger_service.grant(db, wallet_id=wallet.id, grant_key=f"order:{order.order_no}", kind="purchase",
                                 amount_milli=1_000_000)
    refund = billing_checkout_service.request_refund(db, order_no=order.order_no, requester_id=user.id, reason="退款")
    billing_checkout_service.prepare_refund(db, refund_no=refund.refund_no, admin_id=admin.id)
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    batch.refund_reserved_milli = 0  # Preserve what historical code left, do not infer a hold.
    refund.status = "failed"
    refund.provider_status = "FAILED"
    refund.refund_creation_attempt = None
    refund.reconciliation_query_attempt = None
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    assert billing._refund_view(refund)["requires_reconciliation"] is True
    with pytest.raises(HTTPException) as refused:
        billing.execute_refund(refund.refund_no, db=db, admin=admin)
    assert refused.value.status_code == 409
    monkeypatch.setattr(billing, "_gateway_configured", lambda channel: True)
    monkeypatch.setattr(billing, "_provider_refund_query", lambda order, refund: {"state": "FAILED"})
    result = billing.reconcile_refund(refund.refund_no, db=db, _admin=admin).data
    assert result["status"] == "unknown"
    assert order.status == "paid" and batch.refund_reserved_milli == 0


def test_manual_subscription_refund_requires_admin_decision_then_reconciles_same_refund(db, monkeypatch):
    user = User(username="manual-refund-reviewer", password="x", role="user", status=1)
    admin = User(username="manual-refund-platform-admin", password="x", role="super_admin", status=1)
    reviewer = User(username="manual-refund-independent-reviewer", password="x", role="admin", status=1)
    admin_role = Role(name="管理员", code="admin", status="active", sort=30, is_builtin=1)
    db.add_all([user, admin, reviewer, admin_role,
                Role(name="普通用户", code="user", status="active", sort=10, is_builtin=1),
                Role(name="审查员", code="reviewer", status="active", sort=20, is_builtin=1)])
    db.commit()
    db.add(UserRole(user_id=reviewer.id, role_id=admin_role.id))
    db.flush()
    billing.billing_refund_reviewer_service.set_reviewer(
        db, actor_id=admin.id, user_id=reviewer.id, active=True, reason="委任退款复核",
    )
    db.commit()
    order = billing_checkout_service.create_order(
        db, user_id=user.id, product="reviewer_month", quantity=1, channel="alipay",
    )
    billing_checkout_service._apply_verified(db, order, {"trade_id": "ali-manual-review"})
    db.commit()
    requested = billing_checkout_service.request_refund(
        db, order_no=order.order_no, requester_id=user.id, reason="会员人工退款",
    )
    db.commit()
    assert requested.status == "manual_review"

    reviewed = billing.admin_decide_refund(
        requested.refund_no,
        billing.RefundDecision(decision="approve", reason="平台管理员核验通过"),
        db=db, admin=reviewer,
    ).data
    assert reviewed["status"] == "approved"
    assert reviewed["reviewed_by"] == reviewer.id
    assert reviewed["review_note"] == "平台管理员核验通过"

    calls = []
    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    monkeypatch.setattr(billing, "_provider_refund", lambda _order, refund: calls.append(refund.refund_no) or {
        "state": "UNKNOWN",
    })
    started = billing.execute_refund(requested.refund_no, db=db, admin=admin).data
    assert started["status"] == "unknown" and calls == [requested.refund_no]
    with pytest.raises(HTTPException) as error:
        billing.execute_refund(requested.refund_no, db=db, admin=admin)
    assert error.value.status_code == 409 and calls == [requested.refund_no]

    refund = db.query(billing.BillingRefund).filter_by(refund_no=requested.refund_no).one()
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    monkeypatch.setattr(billing, "_provider_refund_query", lambda _order, _refund: {
        "state": "SUCCESS", "provider_refund_id": "ali-manual-refund-success",
    })
    completed = billing.reconcile_refund(requested.refund_no, db=db, _admin=admin).data
    db.refresh(order)
    assert completed["status"] == "succeeded"
    assert order.status == "refunded"


def test_manual_review_after_provider_success_requires_success_requery_before_local_recovery(db, monkeypatch):
    user = User(username="manual-refund-recovery-user", password="x", role="user", status=1)
    admin = User(username="manual-refund-recovery-admin", password="x", role="super_admin", status=1)
    db.add_all([user, admin])
    db.commit()
    order = billing_checkout_service.create_order(
        db, user_id=user.id, product="personal_credits", quantity=1, channel="alipay",
    )
    billing_checkout_service._apply_verified(db, order, {"trade_id": "ali-recovery-source"})
    db.commit()
    refund = billing_checkout_service.request_refund(
        db, order_no=order.order_no, requester_id=user.id, reason="本地账本恢复验证",
    )
    db.commit()
    billing_checkout_service.prepare_refund(db, refund_no=refund.refund_no, admin_id=admin.id)
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    batch.remaining_milli -= 1
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    token = billing_checkout_service.claim_refund_reconciliation(db, refund_no=refund.refund_no)
    assert token
    db.commit()
    billing_checkout_service.update_refund_result(
        db, refund_no=refund.refund_no,
        result={"state": "SUCCESS", "provider_refund_id": "ali-recovery"},
        verified_provider_query=True, reconciliation_token=token,
    )
    db.commit()
    assert refund.status == "manual_review" and refund.provider_status == "SUCCESS"
    batch.remaining_milli += 1
    refund.updated_at -= timedelta(seconds=16)
    db.commit()

    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    monkeypatch.setattr(billing, "_provider_refund_query", lambda _order, _refund: {"state": "FAILED"})
    with pytest.raises(HTTPException) as error:
        billing.reconcile_refund(refund.refund_no, db=db, _admin=admin)
    assert error.value.status_code == 409
    db.refresh(refund)
    assert refund.status == "manual_review" and refund.provider_status == "SUCCESS"

    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    monkeypatch.setattr(billing, "_provider_refund_query", lambda _order, _refund: {
        "state": "SUCCESS", "provider_refund_id": "ali-recovery",
    })
    completed = billing.reconcile_refund(refund.refund_no, db=db, _admin=admin).data
    db.refresh(order)
    assert completed["status"] == "succeeded"
    assert order.status == "refunded" and order.payment_status == "refunded"


def test_refund_query_transport_failure_is_not_recorded_as_verified(db, monkeypatch):
    user = User(username="refund-query-timeout-user", password="x", role="user", status=1)
    admin = User(username="refund-query-timeout-admin", password="x", role="super_admin", status=1)
    db.add_all([user, admin])
    db.commit()
    order = billing_checkout_service.create_order(db, user_id=user.id, product="personal_credits", quantity=1,
                                                  channel="alipay")
    billing_checkout_service._apply_verified(db, order, {"trade_id": "timeout-query"})
    db.commit()
    refund = billing_checkout_service.request_refund(db, order_no=order.order_no, requester_id=user.id, reason="退款")
    billing_checkout_service.prepare_refund(db, refund_no=refund.refund_no, admin_id=admin.id)
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    monkeypatch.setattr(billing, "_provider_refund_query", lambda *_args: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(HTTPException) as error:
        billing.reconcile_refund(refund.refund_no, db=db, _admin=admin)
    assert error.value.status_code == 502
    db.refresh(refund)
    assert refund.status == "processing" and refund.reconciliation_query_attempt == 0
    assert refund.reconciliation_claim_token is None


def test_refund_create_transport_failure_cannot_authorize_release(db, monkeypatch):
    user = User(username="refund-create-timeout-user", password="x", role="user", status=1)
    admin = User(username="refund-create-timeout-admin", password="x", role="super_admin", status=1)
    db.add_all([user, admin])
    db.commit()
    order = billing_checkout_service.create_order(db, user_id=user.id, product="personal_credits", quantity=1,
                                                  channel="alipay")
    billing_checkout_service._apply_verified(db, order, {"trade_id": "timeout-create"})
    refund = billing_checkout_service.request_refund(db, order_no=order.order_no, requester_id=user.id, reason="退款")
    db.commit()
    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    monkeypatch.setattr(billing, "_provider_refund", lambda *_args: (_ for _ in ()).throw(TimeoutError()))
    billing.execute_refund(refund.refund_no, db=db, admin=admin)
    assert refund.refund_creation_attempt != refund.attempt_count
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    monkeypatch.setattr(billing, "_provider_refund_query", lambda *_args: {"state": "FAILED"})
    reconciled = billing.reconcile_refund(refund.refund_no, db=db, _admin=admin).data
    assert reconciled["status"] == "unknown"
    assert batch.refund_reserved_milli == batch.remaining_milli


@pytest.mark.parametrize("in_flight_state", ["PROCESSING", "UNKNOWN"])
@pytest.mark.parametrize(
    "query_state,expected_status",
    [("SUCCESS", "succeeded"), ("FAILED", "failed"), ("PROCESSING", "processing"), ("UNKNOWN", "unknown")],
)
def test_manual_review_in_flight_refund_reconciles_all_provider_states_after_ledger_repair(
    db, monkeypatch, in_flight_state, query_state, expected_status,
):
    user = User(
        username=f"manual-refund-{in_flight_state.lower()}-{query_state.lower()}-user",
        password="x", role="user", status=1,
    )
    admin = User(
        username=f"manual-refund-{in_flight_state.lower()}-{query_state.lower()}-admin",
        password="x", role="super_admin", status=1,
    )
    db.add_all([user, admin])
    db.commit()
    order = billing_checkout_service.create_order(
        db, user_id=user.id, product="personal_credits", quantity=1, channel="alipay",
    )
    billing_checkout_service._apply_verified(db, order, {"trade_id": f"ali-{in_flight_state.lower()}-source"})
    db.commit()
    refund = billing_checkout_service.request_refund(
        db, order_no=order.order_no, requester_id=user.id, reason="在途渠道账本复核",
    )
    db.commit()
    billing_checkout_service.prepare_refund(db, refund_no=refund.refund_no, admin_id=admin.id)
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    batch.remaining_milli -= 1
    db.commit()
    billing_checkout_service.update_refund_result(
        db, refund_no=refund.refund_no, result={"state": in_flight_state},
        attempt_no=1, create_response_verified=True,
    )
    db.commit()
    assert refund.status == "manual_review" and refund.provider_status == in_flight_state

    batch.remaining_milli += 1
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    monkeypatch.setattr(billing, "_provider_refund_query", lambda _order, _refund: {"state": query_state})
    reconciled = billing.reconcile_refund(refund.refund_no, db=db, _admin=admin).data
    assert reconciled["status"] == expected_status
    db.refresh(batch)
    db.refresh(order)
    if query_state == "SUCCESS":
        assert order.status == "refunded" and order.payment_status == "refunded"
        assert (batch.remaining_milli, batch.refund_reserved_milli) == (0, 0)
    elif query_state in {"PROCESSING", "UNKNOWN"}:
        assert order.status == "paid"
        assert batch.refund_reserved_milli == batch.remaining_milli
    else:
        assert order.status == "paid" and batch.refund_reserved_milli == 0
