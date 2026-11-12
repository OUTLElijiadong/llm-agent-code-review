"""The super-admin must be able to reach older refunds without offset races."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import billing
from app.core.database import Base, get_db
from app.core.dependencies import require_super_admin
from app.models.billing_ledger import BillingRefund


def test_refund_admin_cursor_survives_new_records_and_keeps_status_scope():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    def add_refund(number, status="approved"):
        db.add(BillingRefund(order_no=f"order-{number}", refund_no=f"refund-{number}", amount_fen=100,
                             reason="申请", status=status, requested_by=1))
        db.commit()

    for index in range(101):
        add_refund(index)
    add_refund(101, "unknown")
    app = FastAPI()
    app.include_router(billing.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_super_admin] = lambda: object()
    with TestClient(app) as client:
        first = client.get("/api/billing/admin/refunds", params={"status": "approved", "limit": 100})
        assert first.status_code == 200
        rows = first.json()["data"]
        assert len(rows) == 100
        assert len({row["id"] for row in rows}) == 100
        add_refund(102)
        next_page = client.get("/api/billing/admin/refunds", params={
            "status": "approved", "limit": 100, "before_id": rows[-1]["id"],
        }).json()["data"]
        assert [row["refund_no"] for row in next_page] == ["refund-0"]
        assert client.get("/api/billing/admin/refunds", params={"before_id": 0}).status_code == 422
    db.close()
    engine.dispose()
