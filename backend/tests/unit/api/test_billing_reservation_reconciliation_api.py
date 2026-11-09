from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.billing import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user, require_super_admin
from app.models.billing_ledger import BillingReconciliationReviewerGrant
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services.billing_ledger_service import (
    BillingProviderIdentity,
    get_or_create_wallet,
    grant,
    mark_tool_attempt_started,
    mark_unknown,
    reserve,
    reserve_tool,
)
from app.services.billing_rate_catalog_service import approve_draft, baseline_items, create_draft, seed_test_baseline

PROVIDER = BillingProviderIdentity(
    identity_version="deepseek-official-v1", provider_name="deepseek",
    endpoint_host="api.deepseek.com", credential_source="system",
)


def _client(db, admins):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    # Existing tests retain their mocked super-admin dependency for event-view
    # contracts; the decision path uses real per-user reviewer authorization.
    admins[1].username = "admin"
    admins[2].role = "admin"
    db.add_all([
        Role(id=1, name="super", code="super_admin", status="active"),
        Role(id=2, name="admin", code="admin", status="active"),
        UserRole(user_id=1, role_id=1), UserRole(user_id=2, role_id=2),
        BillingReconciliationReviewerGrant(user_id=2, active=1, granted_by=1),
    ])
    db.commit()

    def get_test_db():
        yield db

    def test_super_admin(x_test_user: int = Header()):
        if x_test_user not in admins:
            raise HTTPException(status_code=403, detail="仅超级管理员")
        return admins[x_test_user]

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[require_super_admin] = test_super_admin
    app.dependency_overrides[get_current_user] = test_super_admin
    return TestClient(app)


def test_reservation_reconciliation_api_is_super_admin_only_and_dual_approved():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    admins = {
        1: User(id=1, username="billing-admin-one", password="x", role="super_admin", status=1),
        2: User(id=2, username="billing-admin-two", password="x", role="super_admin", status=1),
    }
    ordinary = User(id=3, username="billing-ordinary", password="x", role="user", status=1)
    db.add_all([*admins.values(), ordinary])
    db.flush()
    seed_test_baseline(db)
    wallet = get_or_create_wallet(db, "user", ordinary.id)
    grant(db, wallet_id=wallet.id, grant_key="api-grant", kind="purchase", amount_milli=100_000)
    reservation = reserve(
        db, request_key="api-internal-key-no-chat-content", model_name="deepseek-flash", user_id=ordinary.id,
        max_milli=80_000, personal_wallet_id=wallet.id, provider_identity=PROVIDER,
        now=datetime(2026, 11, 6),
    )
    mark_unknown(db, reservation.request_key, server_tool_search_calls=0, server_tool_fetch_calls=0)
    db.commit()
    client = _client(db, admins)
    try:
        denied = client.get("/api/billing/admin/reservations/reconciliation", headers={"x-test-user": "3"})
        assert denied.status_code == 403
        denied_history = client.get(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/events",
            headers={"x-test-user": "3"},
        )
        assert denied_history.status_code == 403
        denied_proposal = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/proposals",
            headers={"x-test-user": "3"}, json={"resolution": "settle"},
        )
        assert denied_proposal.status_code == 403
        denied_decision = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/decision",
            headers={"x-test-user": "3"}, json={"decision": "approve", "note": "无权"},
        )
        assert denied_decision.status_code == 403

        headers1 = {"x-test-user": "1"}
        headers2 = {"x-test-user": "2"}
        listing = client.get("/api/billing/admin/reservations/reconciliation", headers=headers1)
        assert listing.status_code == 200
        row = listing.json()["data"]["items"][0]
        assert row["id"] == reservation.id
        assert row["status"] == "unknown"
        assert "api-internal-key" not in listing.text
        assert "prompt" not in listing.text.lower()

        no_proposal = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/decision",
            headers=headers1, json={"decision": "approve", "note": "不得审批不存在的提案"},
        )
        assert no_proposal.status_code == 409
        initial_history = client.get(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/events", headers=headers2,
        ).json()["data"]["items"]
        assert len(initial_history) == 1
        assert initial_history[0]["decision"] == "blocked"
        assert initial_history[0]["resolution"] is None
        assert initial_history[0]["related_event_id"] is None
        assert initial_history[0]["evidence_reference"] is None

        proposal = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/proposals",
            headers=headers1,
            json={
                "resolution": "settle", "provider_request_id": "resp-verified-123",
                "input_tokens": 10_000, "output_tokens": 5_000,
                "evidence_reference": "provider-console-incident-123",
                "evidence_sha256": "b" * 64, "note": "对照供应商控制台 usage 记录",
            },
        )
        assert proposal.status_code == 200
        assert proposal.json()["data"]["decision"] == "proposed"

        self_approval = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/decision",
            headers=headers1, json={"decision": "approve", "note": "自批不得成功"},
        )
        assert self_approval.status_code == 409
        approved = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/decision",
            headers=headers2, json={"decision": "approve", "note": "第二名管理员复核通过"},
        )
        assert approved.status_code == 200
        assert approved.json()["data"]["reservation"]["status"] == "settled"
        assert approved.json()["data"]["event"]["actor_id"] == 2
        history = client.get(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/events", headers=headers2,
        )
        assert history.status_code == 200
        history_events = history.json()["data"]["items"]
        assert [item["decision"] for item in history_events] == [
            "blocked", "proposed", "blocked", "approved",
        ]
        self_approval_block = history_events[2]
        assert self_approval_block["resolution"] == "settle"
        assert self_approval_block["provider_request_id"] is None
        assert self_approval_block["input_tokens"] is None
        assert self_approval_block["output_tokens"] is None
        assert self_approval_block["actual_milli"] is None
        assert self_approval_block["evidence_reference"] is None
        assert self_approval_block["evidence_sha256"] is None
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_tool_reconciliation_api_filters_billable_kind_and_requires_count_and_dual_approval():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    admins = {
        1: User(id=1, username="tool-admin-one", password="x", role="super_admin", status=1),
        2: User(id=2, username="tool-admin-two", password="x", role="super_admin", status=1),
    }
    employee = User(id=3, username="tool-employee", password="x", role="user", status=1)
    db.add_all([*admins.values(), employee])
    db.flush()
    seed_test_baseline(db)
    # The test catalog is already active while its independently verified
    # sources remain fresh, regardless of when the suite is run.
    now = datetime.now(timezone.utc) - timedelta(hours=3)
    checked_at = now - timedelta(minutes=30)
    draft = create_draft(
        db, actor_id=10, effective_at=now + timedelta(hours=2),
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        source_checked_at=checked_at,
        model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        model_cny_source_checked_at=checked_at,
        fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
        fx_source_checked_at=checked_at, fx_cny_per_usd_scaled=67_168,
        change_note="API 测试启用 Search/Fetch 费率。", items=baseline_items(), now=now,
        tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
        tool_cny_source_checked_at=checked_at,
        tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        tool_usd_source_checked_at=checked_at,
        tool_search_cost_cny_micros_per_success=5_038,
        tool_search_price_milli_per_success=1_800,
    )
    approve_draft(db, catalog_id=draft.id, actor_id=11, now=now)
    wallet = get_or_create_wallet(db, "enterprise", 77)
    grant(db, wallet_id=wallet.id, grant_key="tool-api-grant", kind="purchase", amount_milli=5_000)
    reservation = reserve_tool(
        db, request_key="search-api-reservation", tool_name="search", user_id=employee.id,
        enterprise_id=77, enterprise_wallet_id=wallet.id, group_id=8,
        provider_identity=PROVIDER, now=draft.effective_at,
    )
    mark_tool_attempt_started(
        db, reservation.request_key, now=draft.effective_at.replace(tzinfo=None) + timedelta(seconds=1),
    )
    db.commit()
    mark_unknown(db, reservation.request_key)
    db.commit()

    client = _client(db, admins)
    headers_one = {"x-test-user": "1"}
    headers_two = {"x-test-user": "2"}
    try:
        listing = client.get(
            "/api/billing/admin/reservations/reconciliation?status=unknown&billable_kind=search",
            headers=headers_one,
        )
        assert listing.status_code == 200
        item = listing.json()["data"]["items"][0]
        assert item["billable_kind"] == "search"
        assert item["tool_name"] == "search"
        assert item["tool_price_milli_per_success"] == 1_800
        assert item["can_settle"] is True
        assert item["can_release_not_sent"] is False
        assert "search-api-reservation" not in listing.text

        proposal_path = f"/api/billing/admin/reservations/{reservation.id}/reconciliation/proposals"
        body = {
            "resolution": "settle", "provider_request_id": "deepseek-search-request-77",
            "evidence_reference": "provider-statement-row-77", "evidence_sha256": "f" * 64,
            "note": "从供应商账单核实一次成功请求",
        }
        missing_count = client.post(proposal_path, headers=headers_one, json=body)
        assert missing_count.status_code == 409
        assert "0/1 成功次数" in missing_count.json()["detail"]

        proposed = client.post(proposal_path, headers=headers_one,
                               json={**body, "tool_successful_calls": 1})
        assert proposed.status_code == 200
        assert proposed.json()["data"]["tool_successful_calls"] == 1

        own_approval = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/decision",
            headers=headers_one, json={"decision": "approve", "note": "本人不得自批"},
        )
        assert own_approval.status_code == 409
        approved = client.post(
            f"/api/billing/admin/reservations/{reservation.id}/reconciliation/decision",
            headers=headers_two, json={"decision": "approve", "note": "独立核验供应商调用记录"},
        )
        assert approved.status_code == 200
        result = approved.json()["data"]["reservation"]
        assert (result["status"], result["tool_successful_calls"], result["actual_milli"]) == (
            "settled", 1, 1_800,
        )
        db.refresh(reservation)
        assert reservation.input_tokens is None and reservation.output_tokens is None
        assert "input_tokens" not in result and "output_tokens" not in result
    finally:
        client.close()
        db.close()
        engine.dispose()
