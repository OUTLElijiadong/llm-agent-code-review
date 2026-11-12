import csv
import io

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.billing import router
from app.core.database import Base, get_db
from app.core.dependencies import require_super_admin
from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingStatementRow
from app.models.user import User


def _wechat_csv() -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["`交易时间", "`微信订单号", "`商户订单号", "`用户标识", "`交易状态", "`货币种类",
                     "`应结订单金额", "`手续费", "`订单金额"])
    writer.writerow(["`2026-11-04 12:00:00", "`wx-trade", "`pr-order", "`sensitive-openid", "`SUCCESS",
                     "`CNY", "`4.00", "`0.01", "`4.00"])
    return output.getvalue().encode("utf-8-sig")


def _client(db, admin):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def get_test_db():
        yield db

    def test_super_admin(x_test_user: int = Header()):
        if x_test_user != admin.id:
            raise HTTPException(status_code=403, detail="仅超级管理员")
        return admin

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[require_super_admin] = test_super_admin
    return TestClient(app)


def test_statement_upload_and_review_are_super_admin_only_and_ledger_read_only(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    admin = User(username="statement-platform-admin", password="x", role="super_admin", status=1)
    ordinary = User(username="statement-ordinary", password="x", role="user", status=1)
    db.add_all([admin, ordinary])
    db.flush()
    order = BillingOrder(
        order_no="pr-order", user_id=ordinary.id, enterprise_id=None, product="personal_credits", amount_fen=400,
        quantity=1, channel="wechat", status="created", paid_at=None,
    )
    db.add(order)
    db.commit()
    client = _client(db, admin)
    headers = {"x-test-user": str(admin.id)}
    try:
        denied = client.post(
            "/api/billing/admin/statements/import",
            headers={"x-test-user": str(ordinary.id)},
            data={"channel": "wechat", "bill_date": "2026-11-04", "bill_type": "SUCCESS"},
            files={"file": ("statement.csv", _wechat_csv(), "text/csv")},
        )
        assert denied.status_code == 403

        imported = client.post(
            "/api/billing/admin/statements/import",
            headers=headers,
            data={"channel": "wechat", "bill_date": "2026-11-04", "bill_type": "SUCCESS"},
            files={"file": ("statement.csv", _wechat_csv(), "text/csv")},
        )
        assert imported.status_code == 200
        batch_data = imported.json()["data"]
        batch_id = batch_data["id"]
        assert batch_data["source_origin"] == "manual_upload"
        assert batch_data["received_missing_count"] == 0
        assert batch_data["fee_missing_count"] == 0
        batch_page = client.get("/api/billing/admin/statements?limit=1&offset=0", headers=headers)
        assert batch_page.status_code == 200
        assert batch_page.json()["data"]["total"] == 1
        assert batch_page.json()["data"]["items"][0]["id"] == batch_id
        rows_response = client.get(f"/api/billing/admin/statements/{batch_id}/rows", headers=headers)
        assert rows_response.status_code == 200
        row_data = rows_response.json()["data"]["items"][0]
        assert row_data["discrepancy_code"] == "provider_payment_not_applied"
        assert row_data["review_status"] == "unreviewed"
        assert "sensitive-openid" not in rows_response.text

        unreviewed = client.get(
            f"/api/billing/admin/statements/{batch_id}/rows?review_status=unreviewed", headers=headers,
        )
        assert unreviewed.status_code == 200
        assert unreviewed.json()["data"]["total"] == 1
        deepest_valid_page = client.get(
            f"/api/billing/admin/statements/{batch_id}/rows?offset=100000", headers=headers,
        )
        assert deepest_valid_page.status_code == 200
        beyond_valid_pages = client.get(
            f"/api/billing/admin/statements/{batch_id}/rows?offset=100001", headers=headers,
        )
        assert beyond_valid_pages.status_code == 422

        reviewed = client.post(
            f"/api/billing/admin/statements/{batch_id}/rows/{row_data['id']}/review",
            headers=headers,
            json={"decision": "investigating", "note": "先通过原渠道查单核实"},
        )
        assert reviewed.status_code == 200
        db.refresh(order)
        assert order.status == "created"
        assert db.query(BillingPaymentEvent).count() == 0
        assert db.query(BillingStatementRow).one().review_status == "investigating"

        denied_download = client.post(
            "/api/billing/admin/statements/download",
            headers={"x-test-user": str(ordinary.id)},
            json={"channel": "wechat", "bill_date": "2026-11-05", "bill_type": "ALL"},
        )
        assert denied_download.status_code == 403
        monkeypatch.setattr("app.api.v1.billing.settings.wechat_pay_mchid", "")
        unavailable = client.post(
            "/api/billing/admin/statements/download", headers=headers,
            json={"channel": "wechat", "bill_date": "2026-11-05", "bill_type": "ALL"},
        )
        assert unavailable.status_code == 503
    finally:
        client.close()
        db.close()
        engine.dispose()
