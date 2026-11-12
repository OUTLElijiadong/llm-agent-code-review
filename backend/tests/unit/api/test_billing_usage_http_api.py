"""The billing usage endpoint always scopes rows to the authenticated account."""

from datetime import datetime, timezone

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.billing import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.models.billing_ledger import BillingReservation
from app.models.user import User


def test_personal_billing_usage_ignores_caller_supplied_user_id():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    first = User(username="billing-usage-api-1", password="x", role="user", status=1)
    second = User(username="billing-usage-api-2", password="x", role="user", status=1)
    db.add_all([first, second])
    db.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add_all(
        [
            BillingReservation(
                request_key="api-usage-first",
                user_id=first.id,
                usage_kind="member",
                model_name="deepseek-flash",
                requested_milli=100,
                status="settled",
                usage_state="reported",
                input_tokens=10,
                output_tokens=5,
                actual_milli=30,
                server_tool_search_calls=1,
                server_tool_fetch_calls=0,
                created_at=now,
            ),
            BillingReservation(
                request_key="api-usage-second",
                user_id=second.id,
                usage_kind="member",
                model_name="deepseek-flash",
                requested_milli=100,
                status="settled",
                usage_state="reported",
                input_tokens=900,
                output_tokens=500,
                actual_milli=3000,
                server_tool_search_calls=9,
                server_tool_fetch_calls=8,
                created_at=now,
            ),
        ]
    )
    db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(AppError)
    async def app_error_handler(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.dependency_overrides[get_db] = lambda: db

    def current_user(x_test_user: int = Header()):
        return db.get(User, x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/billing/usage?days=1&user_id={second.id}", headers={"x-test-user": str(first.id)}
            )
            assert response.status_code == 200
            item = response.json()["data"]["items"][0]
            assert (item["input_tokens"], item["output_tokens"], item["charged_milli"]) == (10, 5, 30)
            assert item["reported_calls"] == 1
            assert (item["server_tool_search_calls"], item["server_tool_fetch_calls"],
                    item["server_tool_usage_reported_calls"]) == (1, 0, 1)
    finally:
        db.close()
        engine.dispose()
