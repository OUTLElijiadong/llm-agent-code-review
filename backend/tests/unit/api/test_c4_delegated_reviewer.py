"""C4 delegation uses the real authentication and role dependency chain."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.billing import router
from app.core.database import Base, get_db
from app.core.exceptions import AppError
from app.core.security import create_access_token
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services.billing_ledger_service import (
    BillingProviderIdentity,
    get_or_create_wallet,
    grant,
    mark_unknown,
    reserve,
)
from app.services.billing_rate_catalog_service import seed_test_baseline


@pytest.fixture
def system():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    roles = [Role(id=i, name=code, code=code, status="active")
             for i, code in [(1, "super_admin"), (2, "admin"), (3, "user")]]
    users = [User(id=i, username=name, password="x", role=role, status=1, token_version=0)
             for i, name, role in [(1, "admin", "super_admin"), (2, "finance", "admin"),
                                   (3, "other-admin", "admin"), (4, "employee", "user")]]
    role_ids = {"super_admin": 1, "admin": 2, "user": 3}
    db.add_all([*roles, *users, *(UserRole(user_id=user.id, role_id=role_ids[user.role]) for user in users)])
    db.flush()
    seed_test_baseline(db)
    wallet = get_or_create_wallet(db, "user", 4)
    grant(db, wallet_id=wallet.id, grant_key="c4-grant-probe", kind="purchase", amount_milli=100_000)
    provider = BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")
    reservation = reserve(db, request_key="c4-auth-probe", model_name="deepseek-flash", user_id=4,
                          max_milli=80_000, personal_wallet_id=wallet.id, provider_identity=provider,
                          now=datetime(2026, 11, 6))
    mark_unknown(db, reservation.request_key, server_tool_search_calls=0, server_tool_fetch_calls=0)
    db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.exception_handler(AppError)
    async def error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def session():
        yield db

    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        yield client, db, users, reservation
    db.close()
    engine.dispose()


def _headers(user):
    return {"Authorization": "Bearer " + create_access_token(user.id, user.role, user.token_version)}


def test_unique_super_admin_can_delegate_only_pending_review_and_cannot_self_approve(system):
    client, db, users, reservation = system
    owner, reviewer, other_admin, member = users
    prefix = f"/api/billing/admin/reservations/{reservation.id}/reconciliation"
    proposal = {"resolution": "settle", "provider_request_id": "probe", "input_tokens": 100,
                "output_tokens": 100, "evidence_reference": "probe", "evidence_sha256": "b" * 64,
                "note": "凭证对照"}
    decision = {"decision": "approve", "note": "独立复核"}
    assert client.post(prefix + "/proposals", headers=_headers(owner), json=proposal).status_code == 200
    assert client.post(prefix + "/decision", headers=_headers(owner), json=decision).status_code == 409
    assert client.post(prefix + "/decision", headers=_headers(reviewer), json=decision).status_code == 403
    manage = f"/api/billing/admin/reconciliation/reviewers/{reviewer.id}"
    assert client.put(manage, headers=_headers(other_admin), json={"active": True, "reason": "委任"}).status_code == 403
    assert client.put(manage, headers=_headers(owner), json={"active": True, "reason": "委任"}).status_code == 200
    grant_list = client.get("/api/billing/admin/reconciliation/reviewers", headers=_headers(owner))
    assert grant_list.status_code == 200
    assert grant_list.json()["data"]["items"][0]["user_id"] == reviewer.id
    assert grant_list.json()["data"]["items"][0]["active"] is True
    assert grant_list.json()["data"]["items"][0]["eligible"] is True
    assert grant_list.json()["data"]["items"][0]["username"] == reviewer.username
    assert grant_list.json()["data"]["items"][0]["nickname"] == reviewer.nickname
    assert client.get("/api/billing/admin/reconciliation/reviewers", headers=_headers(reviewer)).status_code == 403
    assert client.get("/api/billing/admin/reconciliation/reviewers", headers=_headers(other_admin)).status_code == 403
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(member)).status_code == 403
    rows = client.get("/api/billing/reconciliation/reviews", headers=_headers(reviewer))
    assert rows.status_code == 200 and len(rows.json()["data"]["items"]) == 1
    assert "c4-auth-probe" not in rows.text and "prompt" not in rows.text.lower()
    detail = client.get(f"/api/billing/reconciliation/reviews/{reservation.id}", headers=_headers(reviewer))
    assert detail.status_code == 200 and detail.json()["data"]["reservation"]["id"] == reservation.id
    assert client.get("/api/billing/reconciliation/reviews/999999", headers=_headers(reviewer)).status_code == 404
    assert client.get("/api/billing/admin/reservations/reconciliation", headers=_headers(reviewer)).status_code == 403
    assert client.post(prefix + "/proposals", headers=_headers(reviewer), json=proposal).status_code == 403
    approved = client.post(prefix + "/decision", headers=_headers(reviewer), json=decision)
    assert approved.status_code == 200
    assert set(approved.json()["data"]["reservation"]) == {"id", "status", "actual_milli", "tool_successful_calls"}
    assert "request_key_suffix" not in approved.text and "c4-auth-probe" not in approved.text
    assert client.post(prefix + "/decision", headers=_headers(reviewer), json=decision).status_code == 409
    db.refresh(reservation)
    assert reservation.status == "settled"


def test_revoke_disable_and_role_downgrade_do_not_restore_old_grant(system):
    from app.services import user_service

    client, db, users, reservation = system
    owner, reviewer, _, _ = users
    headers = _headers(owner)
    path = f"/api/billing/admin/reconciliation/reviewers/{reviewer.id}"
    assert client.put(path, headers=headers, json={"active": True, "reason": "委任"}).status_code == 200
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(reviewer)).status_code == 200
    # Session changes alone must not revoke a durable grant.
    reviewer.token_version += 1
    db.commit()
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(reviewer)).status_code == 200
    user_service.toggle_status(db, reviewer.id, 0, owner)
    user_service.toggle_status(db, reviewer.id, 1, owner)
    db.refresh(reviewer)
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(reviewer)).status_code == 403
    assert client.put(path, headers=headers, json={"active": True, "reason": "重新委任"}).status_code == 200
    user_service.set_role(db, reviewer.id, "user", owner.id)
    user_service.set_role(db, reviewer.id, "admin", owner.id)
    db.refresh(reviewer)
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(reviewer)).status_code == 403
    db.refresh(reservation)
    assert reservation.status == "unknown"


def test_rate_catalog_actual_auth_cannot_approve_or_cancel_with_two_real_accounts(system):
    from app.services import billing_rate_catalog_service as rates

    client, db, users, _reservation = system
    owner, reviewer, _other_admin, _member = users
    now = datetime.now(timezone.utc)
    payload = {
        "effective_at": (now + timedelta(hours=4)).isoformat(),
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing/",
        "source_checked_at": now.isoformat(),
        "model_cny_source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "model_cny_source_checked_at": now.isoformat(),
        "fx_source_url": "https://www.boc.cn/sourcedb/whpj/index.html",
        "fx_source_checked_at": now.isoformat(), "fx_cny_per_usd_scaled": 67_168,
        "change_note": "核验两个人的费率审批真实权限", "items": rates.baseline_items(),
    }
    grant_path = f"/api/billing/admin/reconciliation/reviewers/{reviewer.id}"
    assert client.put(grant_path, headers=_headers(owner),
                      json={"active": True, "reason": "仅 C4 委任"}).status_code == 200
    created = client.post("/api/billing/admin/rate-catalogs", headers=_headers(owner), json=payload)
    assert created.status_code == 200
    catalog_id = created.json()["data"]["id"]
    path = f"/api/billing/admin/rate-catalogs/{catalog_id}"
    assert client.post(path + "/approve", headers=_headers(owner)).status_code == 409
    assert client.post(path + "/approve", headers=_headers(reviewer)).status_code == 403
    # Construct the precondition at the service layer solely to probe cancellation.
    rates.approve_draft(db, catalog_id=catalog_id, actor_id=reviewer.id, now=now)
    db.commit()
    assert client.post(path + "/cancel", headers=_headers(owner), json={"note": "取消验证"}).status_code == 409
    assert client.post(path + "/cancel", headers=_headers(reviewer), json={"note": "取消验证"}).status_code == 403


def test_explicit_revoke_immediately_denies_reviewer_with_same_valid_token(system):
    client, db, users, reservation = system
    owner, reviewer, other_admin, member = users
    path = f"/api/billing/admin/reconciliation/reviewers/{reviewer.id}"
    owner_headers, reviewer_headers = _headers(owner), _headers(reviewer)
    assert client.put(path, headers=owner_headers, json={"active": True, "reason": "委任"}).status_code == 200
    assert client.get("/api/billing/reconciliation/reviews", headers=reviewer_headers).status_code == 200
    assert client.put(path, headers=owner_headers, json={"active": False, "reason": "撤销"}).status_code == 200
    assert client.get("/api/billing/reconciliation/reviews", headers=reviewer_headers).status_code == 403
    decision = f"/api/billing/admin/reservations/{reservation.id}/reconciliation/decision"
    assert client.post(decision, headers=reviewer_headers,
                       json={"decision": "approve", "note": "拒绝"}).status_code == 403
    assert client.put(f"/api/billing/admin/reconciliation/reviewers/{member.id}", headers=owner_headers,
                      json={"active": True, "reason": "越权"}).status_code == 403
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(other_admin)).status_code == 403
    db.refresh(reservation)
    assert reservation.status == "unknown"


def test_grant_list_exposes_current_role_inconsistency_without_claiming_eligibility(system):
    client, db, users, _ = system
    owner, reviewer, _, _ = users
    path = f"/api/billing/admin/reconciliation/reviewers/{reviewer.id}"
    assert client.put(path, headers=_headers(owner), json={"active": True, "reason": "委任"}).status_code == 200
    # Simulate historic inconsistency without invoking the normal role mutation path.
    reviewer.role = "user"
    db.commit()
    data = client.get("/api/billing/admin/reconciliation/reviewers", headers=_headers(owner)).json()["data"]
    assert data["items"][0]["active"] is True
    assert data["items"][0]["eligible"] is False
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(reviewer)).status_code == 403


def test_grant_list_displays_missing_user_safely_only_to_super_admin(system):
    from app.models.billing_ledger import BillingReconciliationReviewerGrant

    client, db, users, _ = system
    owner, reviewer, _, _ = users
    db.add(BillingReconciliationReviewerGrant(user_id=999999, active=1, granted_by=1))
    db.commit()
    path = "/api/billing/admin/reconciliation/reviewers"
    data = client.get(path, headers=_headers(owner)).json()["data"]["items"]
    assert data[0]["user_id"] == 999999
    assert data[0]["username"] is None and data[0]["nickname"] is None and data[0]["eligible"] is False
    assert client.get(path, headers=_headers(reviewer)).status_code == 403


def test_independent_rate_review_and_two_person_cancellation(system):
    from app.services import billing_rate_catalog_service as rates

    client, db, users, _reservation = system
    owner, reviewer, other_admin, member = users
    now = datetime.now(timezone.utc)
    payload = {
        "effective_at": (now + timedelta(hours=4)).isoformat(),
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing/",
        "source_checked_at": now.isoformat(),
        "model_cny_source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "model_cny_source_checked_at": now.isoformat(),
        "fx_source_url": "https://www.boc.cn/sourcedb/whpj/index.html",
        "fx_source_checked_at": now.isoformat(), "fx_cny_per_usd_scaled": 67_168,
        "change_note": "独立双人复核", "items": rates.baseline_items(),
    }
    created = client.post("/api/billing/admin/rate-catalogs", headers=_headers(owner), json=payload)
    assert created.status_code == 200
    catalog_id = created.json()["data"]["id"]
    path = f"/api/billing/rate-reviews/{catalog_id}"
    c4_grant = f"/api/billing/admin/reconciliation/reviewers/{reviewer.id}"
    assert client.put(c4_grant, headers=_headers(owner), json={"active": True, "reason": "仅 C4"}).status_code == 200
    assert client.get("/api/billing/rate-reviews", headers=_headers(reviewer)).status_code == 403
    grant = f"/api/billing/admin/rate-reviewers/{reviewer.id}"
    assert client.put(grant, headers=_headers(other_admin), json={"active": True, "reason": "授权"}).status_code == 403
    assert client.put(grant, headers=_headers(owner), json={"active": True, "reason": "费率复核"}).status_code == 200
    assert client.get("/api/billing/rate-reviews", headers=_headers(member)).status_code == 403
    assert client.get("/api/billing/rate-reviews", headers=_headers(reviewer)).status_code == 200
    assert client.post(path + "/approve", headers=_headers(owner)).status_code == 409
    assert client.post(path + "/approve", headers=_headers(reviewer)).status_code == 200
    assert client.post(f"/api/billing/admin/rate-catalogs/{catalog_id}/cancel", headers=_headers(owner),
                       json={"note": "不可单人直接取消"}).status_code == 409
    proposal = client.post(f"/api/billing/rate-catalogs/{catalog_id}/cancel-requests", headers=_headers(owner),
                           json={"note": "发现需取消的异常"})
    assert proposal.status_code == 200
    request_id = proposal.json()["data"]["id"]
    assert client.get("/api/billing/rate-cancel-requests", headers=_headers(member)).status_code == 403
    assert client.get("/api/billing/rate-cancel-requests", headers=_headers(other_admin)).status_code == 403
    pending = client.get("/api/billing/rate-cancel-requests", headers=_headers(reviewer))
    assert pending.status_code == 200
    item = pending.json()["data"]["items"][0]
    assert item["proposal_note"] == "发现需取消的异常"
    assert item["catalog"]["id"] == catalog_id
    assert item["catalog"]["source_url"] == payload["source_url"]
    assert item["catalog"]["items"] == payload["items"]
    assert "events" not in item["catalog"]
    decision = f"/api/billing/rate-cancel-requests/{request_id}/decision"
    assert client.post(decision, headers=_headers(owner),
                       json={"decision": "approve", "note": "自审"}).status_code == 409
    assert client.post(decision, headers=_headers(other_admin),
                       json={"decision": "approve", "note": "未授权"}).status_code == 403
    result = client.post(decision, headers=_headers(reviewer), json={"decision": "approve", "note": "独立核验"})
    assert result.status_code == 200 and result.json()["data"]["status"] == "approved"
    assert rates.active_catalog(db, now=now + timedelta(hours=5)).id != catalog_id


def test_rate_review_grant_is_independent_and_revoked_on_role_or_status_change(system):
    from app.services import user_service

    client, db, users, _reservation = system
    owner, reviewer, other, member = users
    path = f"/api/billing/admin/rate-reviewers/{reviewer.id}"
    root = _headers(owner)
    assert client.put(path, headers=root, json={"active": True, "reason": "财务复核"}).status_code == 200
    grants = client.get("/api/billing/admin/rate-reviewers", headers=root)
    assert grants.status_code == 200
    assert grants.json()["data"]["items"][0]["username"] == reviewer.username
    assert grants.json()["data"]["items"][0]["eligible"] is True
    assert client.get("/api/billing/reconciliation/reviews", headers=_headers(reviewer)).status_code == 403
    assert client.get("/api/billing/admin/rate-reviewers", headers=_headers(reviewer)).status_code == 403
    assert client.put(f"/api/billing/admin/rate-reviewers/{member.id}", headers=root,
                      json={"active": True, "reason": "无平台管理员角色"}).status_code == 403
    user_service.toggle_status(db, reviewer.id, 0, owner)
    user_service.toggle_status(db, reviewer.id, 1, owner)
    db.refresh(reviewer)
    assert client.get("/api/billing/rate-reviews", headers=_headers(reviewer)).status_code == 403
    assert client.put(path, headers=root, json={"active": True, "reason": "重新委任"}).status_code == 200
    user_service.set_role(db, reviewer.id, "user", owner.id)
    user_service.set_role(db, reviewer.id, "admin", owner.id)
    db.refresh(reviewer)
    assert client.get("/api/billing/rate-reviews", headers=_headers(reviewer)).status_code == 403
    assert client.get("/api/billing/rate-reviews", headers=_headers(other)).status_code == 403


def test_rate_cancel_proposal_idempotency_and_expiry_leave_catalog_unchanged(system, monkeypatch):
    from app.services import billing_rate_cancel_service as cancellation
    from app.services import billing_rate_catalog_service as rates

    client, db, users, _reservation = system
    owner, reviewer, _, _ = users
    now = datetime.now(timezone.utc)
    draft = rates.create_draft(
        db, actor_id=owner.id, effective_at=now + timedelta(hours=4),
        source_url="https://api-docs.deepseek.com/quick_start/pricing/", source_checked_at=now,
        model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        model_cny_source_checked_at=now, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
        fx_source_checked_at=now, fx_cny_per_usd_scaled=67_168, change_note="取消测试",
        items=rates.baseline_items(), now=now,
    )
    rates.approve_draft(db, catalog_id=draft.id, actor_id=reviewer.id, now=now)
    db.commit()
    root = _headers(owner)
    path = f"/api/billing/rate-catalogs/{draft.id}/cancel-requests"
    first = client.post(path, headers=root, json={"note": "发现错误"})
    assert first.status_code == 200
    second = client.post(path, headers=root, json={"note": "发现错误"})
    assert second.status_code == 200 and first.json()["data"]["id"] == second.json()["data"]["id"]
    assert client.post(path, headers=root, json={"note": "不同说明"}).status_code == 409
    grant = f"/api/billing/admin/rate-reviewers/{reviewer.id}"
    assert client.put(grant, headers=root, json={"active": True, "reason": "费率复核"}).status_code == 200
    monkeypatch.setattr(cancellation, "_now", lambda: (now + timedelta(hours=5)).replace(tzinfo=None))
    request_id = first.json()["data"]["id"]
    result = client.post(f"/api/billing/rate-cancel-requests/{request_id}/decision", headers=_headers(reviewer),
                         json={"decision": "approve", "note": "尝试复核"})
    assert result.status_code == 200 and result.json()["data"]["status"] == "expired"
    db.refresh(draft)
    assert draft.status == "approved"
