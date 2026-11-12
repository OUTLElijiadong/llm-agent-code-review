"""Manual refunds require independently delegated review before execution."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import billing
from app.core.database import Base, get_db
from app.core.exceptions import AppError
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.billing_ledger import BillingOrder, BillingRefund
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services.billing_rate_catalog_service import seed_test_baseline


def test_refund_review_is_separate_from_other_financial_authorities(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    seed_test_baseline(db)
    roles = [Role(name=name, code=code, status="active") for name, code in (
        ("最高管理员", "super_admin"), ("管理员", "admin"), ("普通用户", "user"), ("审查员", "reviewer"),
    )]
    owner = User(username="admin", password="x", role="super_admin", status=1)
    reviewer = User(username="refund-auditor", password="x", role="admin", status=1)
    ungranted = User(username="other-admin", password="x", role="admin", status=1)
    buyer = User(username="refund-buyer", password="x", role="user", status=1)
    db.add_all([*roles, owner, reviewer, ungranted, buyer])
    db.flush()
    db.add_all([UserRole(user_id=user.id, role_id=role.id) for user, role in (
        (owner, roles[0]), (reviewer, roles[1]), (ungranted, roles[1]), (buyer, roles[2]),
    )])
    db.commit()
    order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
    checkout._apply_verified(db, order, {"trade_id": "mock-refund-review"})
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="申请退款")
    # Simulate an approval persisted by the old single-super-admin workflow.
    checkout.review_manual_refund(db, refund_no=refund.refund_no, admin_id=owner.id,
                                  decision="approve", reason="旧审核")
    db.query(AuditLog).filter_by(action="billing.refund.review", target_id=refund.refund_no).delete()
    db.commit()

    app = FastAPI()
    app.include_router(billing.router, prefix="/api")

    @app.exception_handler(AppError)
    async def app_error(_request, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def test_db():
        yield db

    app.dependency_overrides[get_db] = test_db
    def headers(user):
        return {"Authorization": "Bearer " + create_access_token(
            user.id, user.role, user.token_version or 0,
        )}

    decision_url = f"/api/billing/admin/refunds/{refund.refund_no}/decision"
    decision = {"decision": "approve", "reason": "复核通过"}
    with TestClient(app) as client:
        assert client.get("/api/billing/refund-reviews", headers=headers(ungranted)).status_code == 403
        assert client.post(decision_url, headers=headers(owner), json=decision).status_code == 403
        assert client.post("/api/billing/admin/refunds/not-a-refund/decision", headers=headers(ungranted),
                           json=decision).status_code == 403
        assert client.put(f"/api/billing/admin/refund-reviewers/{reviewer.id}", headers=headers(reviewer),
                          json={"active": True, "reason": "自行委任"}).status_code == 403
        assert client.put(f"/api/billing/admin/refund-reviewers/{reviewer.id}", headers=headers(owner),
                          json={"active": True, "reason": "委任复核"}).status_code == 200
        assert client.get("/api/billing/refund-reviews", headers=headers(reviewer)).json()["data"]["items"][0][
            "refund_no"] == refund.refund_no
        assert client.get("/api/billing/rate-reviews", headers=headers(reviewer)).status_code == 403
        assert client.post(f"/api/billing/admin/refunds/{refund.refund_no}/execute",
                           headers=headers(reviewer)).status_code == 403
        assert client.post(decision_url, headers=headers(reviewer), json=decision).status_code == 200
        assert client.post("/api/billing/admin/refunds/not-a-refund/decision", headers=headers(reviewer),
                           json=decision).status_code == 409
        assert db.query(AuditLog).filter_by(action="billing.refund.legacy_review", actor_id=owner.id,
                                            target_id=refund.refund_no).count() == 1
        assert db.query(AuditLog).filter_by(action="billing.refund.review", actor_id=reviewer.id,
                                            target_id=refund.refund_no).count() == 1
        monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
        monkeypatch.setattr(billing, "_provider_refund", lambda _order, _refund: {"state": "UNKNOWN"})
        assert client.post(f"/api/billing/admin/refunds/{refund.refund_no}/execute",
                           headers=headers(owner)).json()["data"]["status"] == "unknown"
        assert db.query(AuditLog).filter_by(action="billing.refund.execute", actor_id=owner.id,
                                            target_id=refund.refund_no).count() == 1
        for index, provider_state in enumerate((None, None, "UNKNOWN", "NOT_FOUND")):
            order_no = f"page-order-{index}"
            db.add(BillingOrder(order_no=order_no, user_id=buyer.id, product="reviewer_month", amount_fen=4900,
                                quantity=1, channel="alipay", status="paid", payment_status="paid"))
            db.add(BillingRefund(order_no=order_no, refund_no=f"page-refund-{index}", amount_fen=4900,
                                 reason="测试分页", status="manual_review", requested_by=buyer.id,
                                 provider_status=provider_state))
        db.add(BillingOrder(order_no="legacy-manual-order", user_id=buyer.id, product="reviewer_month",
                            amount_fen=4900, quantity=1, channel="alipay", status="paid", payment_status="paid"))
        db.add(BillingRefund(order_no="legacy-manual-order", refund_no="legacy-manual-refund", amount_fen=4900,
                             reason="旧状态", status="manual_review", requested_by=buyer.id,
                             provider_status="FAILED", attempt_count=1,
                             refund_creation_attempt=None, reconciliation_query_attempt=None))
        db.commit()
        page = client.get("/api/billing/refund-reviews?limit=1", headers=headers(reviewer)).json()["data"]
        assert [item["refund_no"] for item in page["items"]] == ["page-refund-1"]
        next_page = client.get(f"/api/billing/refund-reviews?limit=1&before_id={page['next_before_id']}",
                               headers=headers(reviewer)).json()["data"]
        assert [item["refund_no"] for item in next_page["items"]] == ["page-refund-0"]
        assert next_page["next_before_id"] is None
        assert client.post("/api/billing/admin/refunds/legacy-manual-refund/decision", headers=headers(reviewer),
                           json=decision).status_code == 409
        assert client.put(f"/api/billing/admin/refund-reviewers/{reviewer.id}", headers=headers(owner),
                          json={"active": False, "reason": "撤销复核"}).status_code == 200
        assert client.get("/api/billing/refund-reviews", headers=headers(reviewer)).status_code == 403
        assert client.put(f"/api/billing/admin/refund-reviewers/{reviewer.id}", headers=headers(owner),
                          json={"active": True, "reason": "重新委任"}).status_code == 200
        from app.services import user_service

        user_service.toggle_status(db, reviewer.id, 0, actor=owner)
        user_service.toggle_status(db, reviewer.id, 1, actor=owner)
        assert client.get("/api/billing/refund-reviews", headers=headers(reviewer)).status_code == 403
        user_service.set_role(db, reviewer.id, "user", admin_id=owner.id)
        user_service.set_role(db, reviewer.id, "admin", admin_id=owner.id)
        assert client.get("/api/billing/refund-reviews", headers=headers(reviewer)).status_code == 403
        from app.models.billing_ledger import BillingRefundReviewerGrant

        assert db.query(BillingRefundReviewerGrant).filter_by(user_id=reviewer.id).one().active == 0
    db.close()
    engine.dispose()
