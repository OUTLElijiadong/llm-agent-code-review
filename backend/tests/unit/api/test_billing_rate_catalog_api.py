from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy.exc import IntegrityError

from app.api.v1 import billing
from app.api.v1.billing import RateCatalogCreate, RateCatalogItemInput
from app.core.dependencies import require_super_admin
from app.models.user import User
from app.services.billing_rate_catalog_service import baseline_items


def test_rate_catalog_write_routes_require_platform_super_admin():
    protected = {
        ("GET", "/billing/admin/rate-catalogs"),
        ("POST", "/billing/admin/rate-catalogs"),
        ("POST", "/billing/admin/rate-catalogs/{catalog_id}/approve"),
        ("POST", "/billing/admin/rate-catalogs/{catalog_id}/reject"),
        ("POST", "/billing/admin/rate-catalogs/{catalog_id}/cancel"),
    }
    routes = [route for route in billing.router.routes if isinstance(route, APIRoute)]
    for method, path in protected:
        route = next(item for item in routes if item.path == path and method in item.methods)
        assert any(dependency.call is require_super_admin for dependency in route.dependant.dependencies)


def test_rate_catalog_api_creates_computed_digest_and_enforces_two_person_approval(db):
    now = datetime.now(timezone.utc)
    admin = User(id=500, username="pricing-author", password="x", role="admin", status=1)
    reviewer = User(id=501, username="pricing-reviewer", password="x", role="admin", status=1)
    payload = RateCatalogCreate(
        effective_at=now + timedelta(hours=2),
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        source_checked_at=now,
        model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        model_cny_source_checked_at=now,
        fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
        fx_source_checked_at=now,
        fx_cny_per_usd_scaled=67_168,
        change_note="官方价格与汇率核验后的基线复核。",
        items=[RateCatalogItemInput(**item) for item in baseline_items()],
    )
    created = billing.create_rate_catalog(payload, db, admin)
    assert created.data["status"] == "draft"
    assert len(created.data["source_sha256"]) == 64

    with pytest.raises(Exception) as self_approval:
        billing.approve_rate_catalog(created.data["id"], db, admin)
    assert getattr(self_approval.value, "status_code", None) == 409
    approved = billing.approve_rate_catalog(created.data["id"], db, reviewer)
    assert approved.data["status"] == "approved"
    assert approved.data["created_by"] == admin.id
    assert approved.data["approved_by"] == reviewer.id


def test_rate_catalog_api_returns_search_fetch_rate_snapshot_and_margin(db):
    now = datetime.now(timezone.utc)
    admin = User(id=510, username="tool-pricing-author", password="x", role="admin", status=1)
    payload = RateCatalogCreate(
        effective_at=now + timedelta(hours=2),
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        source_checked_at=now,
        model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        model_cny_source_checked_at=now,
        fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
        fx_source_checked_at=now,
        fx_cny_per_usd_scaled=67_168,
        change_note="核对 Search 和 Fetch 官方单次成功请求价格。",
        tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
        tool_cny_source_checked_at=now,
        tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        tool_usd_source_checked_at=now,
        tool_search_cost_cny_micros_per_success=5_038,
        tool_search_price_milli_per_success=1_800,
        tool_fetch_cost_cny_micros_per_success=5_038,
        tool_fetch_price_milli_per_success=1_800,
        items=[RateCatalogItemInput(**item) for item in baseline_items()],
    )

    created = billing.create_rate_catalog(payload, db, admin)

    assert created.data["tool_margin_bps"] == {"search": 3_002, "fetch": 3_002}
    assert created.data["tool_cny_source_url"] == "https://api-docs.deepseek.com/zh-cn/guides/search/"
    assert created.data["tool_usd_source_url"] == "https://api-docs.deepseek.com/quick_start/pricing/"
    assert created.data["tool_search_price_milli_per_success"] == 1_800
    assert created.data["tool_fetch_price_milli_per_success"] == 1_800


def test_rate_catalog_api_turns_database_duplicate_conflict_into_refreshable_409(monkeypatch):
    class ConflictingSession:
        rolled_back = False

        def commit(self):
            raise IntegrityError("INSERT", {}, RuntimeError("duplicate version"))

        def rollback(self):
            self.rolled_back = True

    session = ConflictingSession()
    admin = User(id=502, username="pricing-race", password="x", role="admin", status=1)
    now = datetime.now(timezone.utc)
    payload = RateCatalogCreate(
        effective_at=now + timedelta(hours=2),
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        source_checked_at=now,
        model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        model_cny_source_checked_at=now,
        fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
        fx_source_checked_at=now,
        fx_cny_per_usd_scaled=67_168,
        change_note="并发冲突响应验证。",
        items=[RateCatalogItemInput(**item) for item in baseline_items()],
    )
    monkeypatch.setattr(billing.billing_rate_catalog_service, "create_draft", lambda *args, **kwargs: object())
    monkeypatch.setattr(billing.billing_rate_catalog_service, "catalog_view", lambda *args, **kwargs: {})

    with pytest.raises(HTTPException) as conflict:
        billing.create_rate_catalog(payload, session, admin)

    assert conflict.value.status_code == 409
    assert session.rolled_back
