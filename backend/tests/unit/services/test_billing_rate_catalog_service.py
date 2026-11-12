from datetime import datetime, timedelta, timezone

import pytest

from app.models.billing_ledger import (
    BillingRateCatalog,
    BillingRateCatalogEvent,
    BillingRateCatalogItem,
    BillingReservation,
)
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog_service
from app.services.billing_ledger_service import BillingProviderIdentity, get_or_create_wallet, grant, reserve

UTC = timezone.utc
NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
SOURCE_CHECKED = datetime(2026, 11, 7, 5, 30, tzinfo=UTC)


def _draft(db, *, now=NOW, actor_id=10, effective_at=None, items=None, **overrides):
    values = {
        "actor_id": actor_id,
        "effective_at": effective_at or now + timedelta(hours=2),
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing/",
        "source_checked_at": SOURCE_CHECKED,
        "model_cny_source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "model_cny_source_checked_at": SOURCE_CHECKED,
        "fx_source_url": "https://www.boc.cn/sourcedb/whpj/index.html",
        "fx_source_checked_at": SOURCE_CHECKED,
        "fx_cny_per_usd_scaled": 67_168,
        "change_note": "记录 DeepSeek 官方报价变更并校准平台费率。",
        "items": items or catalog_service.baseline_items(),
        "now": now,
    }
    values.update(overrides)
    return catalog_service.create_draft(db, **values)


def test_catalog_requires_full_official_recent_priced_catalog(db):
    items = catalog_service.baseline_items()
    with pytest.raises(catalog_service.RateCatalogError, match="必须且只能覆盖"):
        _draft(db, items=items[:-1])
    with pytest.raises(catalog_service.RateCatalogError, match="DeepSeek 官方"):
        _draft(db, source_url="https://deepseek.com.evil.example/pricing")
    with pytest.raises(catalog_service.RateCatalogError, match="中国银行外汇牌价"):
        _draft(db, fx_source_url="https://www.boc.cn.evil.example/sourcedb/whpj/index.html")
    with pytest.raises(catalog_service.RateCatalogError, match="中国银行外汇牌价"):
        _draft(db, fx_source_url="https://www.boc.cn/sourcedb/whpj/?token=anything")
    with pytest.raises(catalog_service.RateCatalogError, match="最近 30 天"):
        _draft(db, source_checked_at=NOW - timedelta(days=31))
    with pytest.raises(catalog_service.RateCatalogError, match="汇率来源"):
        _draft(db, fx_source_checked_at=NOW - timedelta(days=31))


def test_reviewed_supplier_floors_match_latest_official_snapshot():
    items = catalog_service.baseline_items()
    assert items
    assert {item["provider_input_usd_micros_per_million"] for item in items} == {300_000}
    assert {item["provider_output_usd_micros_per_million"] for item in items} == {1_200_000}
    assert {item["provider_input_cny_micros_per_million"] for item in items} == {2_000_000}
    assert {item["provider_output_cny_micros_per_million"] for item in items} == {8_000_000}
    assert catalog_service.baseline_values()["fx_cny_per_usd_scaled"] == 67_188
    tool_cost = catalog_service._minimum_tool_cost_cny_micros(fx_cny_per_usd_scaled=67_188)
    assert tool_cost == 5_040
    assert catalog_service._tool_margin_bps(
        price_milli=1_800, cost_cny_micros=tool_cost,
        price_fen=400, credits=1_000,
    ) == 3_000


def test_provider_and_fx_source_facts_both_change_the_immutable_digest(db):
    original = _draft(db)
    changed_fx_source = _draft(db, fx_source_url="https://www.boc.cn/sourcedb/whpj/")
    assert original.source_sha256 != changed_fx_source.source_sha256
    assert original.version_key != changed_fx_source.version_key


def test_unprofitable_supplier_snapshot_cannot_be_submitted(db):
    items = catalog_service.baseline_items()
    items[0]["provider_output_usd_micros_per_million"] = 3_000_000
    with pytest.raises(catalog_service.RateCatalogError, match="低于 30%"):
        _draft(db, items=items)
    assert db.query(BillingRateCatalog).filter_by(status="draft").count() == 0


def test_legacy_catalog_below_current_supplier_floor_fails_closed(db):
    draft = _draft(db)
    approved = catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    item = db.query(BillingRateCatalogItem).filter_by(
        catalog_id=approved.id, model_name="deepseek-flash",
    ).one()
    # Simulate a historical snapshot just below the current official price floor.
    item.provider_output_usd_micros_per_million = 1_199_999
    item.provider_output_cny_micros_per_million = 7_999_999
    db.flush()

    after_activation = NOW + timedelta(hours=3)
    with pytest.raises(catalog_service.RateCatalogError, match="保守下限"):
        catalog_service.rate_snapshot(db, "deepseek-flash", now=after_activation)
    assert catalog_service.public_pricing(db, now=after_activation)["model_pricing_ready"] is False


def test_historical_catalog_without_cny_source_fails_closed(db):
    draft = _draft(db)
    approved = catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    approved.model_cny_source_url = None
    approved.model_cny_source_checked_at = None
    item = db.query(BillingRateCatalogItem).filter_by(
        catalog_id=approved.id, model_name="deepseek-flash",
    ).one()
    item.provider_input_cny_micros_per_million = None
    item.provider_output_cny_micros_per_million = None
    db.flush()

    with pytest.raises(catalog_service.RateCatalogError, match="来源未完整核验"):
        catalog_service.rate_snapshot(db, "deepseek-flash", now=NOW + timedelta(hours=3))


def test_model_margin_uses_higher_of_official_cny_and_converted_usd(db):
    # Flash peak: official CNY input is ¥2 / million; USD $0.30 at 5 CNY/USD
    # is ¥1.5. 0.6 milli-credit/token earns ¥2.4 per million at ¥4/1,000 credits.
    # USD-only margin is 37.5%, but CNY margin is only 16.67%.
    items = catalog_service.baseline_items()
    for item in items:
        item["input_rate_numerator"] = 3
        item["input_rate_denominator"] = 5
    with pytest.raises(catalog_service.RateCatalogError, match="30%"):
        _draft(db, items=items, fx_cny_per_usd_scaled=50_000)


def test_new_model_reservation_refuses_expired_price_and_fx_sources(db):
    draft = _draft(db)
    catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    db.flush()
    stale_at = SOURCE_CHECKED + timedelta(days=31)
    with pytest.raises(catalog_service.RateCatalogError, match="来源.*过期"):
        catalog_service.rate_snapshot(db, "deepseek-flash", now=stale_at)
    public = catalog_service.public_pricing(db, now=stale_at)
    assert public["model_pricing_ready"] is False
    assert "deepseek-flash" in public["model_milli_per_token"]


def test_model_cny_source_required_official_and_part_of_digest(db):
    with pytest.raises((TypeError, catalog_service.RateCatalogError)):
        _draft(db, model_cny_source_url=None)
    with pytest.raises(catalog_service.RateCatalogError, match="官方价格页面"):
        _draft(db, model_cny_source_url="https://api-docs.deepseek.com/quick_start/pricing/")
    with pytest.raises(catalog_service.RateCatalogError, match="30 天"):
        _draft(db, model_cny_source_checked_at=NOW - timedelta(days=31))
    first = _draft(db)
    second = _draft(db, model_cny_source_checked_at=SOURCE_CHECKED - timedelta(minutes=1))
    assert first.source_sha256 != second.source_sha256 and first.version_key != second.version_key


def test_existing_reservation_settles_its_snapshot_after_sources_expire(db):
    wallet = get_or_create_wallet(db, "user", 874)
    grant(db, wallet_id=wallet.id, grant_key="stale-model-settle", kind="purchase", amount_milli=100_000)
    identity = BillingProviderIdentity("deepseek-official-v1", "deepseek", "api.deepseek.com", "system")
    reservation = reserve(db, request_key="stale-model-settle", model_name="deepseek-flash", user_id=874,
                          max_milli=8_000, personal_wallet_id=wallet.id, provider_identity=identity, now=NOW)
    version = reservation.pricing_version
    stale_at = NOW + timedelta(days=40)
    with pytest.raises(catalog_service.RateCatalogError, match="来源.*过期"):
        catalog_service.rate_snapshot(db, "deepseek-flash", now=stale_at)
    actual = ledger.quote_reservation_milli(
        db, request_key=reservation.request_key, input_tokens=100, output_tokens=100,
    )
    settled = ledger.settle(db, request_key=reservation.request_key, actual_milli=actual,
                            input_tokens=100, output_tokens=100, now=stale_at)
    assert (settled.status, settled.pricing_version, settled.actual_milli) == ("settled", version, 500)


def test_model_sources_allow_exactly_30_days_but_not_one_microsecond_more(db):
    draft = _draft(db)
    catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    db.flush()
    at_limit = SOURCE_CHECKED + timedelta(days=30)
    assert catalog_service.rate_snapshot(db, "deepseek-flash", now=at_limit)[0] == draft.version_key
    with pytest.raises(catalog_service.RateCatalogError, match="来源.*过期"):
        catalog_service.rate_snapshot(db, "deepseek-flash", now=at_limit + timedelta(microseconds=1))


def test_first_catalog_can_be_approved_with_explicit_dual_source(db):
    # Metadata-only SQLite fixture, not the migration's immutable production catalog.
    db.query(BillingRateCatalogEvent).delete()
    db.query(BillingRateCatalogItem).delete()
    db.query(BillingRateCatalog).delete()
    db.flush()
    draft = _draft(db)
    catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    db.flush()
    assert catalog_service.rate_snapshot(db, "deepseek-flash", now=draft.effective_at)[0] == draft.version_key


@pytest.mark.parametrize("value", [None, -1, 0, 1.0, True, 2**63])
def test_model_cny_cost_rejects_invalid_or_out_of_range_values(db, value):
    items = catalog_service.baseline_items()
    items[0]["provider_input_cny_micros_per_million"] = value
    with pytest.raises(catalog_service.RateCatalogError):
        _draft(db, items=items)


def test_search_fetch_rates_require_recent_official_source_and_complete_cost_price_pairs(db):
    common = {
        "tool_search_cost_cny_micros_per_success": 5_038,
        "tool_search_price_milli_per_success": 1_800,
        "tool_cny_source_url": "https://api-docs.deepseek.com/zh-cn/guides/search/",
        "tool_cny_source_checked_at": SOURCE_CHECKED,
        "tool_usd_source_url": "https://api-docs.deepseek.com/quick_start/pricing/",
        "tool_usd_source_checked_at": SOURCE_CHECKED,
    }
    enabled = _draft(db, **common)
    assert enabled.tool_search_cost_cny_micros_per_success == 5_038
    assert enabled.tool_search_price_milli_per_success == 1_800
    assert enabled.tool_fetch_cost_cny_micros_per_success == 0
    assert enabled.tool_fetch_price_milli_per_success == 0

    with pytest.raises(catalog_service.RateCatalogError, match="供应商成本和用户收费必须同时启用"):
        _draft(db, tool_search_cost_cny_micros_per_success=5_038)
    with pytest.raises(catalog_service.RateCatalogError, match="DeepSeek 官方页面"):
        _draft(db, **{
            **common,
            "tool_cny_source_url": "https://deepseek.com.evil.example/search",
        })
    with pytest.raises(catalog_service.RateCatalogError, match="分别提供官方 CNY 与 USD"):
        _draft(db, **{
            **common,
            "tool_usd_source_url": None,
            "tool_usd_source_checked_at": None,
        })
    with pytest.raises(catalog_service.RateCatalogError, match="最近 30 天"):
        _draft(db, **{
            **common,
            "tool_cny_source_checked_at": NOW - timedelta(days=31),
        })
    with pytest.raises(catalog_service.RateCatalogError, match="保守折算下限 5038 微元/次"):
        _draft(db,
            tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
            tool_cny_source_checked_at=SOURCE_CHECKED,
            tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            tool_usd_source_checked_at=SOURCE_CHECKED,
            tool_search_cost_cny_micros_per_success=5_000,
            tool_search_price_milli_per_success=1_800,
        )


def test_search_fetch_margin_is_calculated_from_milli_credit_package_basis(db):
    values = {
        "tool_search_cost_cny_micros_per_success": 5_038,
        "tool_search_price_milli_per_success": 1_800,
        "tool_fetch_cost_cny_micros_per_success": 5_038,
        "tool_fetch_price_milli_per_success": 1_800,
        "tool_cny_source_url": "https://api-docs.deepseek.com/zh-cn/guides/search/",
        "tool_cny_source_checked_at": SOURCE_CHECKED,
        "tool_usd_source_url": "https://api-docs.deepseek.com/quick_start/pricing/",
        "tool_usd_source_checked_at": SOURCE_CHECKED,
    }
    draft = _draft(db, **values)
    view = catalog_service.catalog_view(db, draft)
    assert view["tool_margin_bps"] == {"search": 3_002, "fetch": 3_002}
    assert view["tool_cny_source_url"] == values["tool_cny_source_url"]
    assert view["tool_usd_source_url"] == values["tool_usd_source_url"]
    assert view["tool_supplier_price_diff"]["increases"] == [
        {"tool": "search", "previous": 0, "current": 5_038},
        {"tool": "fetch", "previous": 0, "current": 5_038},
    ]

    with pytest.raises(catalog_service.RateCatalogError, match="Search 直采成本毛利低于 30%"):
        _draft(db, **{
            **values,
            "tool_search_price_milli_per_success": 1_799,
        })


def test_search_fetch_cost_price_source_and_effective_rates_are_versioned_and_public(db):
    base = _draft(db)
    with_tools = _draft(db,
        tool_search_cost_cny_micros_per_success=5_038,
        tool_search_price_milli_per_success=1_800,
        tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
        tool_cny_source_checked_at=SOURCE_CHECKED,
        tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        tool_usd_source_checked_at=SOURCE_CHECKED,
    )
    assert base.source_sha256 != with_tools.source_sha256
    assert base.version_key != with_tools.version_key

    catalog_service.approve_draft(db, catalog_id=with_tools.id, actor_id=11, now=NOW)
    db.flush()
    public = catalog_service.public_pricing(db, now=with_tools.effective_at)
    assert public["tool_milli_credits_per_success"] == {"search": 1_800, "fetch": None}
    assert catalog_service.tool_rate_snapshot(db, "search", now=with_tools.effective_at) == (
        with_tools.version_key, 5_038, 1_800,
    )
    with pytest.raises(catalog_service.RateCatalogError, match="没有已批准的按次费率"):
        catalog_service.tool_rate_snapshot(db, "fetch", now=with_tools.effective_at)


def test_tool_pricing_source_must_still_be_fresh_when_the_catalog_is_approved(db):
    draft = _draft(db,
        effective_at=NOW + timedelta(days=14),
        tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
        tool_cny_source_checked_at=SOURCE_CHECKED - timedelta(days=15),
        tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        tool_usd_source_checked_at=SOURCE_CHECKED - timedelta(days=15),
        tool_search_cost_cny_micros_per_success=5_038,
        tool_search_price_milli_per_success=1_800,
    )

    with pytest.raises(catalog_service.RateCatalogError, match="审批/生效时已超过 30 天"):
        catalog_service.approve_draft(
            db, catalog_id=draft.id, actor_id=11, now=NOW + timedelta(days=16),
        )


def test_search_fetch_sources_must_cover_planned_activation(db):
    with pytest.raises(catalog_service.RateCatalogError, match="覆盖计划生效时间"):
        _draft(db,
            effective_at=NOW + timedelta(days=29),
            tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
            tool_cny_source_checked_at=SOURCE_CHECKED - timedelta(days=2),
            tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            tool_usd_source_checked_at=SOURCE_CHECKED - timedelta(days=2),
            tool_search_cost_cny_micros_per_success=5_038,
            tool_search_price_milli_per_success=1_800,
        )


def test_search_fetch_runtime_fails_closed_for_expired_sources_and_unsafe_margin(db):
    draft = _draft(db,
        tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
        tool_cny_source_checked_at=SOURCE_CHECKED,
        tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        tool_usd_source_checked_at=SOURCE_CHECKED,
        tool_search_cost_cny_micros_per_success=5_038,
        tool_search_price_milli_per_success=1_800,
    )
    catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    db.flush()
    current_prices = catalog_service.public_pricing(db, now=draft.effective_at)["tool_milli_credits_per_success"]
    assert current_prices["search"] == 1_800
    stale_at = draft.effective_at + timedelta(days=31)
    assert catalog_service.public_pricing(db, now=stale_at)["tool_milli_credits_per_success"]["search"] is None
    with pytest.raises(catalog_service.RateCatalogError, match="来源已过期"):
        catalog_service.tool_rate_snapshot(db, "search", now=stale_at)

    draft.tool_search_price_milli_per_success = 1_799
    with pytest.raises(catalog_service.RateCatalogError, match="30% 直采毛利"):
        catalog_service.tool_rate_snapshot(db, "search", now=draft.effective_at)


def test_catalog_alerts_supplier_cost_increases_and_requires_distinct_approver(db):
    items = catalog_service.baseline_items()
    for item in items:
        item["input_rate_numerator"] = 2
        item["output_rate_numerator"] = 8
        item["provider_input_usd_micros_per_million"] = 360_000
        item["provider_output_usd_micros_per_million"] = 1_440_000
    draft = _draft(db, items=items)
    view = catalog_service.catalog_view(db, draft)
    assert len(view["supplier_price_diff"]["increases"]) == 6
    assert view["margin_bps"]["deepseek-flash"] == {"input_bps": 6977, "output_bps": 6977}
    assert view["source_url"].startswith("https://api-docs.deepseek.com/")
    assert view["fx_source_url"] == "https://www.boc.cn/sourcedb/whpj/index.html"
    with pytest.raises(catalog_service.RateCatalogError, match="不能审批自己的"):
        catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=10, now=NOW)
    approved = catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    db.flush()
    assert approved.status == "approved"
    assert approved.approved_by == 11
    assert [event.action for event in db.query(BillingRateCatalogEvent)
            .filter_by(catalog_id=draft.id).order_by(BillingRateCatalogEvent.id)] == ["created", "approved"]


def test_catalog_alerts_cny_only_cost_increases(db):
    items = catalog_service.baseline_items()
    for item in items:
        item["provider_input_cny_micros_per_million"] = 2_200_000
    draft = _draft(db, items=items)
    diff = catalog_service.catalog_view(db, draft)["supplier_price_diff"]
    assert len(diff["increases"]) == len(items)
    assert all(change["channel"] == "input_cny" for change in diff["increases"])


def test_public_quote_and_reservation_switch_together_and_keep_old_snapshot(db):
    items = catalog_service.baseline_items()
    for item in items:
        item["input_rate_numerator"] = 2
        item["output_rate_numerator"] = 8
    effective = NOW + timedelta(hours=2)
    draft = _draft(db, items=items, effective_at=effective)
    current = catalog_service.active_catalog(db, now=NOW)
    assert (draft.credit_pack_price_fen, draft.credit_pack_credits) == (
        current.credit_pack_price_fen, current.credit_pack_credits,
    )
    catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    db.flush()
    before = catalog_service.public_pricing(db, now=effective - timedelta(seconds=1))
    after = catalog_service.public_pricing(db, now=effective)
    assert before["pricing_version"] == ledger.RATE_CATALOG_VERSION
    assert before["next_effective_at"] == effective.replace(tzinfo=None)
    assert after["pricing_version"] == draft.version_key
    assert after["model_milli_per_token"]["deepseek-flash"] == {"input": 2, "output": 8}

    wallet = get_or_create_wallet(db, "user", 84)
    grant(db, wallet_id=wallet.id, grant_key="rate-catalog-fund", kind="purchase", amount_milli=500_000)
    identity = BillingProviderIdentity(
        identity_version="deepseek-official-v1", provider_name="deepseek",
        endpoint_host="api.deepseek.com", credential_source="system",
    )
    maximum = catalog_service.quote_milli(db, "deepseek-flash", 10, 5, now=effective)
    reservation = reserve(db, request_key="catalog-rate-request", model_name="deepseek-flash",
                          user_id=84, personal_wallet_id=wallet.id, max_milli=maximum,
                          provider_identity=identity, now=effective)
    assert reservation.requested_milli == 60
    assert (reservation.pricing_version, reservation.input_rate_numerator,
            reservation.output_rate_numerator) == (draft.version_key, 2, 8)

    # A later approved catalog does not reinterpret a call already reserved.
    items[0]["input_rate_numerator"] = 3
    items[0]["output_rate_numerator"] = 12
    later = _draft(db, items=items, actor_id=12, effective_at=effective + timedelta(days=1))
    catalog_service.approve_draft(db, catalog_id=later.id, actor_id=13, now=NOW)
    db.flush()
    assert ledger.quote_reservation_milli(
        db, request_key="catalog-rate-request", input_tokens=10, output_tokens=5,
    ) == 60
    reserved = db.query(BillingReservation).filter_by(request_key="catalog-rate-request").one()
    assert reserved.pricing_version == draft.version_key


def test_scheduled_catalog_can_only_be_canceled_by_uninvolved_admin_before_effective(db):
    effective = NOW + timedelta(hours=3)
    draft = _draft(db, effective_at=effective)
    catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11, now=NOW)
    with pytest.raises(catalog_service.RateCatalogError, match="不能取消"):
        catalog_service.cancel_scheduled(db, catalog_id=draft.id, actor_id=10,
                                         note="取消", now=NOW)
    canceled = catalog_service.cancel_scheduled(db, catalog_id=draft.id, actor_id=12,
                                               note="供应商来源复核发现误录。", now=NOW)
    assert canceled.status == "canceled"
    assert catalog_service.public_pricing(db, now=effective)["pricing_version"] == ledger.RATE_CATALOG_VERSION
    assert db.query(BillingRateCatalogEvent).filter_by(catalog_id=draft.id).count() == 3


def test_only_one_approved_version_can_own_a_scheduled_effective_time(db):
    effective = NOW + timedelta(hours=4)
    first = _draft(db, effective_at=effective)
    changed_items = catalog_service.baseline_items()
    changed_items[0]["input_rate_numerator"] = 2
    second = _draft(db, effective_at=effective, items=changed_items)
    catalog_service.approve_draft(db, catalog_id=first.id, actor_id=11, now=NOW)
    with pytest.raises(catalog_service.RateCatalogError, match="同一 UTC 生效时间"):
        catalog_service.approve_draft(db, catalog_id=second.id, actor_id=12, now=NOW)

    catalog_service.cancel_scheduled(db, catalog_id=first.id, actor_id=13,
                                     note="批准时间安排有误。", now=NOW)
    approved = catalog_service.approve_draft(db, catalog_id=second.id, actor_id=12, now=NOW)
    assert approved.status == "approved"


def test_approval_rejects_close_effective_time_and_rejected_drafts_stay_audited(db):
    draft = _draft(db)
    with pytest.raises(catalog_service.RateCatalogError, match="不足一小时"):
        catalog_service.approve_draft(db, catalog_id=draft.id, actor_id=11,
                                      now=NOW + timedelta(hours=1, minutes=1))
    rejected = catalog_service.reject_draft(db, catalog_id=draft.id, actor_id=11,
                                            note="成本事实与供应商账单不符。", now=NOW)
    assert rejected.status == "rejected"
    assert rejected.rejected_by == 11
    assert db.query(BillingRateCatalogEvent).filter_by(catalog_id=draft.id).count() == 2
