"""Reviewed and scheduled model pricing catalog lifecycle."""

from datetime import datetime, timedelta, timezone
from fractions import Fraction
from hashlib import sha256
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models.billing_ledger import (
    BillingRateCatalog,
    BillingRateCatalogEvent,
    BillingRateCatalogItem,
)
from app.services import billing_ledger_service as ledger
from app.services.billing_products import CREDIT_PACK_CREDITS, CREDIT_PACK_PRICE_FEN

MINIMUM_DIRECT_MODEL_MARGIN_BPS = 3000
MINIMUM_APPROVAL_LEAD = timedelta(hours=1)
MAX_SOURCE_AGE = timedelta(days=30)
_MAX_RATE_COMPONENT = 1_000_000_000
_MAX_RATE_DENOMINATOR = 1_000_000
_MAX_TOOL_RATE = 2**63 - 1
_SEARCH_FETCH_CNY_MICROS_PER_SUCCESS = 5_000
_SEARCH_FETCH_USD_MICROS_PER_SUCCESS = 750  # $0.75 per 1,000 successful requests.
_MODEL_PROVIDER_COSTS = {
    # USD micros per million tokens, at peak, cache-miss rates.
    "deepseek-flash": (300_000, 1_200_000),
    "deepseek-v4-flash": (300_000, 1_200_000),
    "deepseek-v4-flash-vision-exp": (300_000, 1_200_000),
}
_MODEL_PROVIDER_CNY_MICROS = {
    model: (2_000_000, 8_000_000) for model in _MODEL_PROVIDER_COSTS
}
_DEFAULT_FX_CNY_PER_USD_SCALED = 67_188  # CNY 6.7188 / USD, scale 10,000.
_DEFAULT_CREDIT_PACK_PRICE_FEN = CREDIT_PACK_PRICE_FEN
_DEFAULT_CREDIT_PACK_CREDITS = CREDIT_PACK_CREDITS


class RateCatalogError(ValueError):
    pass


def _utc_naive(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RateCatalogError(f"{field} 必须带时区，统一按 UTC 保存")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _official_source_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 512:
        raise RateCatalogError("价格来源链接无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RateCatalogError("价格来源链接无效") from exc
    host = (parsed.hostname or "").casefold()
    if (parsed.scheme.casefold() != "https" or not (host == "deepseek.com" or host.endswith(".deepseek.com"))
            or port not in (None, 443) or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise RateCatalogError("来源必须是无凭据、无查询参数的 HTTPS 官方 DeepSeek 页面")
    return value


def _model_source_url(value: str, *, cny: bool) -> str:
    expected = (
        "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/" if cny
        else "https://api-docs.deepseek.com/quick_start/pricing/"
    )
    if not isinstance(value, str) or value.rstrip("/") != expected.rstrip("/"):
        raise RateCatalogError("模型中英文价格来源必须为对应的 DeepSeek 官方价格页面")
    return value


def _official_fx_source_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 512:
        raise RateCatalogError("汇率来源链接无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RateCatalogError("汇率来源链接无效") from exc
    if (parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != "www.boc.cn"
            or parsed.path not in {"/sourcedb/whpj/", "/sourcedb/whpj/index.html"}
            or port not in (None, 443) or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise RateCatalogError("汇率来源必须是无凭据、无查询参数的 HTTPS 中国银行外汇牌价页面")
    return value


def _official_tool_source_url(value: str, *, currency: str) -> str:
    paths = {
        "CNY": {"/zh-cn/guides/search", "/zh-cn/guides/search/"},
        "USD": {"/quick_start/pricing", "/quick_start/pricing/"},
    }
    if currency not in paths or not isinstance(value, str) or value != value.strip() or len(value) > 512:
        raise RateCatalogError("Search/Fetch 官方价格来源链接无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RateCatalogError("Search/Fetch 官方价格来源链接无效") from exc
    if (parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != "api-docs.deepseek.com"
            or parsed.path not in paths[currency] or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise RateCatalogError(f"Search/Fetch {currency} 价格来源必须是无凭据、无查询参数的 DeepSeek 官方页面")
    return value


def _normalize_items(items: list[dict]) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise RateCatalogError("必须提交完整的可计费模型目录")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise RateCatalogError("模型费率项格式无效")
        model = item.get("model_name")
        if model not in ledger._BILLABLE_MODELS or model in seen:
            raise RateCatalogError("模型目录包含未批准模型或重复模型")
        seen.add(model)
        values = (
            item.get("input_rate_numerator"), item.get("input_rate_denominator"),
            item.get("output_rate_numerator"), item.get("output_rate_denominator"),
            item.get("provider_input_usd_micros_per_million"),
            item.get("provider_output_usd_micros_per_million"),
            item.get("provider_input_cny_micros_per_million"),
            item.get("provider_output_cny_micros_per_million"),
        )
        if any(type(value) is not int for value in values):
            raise RateCatalogError("费率及供应商价格必须使用整数分子/分母，不接受浮点数")
        in_num, in_den, out_num, out_den, provider_in, provider_out, cny_in, cny_out = values
        if any(value <= 0 or value > _MAX_RATE_COMPONENT for value in (in_num, out_num)):
            raise RateCatalogError("客户费率分子超出允许范围")
        if any(value <= 0 or value > _MAX_RATE_DENOMINATOR for value in (in_den, out_den)):
            raise RateCatalogError("客户费率分母超出允许范围")
        if any(value < 0 or value > 2**63 - 1 for value in (provider_in, provider_out)):
            raise RateCatalogError("供应商价格超出数据库支持范围")
        floor_usd = _MODEL_PROVIDER_COSTS[model]
        floor_cny = _MODEL_PROVIDER_CNY_MICROS[model]
        if (provider_in < floor_usd[0] or provider_out < floor_usd[1]
                or cny_in < floor_cny[0] or cny_out < floor_cny[1]
                or cny_in > 2**63 - 1 or cny_out > 2**63 - 1):
            raise RateCatalogError("模型中英文供应商成本低于官方价格保守下限或超出范围")
        normalized.append({
            "model_name": model,
            "input_rate_numerator": in_num,
            "input_rate_denominator": in_den,
            "output_rate_numerator": out_num,
            "output_rate_denominator": out_den,
            "provider_input_usd_micros_per_million": provider_in,
            "provider_output_usd_micros_per_million": provider_out,
            "provider_input_cny_micros_per_million": cny_in,
            "provider_output_cny_micros_per_million": cny_out,
        })
    if seen != set(ledger._BILLABLE_MODELS):
        raise RateCatalogError("必须且只能覆盖当前所有可计费模型")
    return sorted(normalized, key=lambda item: item["model_name"])


def _margin_bps(*, rate: Fraction, provider_usd_micros_per_million: int,
                provider_cny_micros_per_million: int, fx_cny_per_usd_scaled: int, credit_pack_price_fen: int,
                credit_pack_credits: int) -> int:
    revenue_cny_per_token = rate * Fraction(
        credit_pack_price_fen, 100 * credit_pack_credits * 1000,
    )
    cost_cny_per_token = max(
        Fraction(provider_usd_micros_per_million * fx_cny_per_usd_scaled, 10**16),
        Fraction(provider_cny_micros_per_million, 10**12),
    )
    if revenue_cny_per_token <= 0:
        return -10_000
    return int((revenue_cny_per_token - cost_cny_per_token) * 10_000 / revenue_cny_per_token)


def _version_key(items: list[dict], *, effective_at: datetime, source_digest: str,
                 fx: int, price_fen: int, credits: int,
                 tool_values: dict | None = None) -> str:
    lines = [
        "schema=3", f"effective_at={effective_at.isoformat(timespec='seconds')}",
        f"source_sha256={source_digest}", f"fx_cny_per_usd_scaled={fx}",
        f"credit_pack_price_fen={price_fen}", f"credit_pack_credits={credits}",
    ]
    lines.extend(
        ":".join(str(item[key]) for key in (
            "model_name", "input_rate_numerator", "input_rate_denominator",
            "output_rate_numerator", "output_rate_denominator",
            "provider_input_usd_micros_per_million", "provider_output_usd_micros_per_million",
        )) for item in items
    )
    if tool_values is not None:
        lines.extend((
            f"tool_cny_source_url={tool_values['tool_cny_source_url'] or 'none'}",
            "tool_cny_source_checked_at=" + (
                tool_values["tool_cny_source_checked_at"].isoformat(timespec="seconds")
                if tool_values["tool_cny_source_checked_at"] else "none"
            ),
            f"tool_usd_source_url={tool_values['tool_usd_source_url'] or 'none'}",
            "tool_usd_source_checked_at=" + (
                tool_values["tool_usd_source_checked_at"].isoformat(timespec="seconds")
                if tool_values["tool_usd_source_checked_at"] else "none"
            ),
            *(f"{key}={tool_values[key]}" for key in (
                "tool_search_cost_cny_micros_per_success",
                "tool_search_price_milli_per_success",
                "tool_fetch_cost_cny_micros_per_success",
                "tool_fetch_price_milli_per_success",
            )),
        ))
    return sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _source_facts_digest(*, source_url: str, source_checked_at: datetime, fx: int,
                         price_fen: int, credits: int, items: list[dict],
                         fx_source_url: str | None = None,
                         fx_source_checked_at: datetime | None = None,
                         tool_values: dict | None = None,
                         model_cny_source_url: str | None = None,
                         model_cny_source_checked_at: datetime | None = None) -> str:
    lines = [
        f"source_url={source_url}", f"source_checked_at_utc={source_checked_at.isoformat(timespec='seconds')}",
        f"fx_source_url={fx_source_url or 'https://www.boc.cn/sourcedb/whpj/index.html'}",
        "fx_source_checked_at_utc=" + (fx_source_checked_at or source_checked_at).isoformat(timespec="seconds"),
        f"fx_cny_per_usd_scaled={fx}", f"credit_pack_price_fen={price_fen}",
        f"credit_pack_credits={credits}",
    ]
    lines.extend(
        ":".join(str(item[key]) for key in (
            "model_name", "input_rate_numerator", "input_rate_denominator",
            "output_rate_numerator", "output_rate_denominator",
            "provider_input_usd_micros_per_million", "provider_output_usd_micros_per_million",
        )) for item in items
    )
    if model_cny_source_url is not None and model_cny_source_checked_at is not None:
        lines.extend((
            f"model_cny_source_url={model_cny_source_url}",
            f"model_cny_source_checked_at_utc={model_cny_source_checked_at.isoformat(timespec='seconds')}",
            *(f"{item['model_name']}:{item['provider_input_cny_micros_per_million']}:"
              f"{item['provider_output_cny_micros_per_million']}" for item in items),
        ))
    if tool_values is not None:
        lines.extend((
            f"tool_cny_source_url={tool_values['tool_cny_source_url'] or 'none'}",
            "tool_cny_source_checked_at_utc=" + (
                tool_values["tool_cny_source_checked_at"].isoformat(timespec="seconds")
                if tool_values["tool_cny_source_checked_at"] else "none"
            ),
            f"tool_usd_source_url={tool_values['tool_usd_source_url'] or 'none'}",
            "tool_usd_source_checked_at_utc=" + (
                tool_values["tool_usd_source_checked_at"].isoformat(timespec="seconds")
                if tool_values["tool_usd_source_checked_at"] else "none"
            ),
            *(f"{key}={tool_values[key]}" for key in (
                "tool_search_cost_cny_micros_per_success",
                "tool_search_price_milli_per_success",
                "tool_fetch_cost_cny_micros_per_success",
                "tool_fetch_price_milli_per_success",
            )),
        ))
    return sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _normalize_tool_rates(*, tool_cny_source_url: str | None,
                          tool_cny_source_checked_at: datetime | None,
                          tool_usd_source_url: str | None,
                          tool_usd_source_checked_at: datetime | None,
                          tool_search_cost_cny_micros_per_success: int,
                          tool_search_price_milli_per_success: int,
                          tool_fetch_cost_cny_micros_per_success: int,
                          tool_fetch_price_milli_per_success: int,
                          now: datetime) -> dict:
    rates = {
        "tool_search_cost_cny_micros_per_success": tool_search_cost_cny_micros_per_success,
        "tool_search_price_milli_per_success": tool_search_price_milli_per_success,
        "tool_fetch_cost_cny_micros_per_success": tool_fetch_cost_cny_micros_per_success,
        "tool_fetch_price_milli_per_success": tool_fetch_price_milli_per_success,
    }
    if any(type(value) is not int or value < 0 or value > _MAX_TOOL_RATE for value in rates.values()):
        raise RateCatalogError("Search/Fetch 成本和用户费率必须是非负数据库范围内的整数")
    for name in ("search", "fetch"):
        cost = rates[f"tool_{name}_cost_cny_micros_per_success"]
        price = rates[f"tool_{name}_price_milli_per_success"]
        if (cost == 0) != (price == 0):
            raise RateCatalogError("供应商成本和用户收费必须同时启用或同时为零")
    enabled = any(rates.values())
    if not enabled:
        if any((tool_cny_source_url, tool_cny_source_checked_at,
                tool_usd_source_url, tool_usd_source_checked_at)):
            raise RateCatalogError("未启用 Search/Fetch 费率时不能记录工具价格来源")
        return {
            **rates,
            "tool_cny_source_url": None,
            "tool_cny_source_checked_at": None,
            "tool_usd_source_url": None,
            "tool_usd_source_checked_at": None,
        }
    if any(value is None for value in (
        tool_cny_source_url, tool_cny_source_checked_at,
        tool_usd_source_url, tool_usd_source_checked_at,
    )):
        raise RateCatalogError("启用 Search/Fetch 费率必须分别提供官方 CNY 与 USD 价格来源和核验时间")
    cny_checked = _utc_naive(tool_cny_source_checked_at, "Search/Fetch CNY 价格核验时间")
    usd_checked = _utc_naive(tool_usd_source_checked_at, "Search/Fetch USD 价格核验时间")
    for checked in (cny_checked, usd_checked):
        if checked > now or now - checked > MAX_SOURCE_AGE:
            raise RateCatalogError("Search/Fetch 中英文价格来源必须在最近 30 天内核验且不能是未来时间")
    return {
        **rates,
        "tool_cny_source_url": _official_tool_source_url(tool_cny_source_url, currency="CNY"),
        "tool_cny_source_checked_at": cny_checked,
        "tool_usd_source_url": _official_tool_source_url(tool_usd_source_url, currency="USD"),
        "tool_usd_source_checked_at": usd_checked,
    }


def _items_for(db: Session, catalog_id: int) -> list[BillingRateCatalogItem]:
    return (db.query(BillingRateCatalogItem).filter_by(catalog_id=catalog_id)
            .order_by(BillingRateCatalogItem.model_name).all())


def _item_values(row: BillingRateCatalogItem) -> dict:
    fields = (
        "model_name", "input_rate_numerator", "input_rate_denominator",
        "output_rate_numerator", "output_rate_denominator",
        "provider_input_usd_micros_per_million", "provider_output_usd_micros_per_million",
        "provider_input_cny_micros_per_million", "provider_output_cny_micros_per_million",
    )
    return {field: getattr(row, field) for field in fields}


def _require_current_model_facts(db: Session, catalog: BillingRateCatalog, *, at: datetime,
                                 effective_at: datetime | None = None) -> list[dict]:
    checks = (catalog.source_checked_at, catalog.model_cny_source_checked_at, catalog.fx_source_checked_at)
    if any(value is None for value in checks) or catalog.model_cny_source_url is None:
        raise RateCatalogError("模型中英文价格或汇率来源未完整核验，不能用于新计费")
    if any(value > at or at - value > MAX_SOURCE_AGE for value in checks):
        raise RateCatalogError("模型中英文价格或汇率来源已过期或处于未来，不能用于新计费")
    if effective_at and any(effective_at - value > MAX_SOURCE_AGE for value in checks):
        raise RateCatalogError("模型中英文价格或汇率来源不能覆盖生效时间")
    _model_source_url(catalog.source_url, cny=False)
    _model_source_url(catalog.model_cny_source_url, cny=True)
    _official_fx_source_url(catalog.fx_source_url)
    items = _normalize_items([_item_values(item) for item in _items_for(db, catalog.id)])
    margins = _margin_report(items, fx=catalog.fx_cny_per_usd_scaled,
                             price_fen=catalog.credit_pack_price_fen, credits=catalog.credit_pack_credits)
    if any(min(item.values()) < MINIMUM_DIRECT_MODEL_MARGIN_BPS for item in margins.values()):
        raise RateCatalogError("模型双币直采毛利低于 30% 保护线")
    return items


def latest_approved(db: Session) -> BillingRateCatalog | None:
    return (db.query(BillingRateCatalog).filter(BillingRateCatalog.status == "approved")
            .order_by(BillingRateCatalog.effective_at.desc(), BillingRateCatalog.id.desc()).first())


def active_catalog(db: Session, *, now: datetime | None = None, lock_for_use: bool = False,
                   clock=None) -> BillingRateCatalog:
    at = _now(now)

    def lookup(when):
        query = (db.query(BillingRateCatalog)
                 .filter(BillingRateCatalog.status == "approved", BillingRateCatalog.effective_at <= when)
                 .order_by(BillingRateCatalog.effective_at.desc(), BillingRateCatalog.id.desc()))
        if lock_for_use:
            query = query.populate_existing().with_for_update(read=True)
        return query.first()

    catalog = lookup(at)
    if catalog is None:
        raise RateCatalogError("当前没有已生效且经审批的模型费率目录")
    if lock_for_use:
        # A lock wait may cross a scheduled activation. Never charge using the
        # earlier clock if a different version is current after the wait.
        current = lookup(_now(clock() if clock is not None else None))
        if current is None or current.id != catalog.id:
            raise RateCatalogError("费率目录在等待期间发生切换，请重试")
    if catalog.credit_pack_credits != CREDIT_PACK_CREDITS:
        raise RateCatalogError("生效费率目录的积分到账数量与账本商品配置不一致，已停止计费")
    items = _items_for(db, catalog.id)
    if {item.model_name for item in items} != set(ledger._BILLABLE_MODELS):
        raise RateCatalogError("当前费率目录缺少已批准模型或含有未批准模型，已停止计费")
    return catalog


def rate_snapshot(db: Session, model_name: str, *, now: datetime | None = None, lock_for_use: bool = False, clock=None):
    if model_name not in ledger._BILLABLE_MODELS:
        raise RateCatalogError("该模型尚未获准平台计费")
    catalog = active_catalog(db, now=now, lock_for_use=lock_for_use, clock=clock)
    _require_current_model_facts(db, catalog, at=_now(now))
    if lock_for_use:
        _require_current_model_facts(db, catalog, at=_now(clock() if clock is not None else None))
    item = db.query(BillingRateCatalogItem).filter_by(
        catalog_id=catalog.id, model_name=model_name,
    ).one_or_none()
    if item is None:
        raise RateCatalogError("当前费率目录缺少模型条目，已停止计费")
    return (
        catalog.version_key,
        Fraction(item.input_rate_numerator, item.input_rate_denominator),
        Fraction(item.output_rate_numerator, item.output_rate_denominator),
    )


def tool_rate_snapshot(db: Session, tool_name: str, *, now: datetime | None = None,
                       lock_for_use: bool = False, clock=None):
    if tool_name not in {"search", "fetch"}:
        raise RateCatalogError("该 Search/Fetch 工具尚未获准按次计费")
    at = _now(now)
    catalog = active_catalog(db, now=at, lock_for_use=lock_for_use, clock=clock)
    if lock_for_use:
        at = max(at, _now(clock() if clock is not None else None))
    cost = getattr(catalog, f"tool_{tool_name}_cost_cny_micros_per_success")
    price = getattr(catalog, f"tool_{tool_name}_price_milli_per_success")
    source_checks = (
        catalog.tool_cny_source_checked_at,
        catalog.tool_usd_source_checked_at,
        catalog.fx_source_checked_at,
    )
    if cost <= 0 or price <= 0 or any(checked is None for checked in source_checks):
        raise RateCatalogError("该工具当前没有已批准的按次费率")
    if any(checked > at or at - checked > MAX_SOURCE_AGE for checked in source_checks):
        raise RateCatalogError("Search/Fetch 中英文价格或汇率来源已过期，已停止按次计费")
    _official_tool_source_url(catalog.tool_cny_source_url, currency="CNY")
    _official_tool_source_url(catalog.tool_usd_source_url, currency="USD")
    _official_fx_source_url(catalog.fx_source_url)
    minimum_cost = _minimum_tool_cost_cny_micros(
        fx_cny_per_usd_scaled=catalog.fx_cny_per_usd_scaled,
    )
    if cost < minimum_cost:
        raise RateCatalogError("当前工具成本快照低于官方价格的保守折算下限，已停止按次计费")
    if _tool_margin_bps(
        price_milli=price, cost_cny_micros=cost,
        price_fen=catalog.credit_pack_price_fen, credits=catalog.credit_pack_credits,
    ) < MINIMUM_DIRECT_MODEL_MARGIN_BPS:
        raise RateCatalogError("当前工具价格快照低于 30% 直采毛利保护线，已停止按次计费")
    return catalog.version_key, cost, price


def quote_milli(db: Session, model_name: str, input_tokens: int, output_tokens: int,
                *, now: datetime | None = None) -> int:
    version, input_rate, output_rate = rate_snapshot(db, model_name, now=now)
    del version
    return ledger._quote_with_rates((input_rate, output_rate), input_tokens, output_tokens)


def _public_items(catalog: BillingRateCatalog, items: list[BillingRateCatalogItem]) -> dict:
    def rate(numerator: int, denominator: int) -> int | float:
        return numerator if denominator == 1 else float(Fraction(numerator, denominator))

    return {
        item.model_name: {
            "input": rate(item.input_rate_numerator, item.input_rate_denominator),
            "output": rate(item.output_rate_numerator, item.output_rate_denominator),
        }
        for item in items
    }


def public_pricing(db: Session, *, now: datetime | None = None) -> dict:
    at = _now(now)
    current = active_catalog(db, now=at)
    try:
        _require_current_model_facts(db, current, at=at)
        model_pricing_ready = True
    except RateCatalogError:
        model_pricing_ready = False
    upcoming = (db.query(BillingRateCatalog).filter(
        BillingRateCatalog.status == "approved", BillingRateCatalog.effective_at > at,
    ).order_by(BillingRateCatalog.effective_at.asc(), BillingRateCatalog.id.asc()).first())
    return {
        "pricing_version": current.version_key,
        "effective_at": current.effective_at,
        "next_effective_at": upcoming.effective_at if upcoming else None,
        "credit_pack": {
            "credits": current.credit_pack_credits,
            "price_fen": current.credit_pack_price_fen,
        },
        "model_milli_per_token": _public_items(current, _items_for(db, current.id)),
        "model_pricing_ready": model_pricing_ready,
        "tool_milli_credits_per_success": {
            name: _public_tool_price(db, name, now=at)
            for name in ("search", "fetch")
        },
        "billable_models": tuple(sorted(ledger._BILLABLE_MODELS)),
    }


def _public_tool_price(db: Session, tool_name: str, *, now: datetime) -> int | None:
    try:
        _, _, price = tool_rate_snapshot(db, tool_name, now=now)
    except RateCatalogError:
        return None
    return price


def _margin_report(items: list[dict], *, fx: int, price_fen: int, credits: int) -> dict:
    report = {}
    for item in items:
        input_bps = _margin_bps(
            rate=Fraction(item["input_rate_numerator"], item["input_rate_denominator"]),
            provider_usd_micros_per_million=item["provider_input_usd_micros_per_million"],
            provider_cny_micros_per_million=item["provider_input_cny_micros_per_million"],
            fx_cny_per_usd_scaled=fx, credit_pack_price_fen=price_fen,
            credit_pack_credits=credits,
        )
        output_bps = _margin_bps(
            rate=Fraction(item["output_rate_numerator"], item["output_rate_denominator"]),
            provider_usd_micros_per_million=item["provider_output_usd_micros_per_million"],
            provider_cny_micros_per_million=item["provider_output_cny_micros_per_million"],
            fx_cny_per_usd_scaled=fx, credit_pack_price_fen=price_fen,
            credit_pack_credits=credits,
        )
        report[item["model_name"]] = {"input_bps": input_bps, "output_bps": output_bps}
    return report


def _tool_margin_bps(*, price_milli: int, cost_cny_micros: int,
                     price_fen: int, credits: int) -> int:
    revenue_cny = Fraction(price_milli * price_fen, 100 * credits * 1000)
    cost_cny = Fraction(cost_cny_micros, 1_000_000)
    if revenue_cny <= 0:
        return -10_000
    return int((revenue_cny - cost_cny) * 10_000 / revenue_cny)


def _minimum_tool_cost_cny_micros(*, fx_cny_per_usd_scaled: int) -> int:
    usd_cost_cny_micros = Fraction(
        _SEARCH_FETCH_USD_MICROS_PER_SUCCESS * fx_cny_per_usd_scaled, 10_000,
    )
    rounded_usd_cost = (
        usd_cost_cny_micros.numerator + usd_cost_cny_micros.denominator - 1
    ) // usd_cost_cny_micros.denominator
    return max(_SEARCH_FETCH_CNY_MICROS_PER_SUCCESS, rounded_usd_cost)


def _tool_margin_report(tool_values: dict, *, price_fen: int, credits: int) -> dict:
    report = {}
    for name in ("search", "fetch"):
        cost = tool_values[f"tool_{name}_cost_cny_micros_per_success"]
        price = tool_values[f"tool_{name}_price_milli_per_success"]
        report[name] = (
            _tool_margin_bps(price_milli=price, cost_cny_micros=cost,
                             price_fen=price_fen, credits=credits)
            if cost and price else None
        )
    return report


def _compare_supplier_prices(db: Session, items: list[dict], prior: BillingRateCatalog | None,
                             tool_values: dict | None = None) -> dict:
    if prior is None:
        return {"changes": [], "increases": [], "decreases": [],
                "tool_changes": [], "tool_increases": [], "tool_decreases": []}
    old_items = {row.model_name: row for row in _items_for(db, prior.id)}
    changes, increases, decreases = [], [], []
    for item in items:
        old = old_items.get(item["model_name"])
        if old is None:
            continue
        for channel, key, old_value in (
            ("input", "provider_input_usd_micros_per_million", old.provider_input_usd_micros_per_million),
            ("output", "provider_output_usd_micros_per_million", old.provider_output_usd_micros_per_million),
            ("input_cny", "provider_input_cny_micros_per_million", old.provider_input_cny_micros_per_million),
            ("output_cny", "provider_output_cny_micros_per_million", old.provider_output_cny_micros_per_million),
        ):
            new_value = item[key]
            if old_value is None or new_value is None or new_value == old_value:
                continue
            change = {"model_name": item["model_name"], "channel": channel,
                      "previous": old_value, "current": new_value}
            changes.append(change)
            (increases if new_value > old_value else decreases).append(change)
    tool_changes, tool_increases, tool_decreases = [], [], []
    if tool_values is not None:
        for name in ("search", "fetch"):
            key = f"tool_{name}_cost_cny_micros_per_success"
            old_value = getattr(prior, key)
            new_value = tool_values[key]
            if new_value == old_value:
                continue
            change = {"tool": name, "previous": old_value, "current": new_value}
            tool_changes.append(change)
            (tool_increases if new_value > old_value else tool_decreases).append(change)
    return {"changes": changes, "increases": increases, "decreases": decreases,
            "tool_changes": tool_changes, "tool_increases": tool_increases,
            "tool_decreases": tool_decreases}


def create_draft(db: Session, *, actor_id: int, effective_at: datetime, source_url: str,
                 source_checked_at: datetime, fx_source_url: str, fx_source_checked_at: datetime,
                 fx_cny_per_usd_scaled: int, change_note: str, items: list[dict],
                 model_cny_source_url: str, model_cny_source_checked_at: datetime,
                 tool_cny_source_url: str | None = None,
                 tool_cny_source_checked_at: datetime | None = None,
                 tool_usd_source_url: str | None = None,
                 tool_usd_source_checked_at: datetime | None = None,
                 tool_search_cost_cny_micros_per_success: int = 0,
                 tool_search_price_milli_per_success: int = 0,
                 tool_fetch_cost_cny_micros_per_success: int = 0,
                 tool_fetch_price_milli_per_success: int = 0,
                 now: datetime | None = None) -> BillingRateCatalog:
    at = _now(now)
    effective = _utc_naive(effective_at, "生效时间")
    checked = _utc_naive(source_checked_at, "供应商核价时间")
    fx_checked = _utc_naive(fx_source_checked_at, "汇率核价时间")
    cny_checked = _utc_naive(model_cny_source_checked_at, "模型人民币价格核验时间")
    if effective < at + MINIMUM_APPROVAL_LEAD:
        raise RateCatalogError("费率目录生效时间至少应晚于当前时间一小时")
    if checked > at or at - checked > MAX_SOURCE_AGE:
        raise RateCatalogError("供应商价格来源必须在最近 30 天内核验且不能是未来时间")
    source_url = _model_source_url(source_url, cny=False)
    model_cny_source_url = _model_source_url(model_cny_source_url, cny=True)
    if any(value > at or at - value > MAX_SOURCE_AGE or effective - value > MAX_SOURCE_AGE
           for value in (checked, fx_checked, cny_checked)):
        raise RateCatalogError("模型中英文价格及汇率来源必须在最近 30 天核验并覆盖生效时间")
    if fx_checked > at or at - fx_checked > MAX_SOURCE_AGE:
        raise RateCatalogError("汇率来源必须在最近 30 天内核验且不能是未来时间")
    fx_source_url = _official_fx_source_url(fx_source_url)
    normalized = _normalize_items(items)
    tool_values = _normalize_tool_rates(
        tool_cny_source_url=tool_cny_source_url,
        tool_cny_source_checked_at=tool_cny_source_checked_at,
        tool_usd_source_url=tool_usd_source_url,
        tool_usd_source_checked_at=tool_usd_source_checked_at,
        tool_search_cost_cny_micros_per_success=tool_search_cost_cny_micros_per_success,
        tool_search_price_milli_per_success=tool_search_price_milli_per_success,
        tool_fetch_cost_cny_micros_per_success=tool_fetch_cost_cny_micros_per_success,
        tool_fetch_price_milli_per_success=tool_fetch_price_milli_per_success,
        now=at,
    )
    if any(tool_values[f"tool_{currency}_source_checked_at"] for currency in ("cny", "usd")):
        source_times = (
            tool_values["tool_cny_source_checked_at"],
            tool_values["tool_usd_source_checked_at"],
            fx_checked,
        )
        if any(effective - checked > MAX_SOURCE_AGE for checked in source_times):
            raise RateCatalogError("Search/Fetch 中英文价格及汇率来源必须覆盖计划生效时间，生效时不能超过 30 天")
    if type(actor_id) is not int or actor_id <= 0:
        raise RateCatalogError("创建人身份无效")
    latest = latest_approved(db)
    if latest is not None and effective <= latest.effective_at:
        raise RateCatalogError("新费率生效时间必须晚于所有已批准目录")
    if type(fx_cny_per_usd_scaled) is not int or not 1 <= fx_cny_per_usd_scaled <= 1_000_000_000:
        raise RateCatalogError("汇率参考值无效")
    # Checkout reads the active catalog's unit price, so each new model-rate
    # revision inherits that same product basis. Changing package price itself
    # is outside this admin form and requires an explicit reviewed migration.
    credit_pack_price_fen = latest.credit_pack_price_fen if latest is not None else _DEFAULT_CREDIT_PACK_PRICE_FEN
    credit_pack_credits = latest.credit_pack_credits if latest is not None else _DEFAULT_CREDIT_PACK_CREDITS
    if not isinstance(change_note, str) or not change_note.strip() or len(change_note.strip()) > 500:
        raise RateCatalogError("必须填写不超过 500 字的费率变更说明")
    margins = _margin_report(normalized, fx=fx_cny_per_usd_scaled,
                             price_fen=credit_pack_price_fen, credits=credit_pack_credits)
    below_minimum = {
        model: channels for model, channels in margins.items()
        if min(channels["input_bps"], channels["output_bps"]) < MINIMUM_DIRECT_MODEL_MARGIN_BPS
    }
    if below_minimum:
        raise RateCatalogError("模型直采成本毛利低于 30% 保护线，不能提交审批")
    tool_margins = _tool_margin_report(
        tool_values, price_fen=credit_pack_price_fen, credits=credit_pack_credits,
    )
    minimum_tool_cost = _minimum_tool_cost_cny_micros(fx_cny_per_usd_scaled=fx_cny_per_usd_scaled)
    below_official_cost = [
        name for name in ("search", "fetch")
        if tool_values[f"tool_{name}_cost_cny_micros_per_success"]
        and tool_values[f"tool_{name}_cost_cny_micros_per_success"] < minimum_tool_cost
    ]
    if below_official_cost:
        raise RateCatalogError(
            f"{', '.join(name.title() for name in below_official_cost)} 供应商成本低于官方价格的保守折算下限 "
            f"{minimum_tool_cost} 微元/次"
        )
    below_minimum_tools = [
        name for name, margin in tool_margins.items()
        if margin is not None and margin < MINIMUM_DIRECT_MODEL_MARGIN_BPS
    ]
    if below_minimum_tools:
        raise RateCatalogError(
            f"{', '.join(name.title() for name in below_minimum_tools)} 直采成本毛利低于 30% 保护线，不能提交审批"
        )
    source_sha256 = _source_facts_digest(
        source_url=source_url, source_checked_at=checked,
        fx=fx_cny_per_usd_scaled, price_fen=credit_pack_price_fen,
        credits=credit_pack_credits, items=normalized,
        fx_source_url=fx_source_url, fx_source_checked_at=fx_checked,
        tool_values=tool_values,
        model_cny_source_url=model_cny_source_url,
        model_cny_source_checked_at=cny_checked,
    )
    version = _version_key(normalized, effective_at=effective, source_digest=source_sha256,
                           fx=fx_cny_per_usd_scaled, price_fen=credit_pack_price_fen,
                           credits=credit_pack_credits, tool_values=tool_values)
    if db.query(BillingRateCatalog.id).filter_by(version_key=version).first() is not None:
        raise RateCatalogError("相同价格事实目录已存在，不能重复创建")
    catalog = BillingRateCatalog(
        version_key=version, status="draft", effective_at=effective, source_url=source_url,
        source_checked_at=checked, fx_source_url=fx_source_url, fx_source_checked_at=fx_checked,
        model_cny_source_url=model_cny_source_url, model_cny_source_checked_at=cny_checked,
        source_sha256=source_sha256,
        fx_cny_per_usd_scaled=fx_cny_per_usd_scaled, fx_scale=10000,
        credit_pack_price_fen=credit_pack_price_fen, credit_pack_credits=credit_pack_credits,
        **tool_values,
        change_note=change_note.strip(), created_by=actor_id, created_at=at,
    )
    db.add(catalog)
    db.flush()
    for item in normalized:
        db.add(BillingRateCatalogItem(catalog_id=catalog.id, **item))
    db.add(BillingRateCatalogEvent(
        catalog_id=catalog.id, actor_id=actor_id, action="created",
        note=catalog.change_note, created_at=at,
    ))
    db.flush()
    catalog._margin_report = margins
    catalog._tool_margin_report = tool_margins
    catalog._supplier_price_diff = _compare_supplier_prices(
        db, normalized, latest_approved(db), tool_values,
    )
    return catalog


def approve_draft(db: Session, *, catalog_id: int, actor_id: int,
                  now: datetime | None = None, clock=None) -> BillingRateCatalog:
    if type(actor_id) is not int or actor_id <= 0:
        raise RateCatalogError("审批人身份无效")
    catalog = (db.query(BillingRateCatalog).filter_by(id=catalog_id)
               .with_for_update().one_or_none())
    if catalog is None or catalog.status != "draft":
        raise RateCatalogError("费率草稿不存在或已完成审批")
    # The row lock may wait across activation or source expiry. Approval
    # checks must use the time after the lock was acquired.
    at = _now(clock() if clock is not None else now)
    if catalog.created_by == actor_id:
        raise RateCatalogError("创建人不能审批自己的费率目录")
    _require_current_model_facts(db, catalog, at=at, effective_at=catalog.effective_at)
    if any((catalog.tool_search_cost_cny_micros_per_success,
            catalog.tool_fetch_cost_cny_micros_per_success)):
        source_checks = (
            catalog.tool_cny_source_checked_at,
            catalog.tool_usd_source_checked_at,
            catalog.fx_source_checked_at,
        )
        if (any(checked is None or checked > at or at - checked > MAX_SOURCE_AGE for checked in source_checks)
                or any(catalog.effective_at - checked > MAX_SOURCE_AGE for checked in source_checks if checked)):
            raise RateCatalogError("Search/Fetch 中英文价格或汇率来源在审批/生效时已超过 30 天，不能批准")
        minimum_cost = _minimum_tool_cost_cny_micros(
            fx_cny_per_usd_scaled=catalog.fx_cny_per_usd_scaled,
        )
        for name in ("search", "fetch"):
            cost = getattr(catalog, f"tool_{name}_cost_cny_micros_per_success")
            price = getattr(catalog, f"tool_{name}_price_milli_per_success")
            if not cost:
                continue
            if cost < minimum_cost:
                raise RateCatalogError(f"{name.title()} 供应商成本低于官方价格的保守折算下限，不能批准")
            if _tool_margin_bps(
                price_milli=price, cost_cny_micros=cost,
                price_fen=catalog.credit_pack_price_fen, credits=catalog.credit_pack_credits,
            ) < MINIMUM_DIRECT_MODEL_MARGIN_BPS:
                raise RateCatalogError(f"{name.title()} 直采成本毛利低于 30% 保护线，不能批准")
    if catalog.effective_at < at + MINIMUM_APPROVAL_LEAD:
        raise RateCatalogError("费率目录距离生效不足一小时，不能批准")
    conflict = db.query(BillingRateCatalog.id).filter(
        BillingRateCatalog.status == "approved",
        BillingRateCatalog.approved_effective_at == catalog.effective_at,
    ).first()
    if conflict is not None:
        raise RateCatalogError("同一 UTC 生效时间已被另一个批准版本占用")
    if len(_items_for(db, catalog.id)) != len(ledger._BILLABLE_MODELS):
        raise RateCatalogError("费率草稿模型条目不完整，不能批准")
    changed = (db.query(BillingRateCatalog).filter_by(id=catalog.id, status="draft")
               .update({BillingRateCatalog.status: "approved",
                        BillingRateCatalog.approved_effective_at: catalog.effective_at,
                        BillingRateCatalog.approved_by: actor_id,
                        BillingRateCatalog.approved_at: at}, synchronize_session=False))
    if changed != 1:
        raise RateCatalogError("该费率草稿已被其他管理员处理")
    catalog.status = "approved"
    catalog.approved_by = actor_id
    catalog.approved_at = at
    db.add(BillingRateCatalogEvent(
        catalog_id=catalog.id, actor_id=actor_id, action="approved",
        note="独立授权复核人已审批；将按 UTC 生效时间自动切换。", created_at=at,
    ))
    db.flush()
    return catalog


def reject_draft(db: Session, *, catalog_id: int, actor_id: int, note: str,
                 now: datetime | None = None) -> BillingRateCatalog:
    at = _now(now)
    if type(actor_id) is not int or actor_id <= 0:
        raise RateCatalogError("操作人身份无效")
    catalog = (db.query(BillingRateCatalog).filter_by(id=catalog_id)
               .with_for_update().one_or_none())
    if catalog is None or catalog.status != "draft":
        raise RateCatalogError("只有待审批费率草稿可以拒绝")
    if not isinstance(note, str) or not note.strip() or len(note.strip()) > 500:
        raise RateCatalogError("必须填写拒绝原因")
    changed = (db.query(BillingRateCatalog).filter_by(id=catalog.id, status="draft")
               .update({BillingRateCatalog.status: "rejected",
                        BillingRateCatalog.rejected_by: actor_id,
                        BillingRateCatalog.rejected_at: at,
                        BillingRateCatalog.transition_note: note.strip()},
                       synchronize_session=False))
    if changed != 1:
        raise RateCatalogError("该费率草稿已被其他管理员处理")
    catalog.status = "rejected"
    catalog.rejected_by = actor_id
    catalog.rejected_at = at
    catalog.transition_note = note.strip()
    db.add(BillingRateCatalogEvent(catalog_id=catalog.id, actor_id=actor_id,
                                   action="rejected", note=note.strip(), created_at=at))
    db.flush()
    return catalog


def cancel_scheduled(db: Session, *, catalog_id: int, actor_id: int, note: str,
                     now: datetime | None = None) -> BillingRateCatalog:
    at = _now(now)
    if type(actor_id) is not int or actor_id <= 0:
        raise RateCatalogError("操作人身份无效")
    catalog = (db.query(BillingRateCatalog).filter_by(id=catalog_id)
               .with_for_update().one_or_none())
    if catalog is None or catalog.status != "approved" or catalog.effective_at <= at:
        raise RateCatalogError("只有尚未生效的已批准目录可以取消")
    if actor_id in {catalog.created_by, catalog.approved_by}:
        raise RateCatalogError("目录创建人和审批人不能取消该已批准版本")
    if not isinstance(note, str) or not note.strip() or len(note.strip()) > 500:
        raise RateCatalogError("必须填写取消原因")
    changed = (db.query(BillingRateCatalog).filter(
        BillingRateCatalog.id == catalog.id, BillingRateCatalog.status == "approved",
        BillingRateCatalog.effective_at > at,
    ).update({BillingRateCatalog.status: "canceled",
              BillingRateCatalog.canceled_by: actor_id,
              BillingRateCatalog.canceled_at: at,
              BillingRateCatalog.approved_effective_at: None,
              BillingRateCatalog.transition_note: note.strip()}, synchronize_session=False))
    if changed != 1:
        raise RateCatalogError("目录已生效或被其他管理员处理，无法取消")
    catalog.status = "canceled"
    catalog.canceled_by = actor_id
    catalog.canceled_at = at
    catalog.transition_note = note.strip()
    db.add(BillingRateCatalogEvent(catalog_id=catalog.id, actor_id=actor_id,
                                   action="canceled", note=note.strip(), created_at=at))
    db.flush()
    return catalog


def list_catalogs(db: Session, *, limit: int = 100) -> list[BillingRateCatalog]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise RateCatalogError("费率版本列表条数应为 1 到 100")
    return (db.query(BillingRateCatalog).order_by(
        BillingRateCatalog.effective_at.desc(), BillingRateCatalog.id.desc(),
    ).limit(limit).all())


def catalog_view(db: Session, catalog: BillingRateCatalog) -> dict:
    items = _items_for(db, catalog.id)
    input = [_item_values(row) for row in items]
    events = (db.query(BillingRateCatalogEvent).filter_by(catalog_id=catalog.id)
              .order_by(BillingRateCatalogEvent.id).all())
    prior = (db.query(BillingRateCatalog).filter(
        BillingRateCatalog.status == "approved",
        BillingRateCatalog.effective_at < catalog.effective_at,
    ).order_by(BillingRateCatalog.effective_at.desc(), BillingRateCatalog.id.desc()).first())
    has_cny_costs = bool(catalog.model_cny_source_url and catalog.model_cny_source_checked_at) and all(
        item["provider_input_cny_micros_per_million"] is not None
        and item["provider_output_cny_micros_per_million"] is not None for item in input
    )
    margin_report = _margin_report(input, fx=catalog.fx_cny_per_usd_scaled,
                                   price_fen=catalog.credit_pack_price_fen,
                                   credits=catalog.credit_pack_credits) if input and has_cny_costs else {}
    tool_values = {
        "tool_cny_source_url": catalog.tool_cny_source_url,
        "tool_cny_source_checked_at": catalog.tool_cny_source_checked_at,
        "tool_usd_source_url": catalog.tool_usd_source_url,
        "tool_usd_source_checked_at": catalog.tool_usd_source_checked_at,
        "tool_search_cost_cny_micros_per_success": catalog.tool_search_cost_cny_micros_per_success,
        "tool_search_price_milli_per_success": catalog.tool_search_price_milli_per_success,
        "tool_fetch_cost_cny_micros_per_success": catalog.tool_fetch_cost_cny_micros_per_success,
        "tool_fetch_price_milli_per_success": catalog.tool_fetch_price_milli_per_success,
    }
    tool_margins = _tool_margin_report(
        tool_values, price_fen=catalog.credit_pack_price_fen, credits=catalog.credit_pack_credits,
    )
    price_diff = (
        _compare_supplier_prices(db, input, prior, tool_values)
        if input else {"changes": [], "increases": [], "decreases": [],
                       "tool_changes": [], "tool_increases": [], "tool_decreases": []}
    )
    return {
        "id": catalog.id, "version_key": catalog.version_key, "status": catalog.status,
        "effective_at": catalog.effective_at, "source_url": catalog.source_url,
        "model_cny_source_url": catalog.model_cny_source_url,
        "model_cny_source_checked_at": catalog.model_cny_source_checked_at,
        "source_checked_at": catalog.source_checked_at, "fx_source_url": catalog.fx_source_url,
        "fx_source_checked_at": catalog.fx_source_checked_at,
        "source_sha256": catalog.source_sha256,
        "fx_cny_per_usd": catalog.fx_cny_per_usd_scaled / catalog.fx_scale,
        "credit_pack_price_fen": catalog.credit_pack_price_fen,
        "credit_pack_credits": catalog.credit_pack_credits,
        **tool_values,
        "change_note": catalog.change_note, "created_by": catalog.created_by,
        "created_at": catalog.created_at, "approved_by": catalog.approved_by,
        "approved_at": catalog.approved_at, "rejected_by": catalog.rejected_by,
        "rejected_at": catalog.rejected_at, "canceled_by": catalog.canceled_by,
        "canceled_at": catalog.canceled_at, "transition_note": catalog.transition_note,
        "minimum_direct_margin_bps": MINIMUM_DIRECT_MODEL_MARGIN_BPS,
        "margin_bps": margin_report, "tool_margin_bps": tool_margins,
        "supplier_price_diff": price_diff,
        "tool_supplier_price_diff": {
            "changes": price_diff["tool_changes"],
            "increases": price_diff["tool_increases"],
            "decreases": price_diff["tool_decreases"],
        },
        "items": input,
        "events": [{"actor_id": event.actor_id, "action": event.action,
                    "note": event.note, "created_at": event.created_at} for event in events],
    }


def current_or_default_pricing(db: Session, *, now: datetime | None = None) -> dict:
    """Test/bootstrap helper: migrations seed a baseline; production never uses static fallback."""
    try:
        return public_pricing(db, now=now)
    except RateCatalogError:
        # Metadata-only test databases are bootstrapped by fixture; fail closed everywhere else.
        raise


def baseline_items() -> list[dict]:
    """Return the code-approved bootstrap values for unit database fixtures only."""
    return [
        {
            "model_name": model,
            "input_rate_numerator": 1,
            "input_rate_denominator": 1,
            "output_rate_numerator": 4,
            "output_rate_denominator": 1,
            "provider_input_usd_micros_per_million": _MODEL_PROVIDER_COSTS[model][0],
            "provider_output_usd_micros_per_million": _MODEL_PROVIDER_COSTS[model][1],
            "provider_input_cny_micros_per_million": _MODEL_PROVIDER_CNY_MICROS[model][0],
            "provider_output_cny_micros_per_million": _MODEL_PROVIDER_CNY_MICROS[model][1],
        }
        for model in sorted(ledger._BILLABLE_MODELS)
    ]


def baseline_values() -> dict:
    return {
        "fx_cny_per_usd_scaled": _DEFAULT_FX_CNY_PER_USD_SCALED,
        "credit_pack_price_fen": _DEFAULT_CREDIT_PACK_PRICE_FEN,
        "credit_pack_credits": _DEFAULT_CREDIT_PACK_CREDITS,
    }


def seed_test_baseline(db: Session) -> None:
    """Seed the migration's baseline in metadata-only test databases, never in production."""
    if db.query(BillingRateCatalog.id).first() is not None:
        return
    checked = datetime(2026, 10, 20, 5, 30)
    provider_source = "https://api-docs.deepseek.com/quick_start/pricing/"
    model_cny_source = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
    fx_source = "https://www.boc.cn/sourcedb/whpj/index.html"
    items = baseline_items()
    catalog = BillingRateCatalog(
        version_key=ledger.RATE_CATALOG_VERSION, status="approved", effective_at=datetime(1970, 1, 1),
        approved_effective_at=datetime(1970, 1, 1),
        source_url=provider_source,
        source_checked_at=checked,
        model_cny_source_url=model_cny_source, model_cny_source_checked_at=checked,
        fx_source_url=fx_source, fx_source_checked_at=checked,
        source_sha256=_source_facts_digest(
            source_url=provider_source, source_checked_at=checked,
            fx_source_url=fx_source, fx_source_checked_at=checked,
            model_cny_source_url=model_cny_source, model_cny_source_checked_at=checked,
            fx=_DEFAULT_FX_CNY_PER_USD_SCALED,
            price_fen=_DEFAULT_CREDIT_PACK_PRICE_FEN,
            credits=_DEFAULT_CREDIT_PACK_CREDITS, items=items,
        ),
        fx_cny_per_usd_scaled=_DEFAULT_FX_CNY_PER_USD_SCALED, fx_scale=10_000,
        credit_pack_price_fen=_DEFAULT_CREDIT_PACK_PRICE_FEN,
        credit_pack_credits=_DEFAULT_CREDIT_PACK_CREDITS,
        change_note="测试数据库基线目录；生产由 098 迁移写入。",
        created_by=None, created_at=checked, approved_by=None, approved_at=checked,
    )
    db.add(catalog)
    db.flush()
    for item in items:
        db.add(BillingRateCatalogItem(catalog_id=catalog.id, **item))
    db.add(BillingRateCatalogEvent(
        catalog_id=catalog.id, actor_id=None, action="created",
        note="测试数据库费率目录初始化。", created_at=checked,
    ))
    db.add(BillingRateCatalogEvent(
        catalog_id=catalog.id, actor_id=None, action="approved",
        note="保留当前代码基线费率。", created_at=checked,
    ))
    db.flush()
