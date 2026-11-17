"""平台托管模型的保守成本预占与结算，不触碰个人/企业积分账本。"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.platform_model_cost_attempt import PlatformModelCostAttempt
from app.services.billing_ledger_service import BillingError

_COSTS_CNY_MICROS_PER_MILLION = {
    # DeepSeek 官方人民币峰时、缓存未命中价；Flash 旧别名仍按官方现行 Flash 价格计费。
    "deepseek-flash": (2_000_000, 8_000_000),
    "deepseek-v4-flash": (2_000_000, 8_000_000),
    "deepseek-v4-flash-vision-exp": (2_000_000, 8_000_000),
    "deepseek-v4-pro": (9_000_000, 27_000_000),
}
_PRICING_VERSION = "deepseek-cny-peak-cache-miss-2026-11-17-v1"
_PRICING_SOURCE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
_PRICING_SOURCE_CHECKED_AT = datetime(2026, 11, 17)
_PROVIDER_IDENTITY_VERSION = "deepseek-platform-cost-v1"
_PROVIDER_NAME = "deepseek"
_PROVIDER_HOST = "api.deepseek.com"
_CNY_MICROS_PER_MILLION_TOKENS = 1_000_000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _model_cost(model_name: str) -> tuple[int, int]:
    rates = _COSTS_CNY_MICROS_PER_MILLION.get(model_name)
    if rates is None:
        raise BillingError("该模型尚无核准的平台供应商价格，已在请求前阻断")
    return rates


def resolve_provider(*, model_name: str, provider_name: str | None, base_url: str | None,
                     credential_source: str | None) -> tuple[int, int]:
    """只允许价格已核对的官方 DeepSeek 托管凭据。"""
    rates = _model_cost(model_name)
    if credential_source == "user":
        raise BillingError("用户自有 Key 由其供应商账号结算，不记为平台成本")
    if credential_source not in {"system", "global"} or provider_name != _PROVIDER_NAME:
        raise BillingError("平台成本只支持已核验的 DeepSeek 托管凭据")
    if not isinstance(base_url, str) or base_url != base_url.strip() or not base_url:
        raise BillingError("平台计费模型端点无效")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise BillingError("平台计费模型端点无效") from exc
    if (parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != _PROVIDER_HOST
            or port not in (None, 443) or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in {"", "/v1"}):
        raise BillingError("平台成本只支持 DeepSeek 官方 HTTPS API 端点")
    return rates


def quote_cost(input_tokens: int, output_tokens: int, input_rate: int, output_rate: int) -> int:
    if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
        raise BillingError("模型 Token 用量无效")
    if any(type(value) is not int or value <= 0 for value in (input_rate, output_rate)):
        raise BillingError("平台模型成本费率无效")
    numerator = input_tokens * input_rate + output_tokens * output_rate
    return (numerator + _CNY_MICROS_PER_MILLION_TOKENS - 1) // _CNY_MICROS_PER_MILLION_TOKENS


def reserve(db: Session, *, request_key: str, actor_user_id: int, run_id: str | None,
            model_name: str, provider_name: str | None, base_url: str | None,
            credential_source: str | None, max_input_tokens: int, max_output_tokens: int,
            now: datetime | None = None) -> PlatformModelCostAttempt:
    if (not request_key or len(request_key) > 160 or type(actor_user_id) is not int or actor_user_id <= 0
            or (run_id is not None and (not isinstance(run_id, str) or not run_id or len(run_id) > 128))
            or type(max_input_tokens) is not int or max_input_tokens <= 0
            or type(max_output_tokens) is not int or max_output_tokens <= 0):
        raise BillingError("平台模型成本预占参数无效")
    input_rate, output_rate = resolve_provider(
        model_name=model_name, provider_name=provider_name,
        base_url=base_url, credential_source=credential_source,
    )
    at = now or _now()
    existing = db.query(PlatformModelCostAttempt).filter_by(request_key=request_key).first()
    if existing is not None:
        raise BillingError("该平台模型请求键已存在，禁止重复发送")
    if run_id is not None:
        unresolved = db.query(PlatformModelCostAttempt.id).filter(
            PlatformModelCostAttempt.run_id == run_id,
            PlatformModelCostAttempt.status == "unknown",
        ).first()
        if unresolved is not None:
            raise BillingError("该小菱运行有结果未知的模型调用，待核对后才能继续")
    hold = quote_cost(max_input_tokens, max_output_tokens, input_rate, output_rate)
    if hold <= 0:
        raise BillingError("平台模型成本上界无效")
    row = PlatformModelCostAttempt(
        request_key=request_key,
        actor_user_id=actor_user_id,
        run_id=run_id,
        model_name=model_name,
        pricing_version=_PRICING_VERSION,
        pricing_source_url=_PRICING_SOURCE_URL,
        pricing_source_checked_at=_PRICING_SOURCE_CHECKED_AT,
        provider_identity_version=_PROVIDER_IDENTITY_VERSION,
        provider_name=_PROVIDER_NAME,
        endpoint_host=_PROVIDER_HOST,
        credential_source=credential_source,
        input_cost_cny_micros_per_million=input_rate,
        output_cost_cny_micros_per_million=output_rate,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        reserved_cost_cny_micros=hold,
        status="reserved",
        created_at=at,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise BillingError("平台模型请求键冲突，禁止重复发送") from exc
    return row


def mark_started(db: Session, request_key: str, *, now: datetime | None = None) -> None:
    row = db.query(PlatformModelCostAttempt).filter_by(request_key=request_key).with_for_update().one_or_none()
    if row is None or row.status != "reserved" or row.provider_attempt_started_at is not None:
        raise BillingError("平台模型成本预占状态无效，已阻断发送")
    row.provider_attempt_started_at = now or _now()
    db.flush()


def mark_not_sent(db: Session, request_key: str) -> None:
    row = db.query(PlatformModelCostAttempt).filter_by(request_key=request_key).with_for_update().one_or_none()
    if row is not None and row.status == "reserved" and row.provider_attempt_started_at is None:
        row.status = "not_sent"
        db.flush()


def mark_unknown(db: Session, request_key: str, *, now: datetime | None = None) -> None:
    row = db.query(PlatformModelCostAttempt).filter_by(request_key=request_key).with_for_update().one_or_none()
    if row is None or row.status in {"settled", "not_sent", "unknown"}:
        return
    row.provider_attempt_started_at = row.provider_attempt_started_at or now or _now()
    row.status = "unknown"
    db.flush()


def settle(db: Session, request_key: str, *, input_tokens: int, output_tokens: int,
           now: datetime | None = None) -> PlatformModelCostAttempt:
    row = db.query(PlatformModelCostAttempt).filter_by(request_key=request_key).with_for_update().one_or_none()
    if row is None:
        raise BillingError("平台模型成本预占不存在")
    if row.status == "settled":
        if (row.actual_input_tokens, row.actual_output_tokens) != (input_tokens, output_tokens):
            raise BillingError("平台模型结算重试与原用量不一致")
        return row
    if row.status == "not_sent":
        raise BillingError("已确认未发送的模型请求不能结算")
    if (type(input_tokens) is not int or type(output_tokens) is not int
            or input_tokens < 0 or output_tokens < 0
            or input_tokens > row.max_input_tokens or output_tokens > row.max_output_tokens):
        raise BillingError("模型实际用量缺失或超出平台成本预占，待核对")
    cost = quote_cost(
        input_tokens, output_tokens,
        int(row.input_cost_cny_micros_per_million), int(row.output_cost_cny_micros_per_million),
    )
    if cost > row.reserved_cost_cny_micros:
        raise BillingError("模型实际成本超过平台成本预占，待核对")
    row.actual_input_tokens = input_tokens
    row.actual_output_tokens = output_tokens
    row.actual_cost_cny_micros = cost
    row.status = "settled"
    row.provider_attempt_started_at = row.provider_attempt_started_at or now or _now()
    row.settled_at = now or _now()
    db.flush()
    return row


def mark_interrupted_run_attempts(db: Session, run_id: str, *, now: datetime | None = None) -> int:
    """Freeze started attempts before a persisted run is recovered after worker loss."""
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        raise BillingError("平台模型恢复运行标识无效")
    rows = (
        db.query(PlatformModelCostAttempt)
        .filter(PlatformModelCostAttempt.run_id == run_id, PlatformModelCostAttempt.status == "reserved")
        .with_for_update()
        .all()
    )
    for row in rows:
        if row.provider_attempt_started_at is None:
            row.status = "not_sent"
        else:
            row.status = "unknown"
            row.provider_attempt_started_at = row.provider_attempt_started_at or now or _now()
    db.flush()
    return len(rows)
