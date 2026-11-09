"""Transactional credit ledger; caller owns commit/rollback and authorization.

Amounts are milli-credits. Calling reserve before every upstream request is
required; this module does not claim coverage of model call sites by itself.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
from math import ceil
from urllib.parse import urlsplit

from sqlalchemy import case, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingLimit,
    BillingReservation,
    BillingReservationLimit,
    BillingReservationPart,
    BillingWallet,
)


class BillingError(ValueError):
    pass


class RetryableWalletSnapshot(BillingError):
    """The wallet changed after the transaction's consistent-read snapshot."""


class InsufficientCredits(BillingError):
    pass


class IdempotencyConflict(BillingError):
    pass


_RATES = {
    # Values are milli-credits per token. Fraction preserves exact half-milli
    # prices while final charges are rounded up to the ledger's integer unit.
    "deepseek-flash": (Fraction(1), Fraction(4)),
    "deepseek-v4-flash": (Fraction(1), Fraction(4)),
    # Official pricing docs retain this legacy vision alias and bill it at Flash rates.
    "deepseek-v4-flash-vision-exp": (Fraction(1), Fraction(4)),
    # The current vendor price sheet lists V4-Pro-0813 at ¥9/¥27 per million.
    # Use 2x peak pricing; never assume the old alias is currently routed to Flash.
    "deepseek-v4-pro": (Fraction(9, 2), Fraction(27, 2)),
}
_BILLABLE_MODELS = frozenset({
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
})
_BILLABLE_RATES = {model: _RATES[model] for model in sorted(_BILLABLE_MODELS)}


def billable_model_names() -> tuple[str, ...]:
    """Models whose current route and provider price are approved for platform billing."""
    return tuple(sorted(_BILLABLE_MODELS))
_PROVIDER_IDENTITY_VERSION = "deepseek-official-v1"
_PROVIDER_NAME = "deepseek"
_PROVIDER_HOST = "api.deepseek.com"
_PROVIDER_CREDENTIAL_SOURCES = frozenset({"system", "global"})


@dataclass(frozen=True)
class BillingProviderIdentity:
    """Sanitized, immutable provider identity; never stores a URL path or secret."""

    identity_version: str
    provider_name: str
    endpoint_host: str
    credential_source: str


def resolve_billing_provider_identity(*, provider_name: str | None, base_url: str | None,
                                      credential_source: str | None, model_name: str
                                      ) -> BillingProviderIdentity:
    """Allow platform billing only for priced models on the official managed API.

    BYOK and administrator-defined compatible endpoints remain usable outside a
    Prism billing scope. Inside one, fail before reserving credits or sending HTTP.
    """
    if credential_source == "user":
        raise BillingError("平台计费仅允许平台托管凭据；用户自有 Key 不进入平台钱包")
    if credential_source not in _PROVIDER_CREDENTIAL_SOURCES:
        raise BillingError("模型凭据来源未列入平台计费白名单")
    if provider_name != _PROVIDER_NAME:
        raise BillingError("当前供应商未通过平台计费审查")
    if model_name not in _BILLABLE_MODELS:
        raise BillingError("该模型的供应商路由或结算价格尚未确认，禁止平台计费")
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        raise BillingError("平台计费模型端点无效")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise BillingError("平台计费模型端点无效") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != _PROVIDER_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise BillingError("平台计费仅允许 DeepSeek 官方 HTTPS API 端点")
    return BillingProviderIdentity(
        identity_version=_PROVIDER_IDENTITY_VERSION,
        provider_name=_PROVIDER_NAME,
        endpoint_host=_PROVIDER_HOST,
        credential_source=credential_source,
    )


def resolve_billing_tool_provider_identity(*, provider_name: str | None, base_url: str | None,
                                           credential_source: str | None
                                           ) -> BillingProviderIdentity:
    """Authorize a fixed-price tool only against the managed DeepSeek HTTPS API."""
    if credential_source not in _PROVIDER_CREDENTIAL_SOURCES:
        raise BillingError("Search/Fetch 平台计费只允许系统或全局托管凭据")
    if provider_name != _PROVIDER_NAME or not isinstance(base_url, str) or not base_url:
        raise BillingError("Search/Fetch 平台计费只允许 DeepSeek 官方 API")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise BillingError("Search/Fetch 平台计费端点无效") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != _PROVIDER_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise BillingError("Search/Fetch 平台计费仅允许 DeepSeek 官方 HTTPS API 端点")
    return BillingProviderIdentity(
        identity_version=_PROVIDER_IDENTITY_VERSION,
        provider_name=_PROVIDER_NAME,
        endpoint_host=_PROVIDER_HOST,
        credential_source=credential_source,
    )


def validate_billing_provider_identity(identity: BillingProviderIdentity | None) -> BillingProviderIdentity:
    if (not isinstance(identity, BillingProviderIdentity)
            or identity.identity_version != _PROVIDER_IDENTITY_VERSION
            or identity.provider_name != _PROVIDER_NAME
            or identity.endpoint_host != _PROVIDER_HOST
            or identity.credential_source not in _PROVIDER_CREDENTIAL_SOURCES):
        raise BillingError("计费供应商身份缺失或未通过平台白名单校验")
    return identity


def _reservation_provider_identity(reservation: BillingReservation) -> BillingProviderIdentity | None:
    values = (
        reservation.provider_identity_version,
        reservation.provider_name,
        reservation.provider_endpoint_host,
        reservation.credential_source,
    )
    if all(value is None for value in values):
        return None
    return validate_billing_provider_identity(BillingProviderIdentity(*values))


def _rate_catalog_version(rates: dict[str, tuple[Fraction, Fraction]]) -> str:
    semantics = (
        "schema=1\n"
        "balance_unit=milli_credit\n"
        "rate_unit=milli_credit_per_token\n"
        "rounding=ceil_per_model_request"
    )
    catalog = "\n".join(
        f"{model}:{input_rate.numerator}/{input_rate.denominator}:"
        f"{output_rate.numerator}/{output_rate.denominator}"
        for model, (input_rate, output_rate) in sorted(rates.items())
    )
    return sha256(f"{semantics}\n{catalog}".encode("ascii")).hexdigest()


RATE_CATALOG_VERSION = _rate_catalog_version(_BILLABLE_RATES)
_MAX_DB_INTEGER = 2**63 - 1


def public_rate_catalog(db: Session | None = None) -> dict[str, dict[str, int | float]]:
    """Expose the active persisted settlement rates when a database is available."""
    if db is not None:
        from app.services import billing_rate_catalog_service

        try:
            return billing_rate_catalog_service.public_pricing(db)["model_milli_per_token"]
        except billing_rate_catalog_service.RateCatalogError as exc:
            raise BillingError(str(exc)) from exc

    def number(rate: Fraction) -> int | float:
        return rate.numerator if rate.denominator == 1 else float(rate)

    return {
        model: {"input": number(rates[0]), "output": number(rates[1])}
        for model, rates in _BILLABLE_RATES.items()
    }


def _rate_snapshot(db: Session, model_name: str, *, now=None, clock=None) -> tuple[str, Fraction, Fraction]:
    from app.services import billing_rate_catalog_service

    try:
        return billing_rate_catalog_service.rate_snapshot(db, model_name, now=now, lock_for_use=True, clock=clock)
    except billing_rate_catalog_service.RateCatalogError as exc:
        raise BillingError(str(exc)) from exc


def quote_catalog_milli(db: Session, model_name: str, input_tokens: int, output_tokens: int,
                        *, now=None, clock=None) -> int:
    """Quote the active DB-backed catalog inside the caller's reservation transaction."""
    _, input_rate, output_rate = _rate_snapshot(db, model_name, now=now, clock=clock)
    return _quote_with_rates((input_rate, output_rate), input_tokens, output_tokens)


def active_pricing_metadata(db: Session, *, now=None) -> dict:
    from app.services import billing_rate_catalog_service

    try:
        return billing_rate_catalog_service.public_pricing(db, now=now)
    except billing_rate_catalog_service.RateCatalogError as exc:
        raise BillingError(str(exc)) from exc


def _legacy_static_rate_snapshot(model_name: str) -> tuple[str, Fraction, Fraction]:
    """Compatibility helper for callers that only need a static model allowlist check."""
    rates = _RATES.get(model_name)
    if rates is None:
        raise BillingError("模型尚未配置计费规则")
    return RATE_CATALOG_VERSION, rates[0], rates[1]


def _reservation_rates(reservation: BillingReservation) -> tuple[Fraction, Fraction] | None:
    values = (
        reservation.pricing_version,
        reservation.input_rate_numerator,
        reservation.input_rate_denominator,
        reservation.output_rate_numerator,
        reservation.output_rate_denominator,
    )
    if all(value is None for value in values):
        return None
    version, input_numerator, input_denominator, output_numerator, output_denominator = values
    if (not isinstance(version, str) or not version or
            any(type(value) is not int or value <= 0 for value in (
                input_numerator, input_denominator, output_numerator, output_denominator,
            ))):
        raise BillingError("预占费率快照损坏，需人工对账")
    return Fraction(input_numerator, input_denominator), Fraction(output_numerator, output_denominator)


def _quote_with_rates(rates: tuple[Fraction, Fraction], prompt_tokens: int, completion_tokens: int) -> int:
    if (type(prompt_tokens) is not int or type(completion_tokens) is not int
            or prompt_tokens < 0 or completion_tokens < 0):
        raise BillingError("Token 数必须为非负整数")
    return ceil(prompt_tokens * rates[0] + completion_tokens * rates[1])


def quote_reservation_milli(db: Session, *, request_key: str, input_tokens: int, output_tokens: int) -> int:
    """Quote provider usage with the immutable rates attached to its reservation."""
    reservation = db.query(BillingReservation).filter_by(request_key=request_key).first()
    if reservation is None:
        raise BillingError("费率快照对应的预占记录不存在")
    if reservation.billable_kind != "model":
        raise BillingError("Search/Fetch reservation 不能按模型 Token 费率报价")
    if _reservation_provider_identity(reservation) is None:
        raise BillingError("预占缺少供应商身份快照，需人工对账")
    rates = _reservation_rates(reservation)
    if rates is None:
        raise BillingError("预占缺少费率快照，需人工对账")
    return _quote_with_rates(rates, input_tokens, output_tokens)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value):
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def quote_milli(model_name: str, prompt_tokens: int, completion_tokens: int) -> int:
    """Conservative public price: no assumption of supplier cache discounts."""
    rates = _RATES.get(model_name)
    if rates is None:
        raise BillingError("模型尚未配置计费规则")
    return _quote_with_rates(rates, prompt_tokens, completion_tokens)


def _lock_wallets(db: Session, wallet_ids: list[int]) -> dict[int, BillingWallet]:
    """Lock in a deterministic order; UPDATE also serializes SQLite writers."""
    found = {}
    for wallet_id in sorted(set(wallet_ids)):
        changed = db.execute(
            update(BillingWallet).where(BillingWallet.id == wallet_id)
            .values(lock_version=BillingWallet.lock_version + 1)
        ).rowcount
        if changed != 1:
            raise BillingError("积分账户不存在")
        found[wallet_id] = (db.query(BillingWallet).filter_by(id=wallet_id)
                            .populate_existing().with_for_update().one())
    return found


def _wallet_snapshot_version(db: Session, wallet_id: int) -> int | None:
    """Read the caller's RR snapshot before acquiring a wallet lock."""
    return db.query(BillingWallet.lock_version).filter_by(id=wallet_id).scalar()


def _require_wallet_snapshot(snapshot_version: int | None, wallet: BillingWallet) -> None:
    # _lock_wallets increments this sequence once. A different value means
    # another wallet mutation committed after our transaction's read snapshot;
    # history must be read again in a fresh transaction rather than guessed.
    if snapshot_version is None or int(wallet.lock_version) != int(snapshot_version) + 1:
        raise RetryableWalletSnapshot("积分账本刚发生变化，请重试")


def _reservation_wallet_ids(reservation: BillingReservation, parts: list[BillingReservationPart]) -> list[int]:
    """Lock both declared payers even when one supplied zero of the hold.

    Zero-company personal fallback still has enterprise limit links; keeping
    the same wallet order as reserve also serializes limit reconciliation.
    """
    return [int(value) for value in (
        *(part.wallet_id for part in parts), reservation.enterprise_wallet_id, reservation.personal_wallet_id,
    ) if value is not None]


def _require_locked_reservation_wallets(reservation, parts, locked_wallets):
    if not set(_reservation_wallet_ids(reservation, parts)).issubset(locked_wallets):
        raise BillingError("预占钱包归属已变化，账单保持冻结")


def get_or_create_wallet(db: Session, owner_type: str, owner_id: int) -> BillingWallet:
    if owner_type not in {"user", "enterprise"} or type(owner_id) is not int or owner_id <= 0:
        raise BillingError("积分账户主体无效")
    wallet = db.query(BillingWallet).filter_by(owner_type=owner_type, owner_id=owner_id).first()
    if wallet is None:
        wallet = BillingWallet(owner_type=owner_type, owner_id=owner_id)
        db.add(wallet)
        db.flush()
    return wallet


def grant(db: Session, *, wallet_id: int, grant_key: str, kind: str,
          amount_milli: int, expires_at=None) -> BillingCreditBatch:
    if (not grant_key or len(grant_key) > 160 or kind not in {"subscription", "purchase", "trial", "permanent"}
            or type(amount_milli) is not int or amount_milli <= 0):
        raise BillingError("积分发放参数无效")
    if kind == "purchase" and expires_at is not None:
        raise BillingError("购买积分不应设置过期时间")
    if kind != "purchase" and expires_at is None:
        raise BillingError("赠送积分必须设置过期时间")
    expires_at = _naive_utc(expires_at)
    _lock_wallets(db, [wallet_id])
    old = db.query(BillingCreditBatch).filter_by(grant_key=grant_key).first()
    if old:
        if (old.wallet_id, old.kind, old.issued_milli, old.expires_at) != (wallet_id, kind, amount_milli, expires_at):
            raise IdempotencyConflict("积分发放键与原请求不一致")
        return old
    batch = BillingCreditBatch(wallet_id=wallet_id, grant_key=grant_key, kind=kind,
                               issued_milli=amount_milli, remaining_milli=amount_milli,
                               reserved_milli=0, expires_at=expires_at)
    db.add(batch)
    db.flush()
    return batch


def available_milli(db: Session, wallet_id: int, now=None, *, lock: bool = False) -> int:
    now = _naive_utc(now) if now is not None else _now()
    query = db.query(BillingCreditBatch).filter(
        BillingCreditBatch.wallet_id == wallet_id,
        or_(BillingCreditBatch.expires_at.is_(None), BillingCreditBatch.expires_at > now),
    ).order_by(BillingCreditBatch.id)
    if lock:
        # Callers first lock the wallet. A MySQL RR snapshot and already-loaded
        # ORM instances must not hide a committed hold on that wallet.
        query = query.populate_existing().with_for_update()
    return sum(max(0, b.remaining_milli - b.reserved_milli - b.refund_reserved_milli) for b in query.all())


def _limits(db, enterprise_id, group_id, user_id, now, usage_kind, enterprise_wallet_id,
            enterprise_wallet_snapshot, enterprise_wallet):
    if enterprise_id is None:
        return []
    query = db.query(BillingLimit).filter(
        BillingLimit.enterprise_id == enterprise_id,
        BillingLimit.category == ("auto" if usage_kind == "enterprise_auto" else "normal"),
        BillingLimit.period_start <= now, BillingLimit.period_end > now,
        or_(BillingLimit.group_id.is_(None), BillingLimit.group_id == group_id),
        or_(BillingLimit.user_id.is_(None), BillingLimit.user_id == user_id),
    )
    rows = query.order_by(BillingLimit.id).populate_existing().with_for_update().all()
    if usage_kind == "member":
        # Older limits may have been created after the month's usage. Repair
        # them under the already-held wallet lock before any new reservation.
        from app.services.billing_quota_service import _reconcile_limit

        for row in rows:
            if row.accounting_version not in {0, 1}:
                raise BillingError("额度账本版本无法识别，禁止预占")
            if row.accounting_version == 0:
                _require_wallet_snapshot(enterprise_wallet_snapshot, enterprise_wallet)
                used, held = _reconcile_limit(db, row, int(enterprise_wallet_id))
                row.used_milli, row.reserved_milli, row.accounting_version = used, held, 1
    return rows


def reserve(db: Session, *, request_key: str, model_name: str, user_id: int | None, max_milli: int,
            enterprise_wallet_id=None, personal_wallet_id=None, enterprise_id=None,
            group_id=None, allow_personal=False, usage_kind="member", require_new=False,
            provider_identity: BillingProviderIdentity | None = None, now=None, clock=None) -> BillingReservation:
    """Reserve before sending; enterprise limits apply only to enterprise-funded units.

    For enterprise chat pass allow_personal=False. Caller validates membership,
    selected group, personal fallback consent and max-token estimate first.
    """
    now = _naive_utc(now) if now is not None else _now()
    if model_name not in _BILLABLE_MODELS or not request_key or len(request_key) > 160:
        raise BillingError("请求或计费模型无效")
    provider_identity = validate_billing_provider_identity(provider_identity)
    pricing_version, input_rate, output_rate = _rate_snapshot(db, model_name, now=now, clock=clock)
    if usage_kind not in {"member", "enterprise_auto"}:
        raise BillingError("用量归属无效")
    if usage_kind == "member" and (type(user_id) is not int or user_id <= 0):
        raise BillingError("请求用户无效")
    if usage_kind == "enterprise_auto" and (user_id is not None or personal_wallet_id is not None or allow_personal):
        raise BillingError("企业自主用量不得计入员工或个人账户")
    if usage_kind == "enterprise_auto" and group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise BillingError("企业自主用量小组归属无效")
    if type(max_milli) is not int or max_milli <= 0:
        raise BillingError("请求用户或预占额度无效")
    if (enterprise_wallet_id is None) != (enterprise_id is None):
        raise BillingError("企业账户与企业身份不匹配")
    if usage_kind == "enterprise_auto" and enterprise_wallet_id is None:
        raise BillingError("企业自主用量需要企业账户")
    if enterprise_wallet_id is None and group_id is not None:
        raise BillingError("个人请求不能包含企业小组")
    ids = [x for x in (enterprise_wallet_id, personal_wallet_id) if x is not None]
    if not ids or len(ids) != len(set(ids)):
        raise BillingError("未提供有效积分账户")
    enterprise_wallet_snapshot = (_wallet_snapshot_version(db, enterprise_wallet_id)
                                  if enterprise_wallet_id is not None else None)
    wallets = _lock_wallets(db, ids)
    if enterprise_wallet_id is not None:
        w = wallets[enterprise_wallet_id]
        if (w.owner_type, w.owner_id) != ("enterprise", enterprise_id):
            raise BillingError("企业账户归属不匹配")
    if personal_wallet_id is not None:
        w = wallets[personal_wallet_id]
        if (w.owner_type, w.owner_id) != ("user", user_id):
            raise BillingError("个人账户归属不匹配")
    old = db.query(BillingReservation).filter_by(request_key=request_key).first()
    if old:
        if require_new:
            raise IdempotencyConflict("调用已存在，禁止重复发送模型请求")
        if (old.user_id, old.model_name, old.enterprise_id, old.group_id, old.requested_milli,
                old.enterprise_wallet_id, old.personal_wallet_id, bool(old.allow_personal), old.usage_kind,
                old.provider_identity_version, old.provider_name, old.provider_endpoint_host,
                old.credential_source,
                old.pricing_version, old.input_rate_numerator, old.input_rate_denominator,
                old.output_rate_numerator, old.output_rate_denominator) != (
                user_id, model_name, enterprise_id, group_id, max_milli,
                enterprise_wallet_id, personal_wallet_id, allow_personal, usage_kind,
                provider_identity.identity_version, provider_identity.provider_name,
                provider_identity.endpoint_host, provider_identity.credential_source, pricing_version,
                input_rate.numerator, input_rate.denominator,
                output_rate.numerator, output_rate.denominator):
            raise IdempotencyConflict("预占键与原请求不一致")
        return old
    limits = _limits(db, enterprise_id, group_id, user_id, now, usage_kind, enterprise_wallet_id,
                     enterprise_wallet_snapshot, wallets.get(enterprise_wallet_id))
    if usage_kind == "enterprise_auto" and not limits:
        raise BillingError("企业主动服务预算尚未配置")
    company_cap = min([max_milli] + [max(0, x.limit_milli - x.used_milli - x.reserved_milli) for x in limits])
    company_amount = (min(company_cap, available_milli(db, enterprise_wallet_id, now, lock=True))
                      if enterprise_wallet_id else 0)
    personal_amount = max_milli - company_amount
    if enterprise_wallet_id is not None and not allow_personal and personal_amount:
        raise InsufficientCredits("企业积分或额度不足")
    if personal_amount and (
        personal_wallet_id is None or available_milli(db, personal_wallet_id, now, lock=True) < personal_amount
    ):
        raise InsufficientCredits("积分或额度不足")
    res = BillingReservation(request_key=request_key, model_name=model_name,
                             provider_identity_version=provider_identity.identity_version,
                             provider_name=provider_identity.provider_name,
                             provider_endpoint_host=provider_identity.endpoint_host,
                             credential_source=provider_identity.credential_source,
                             pricing_version=pricing_version,
                             input_rate_numerator=input_rate.numerator,
                             input_rate_denominator=input_rate.denominator,
                             output_rate_numerator=output_rate.numerator,
                             output_rate_denominator=output_rate.denominator,
                             enterprise_id=enterprise_id,
                             group_id=group_id, user_id=user_id, requested_milli=max_milli, status="reserved",
                             enterprise_wallet_id=enterprise_wallet_id, personal_wallet_id=personal_wallet_id,
                             allow_personal=int(allow_personal), usage_kind=usage_kind, created_at=now)
    db.add(res)
    db.flush()
    for wallet_id, amount in ((enterprise_wallet_id, company_amount), (personal_wallet_id, personal_amount)):
        if not amount:
            continue
        batches = (db.query(BillingCreditBatch).filter(
            BillingCreditBatch.wallet_id == wallet_id,
            or_(BillingCreditBatch.expires_at.is_(None), BillingCreditBatch.expires_at > now),
        ).order_by(case((BillingCreditBatch.expires_at.is_(None), 1), else_=0),
                   BillingCreditBatch.expires_at, BillingCreditBatch.id).populate_existing().with_for_update().all())
        for batch in batches:
            take = min(amount, batch.remaining_milli - batch.reserved_milli - batch.refund_reserved_milli)
            if take <= 0:
                continue
            batch.reserved_milli += take
            db.add(BillingReservationPart(reservation_id=res.id, wallet_id=wallet_id,
                                          batch_id=batch.id, reserved_milli=take))
            amount -= take
            if not amount:
                break
        if amount:
            raise InsufficientCredits("积分预占失败")
    for limit in limits:
        limit.reserved_milli += company_amount
        db.add(BillingReservationLimit(reservation_id=res.id, limit_id=limit.id, reserved_milli=company_amount))
    db.flush()
    return res


def reserve_tool(db: Session, *, request_key: str, tool_name: str, user_id: int,
                 enterprise_id: int, enterprise_wallet_id: int, group_id: int | None,
                 provider_identity: BillingProviderIdentity, now=None, clock=None) -> BillingReservation:
    """Reserve one approved Search/Fetch request from the enterprise wallet only."""
    from app.services import billing_rate_catalog_service

    at = _naive_utc(now) if now is not None else _now()
    if tool_name not in {"search", "fetch"} or not request_key or len(request_key) > 160:
        raise BillingError("工具预占请求无效")
    if type(user_id) is not int or user_id <= 0 or type(enterprise_id) is not int or enterprise_id <= 0:
        raise BillingError("工具预占的企业成员身份无效")
    if type(enterprise_wallet_id) is not int or enterprise_wallet_id <= 0:
        raise BillingError("工具调用必须使用有效企业钱包")
    if group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise BillingError("工具调用小组范围无效")
    provider_identity = validate_billing_provider_identity(provider_identity)
    try:
        pricing_version, supplier_cost, price = billing_rate_catalog_service.tool_rate_snapshot(
            db, tool_name, now=at, lock_for_use=True, clock=clock,
        )
    except billing_rate_catalog_service.RateCatalogError as exc:
        raise BillingError(str(exc)) from exc
    if type(supplier_cost) is not int or supplier_cost <= 0 or type(price) is not int or price <= 0:
        raise BillingError("已批准的工具成本或用户费率无效")
    if db.query(BillingReservation.id).filter_by(request_key=request_key).first() is not None:
        raise IdempotencyConflict("工具调用键已存在，禁止重复创建 reservation 或再次外呼")

    enterprise_wallet_snapshot = _wallet_snapshot_version(db, enterprise_wallet_id)
    wallets = _lock_wallets(db, [enterprise_wallet_id])
    wallet = wallets[enterprise_wallet_id]
    if (wallet.owner_type, wallet.owner_id) != ("enterprise", enterprise_id):
        raise BillingError("工具调用企业钱包归属不匹配")
    limits = _limits(db, enterprise_id, group_id, user_id, at, "member", enterprise_wallet_id,
                     enterprise_wallet_snapshot, wallet)
    company_cap = min(
        [price] + [max(0, row.limit_milli - row.used_milli - row.reserved_milli) for row in limits]
    )
    available = available_milli(db, enterprise_wallet_id, at, lock=True)
    if company_cap < price or available < price:
        raise InsufficientCredits("企业积分或适用月额度不足，未创建工具 reservation")

    reservation = BillingReservation(
        request_key=request_key,
        model_name=f"deepseek-{tool_name}",
        billable_kind=tool_name,
        tool_name=tool_name,
        tool_cost_cny_micros_per_success=supplier_cost,
        tool_price_milli_per_success=price,
        tool_successful_calls=None,
        provider_attempt_started_at=None,
        provider_identity_version=provider_identity.identity_version,
        provider_name=provider_identity.provider_name,
        provider_endpoint_host=provider_identity.endpoint_host,
        credential_source=provider_identity.credential_source,
        pricing_version=pricing_version,
        enterprise_id=enterprise_id,
        group_id=group_id,
        user_id=user_id,
        usage_kind="member",
        enterprise_wallet_id=enterprise_wallet_id,
        personal_wallet_id=None,
        allow_personal=0,
        requested_milli=price,
        status="reserved",
        usage_state="pending",
        created_at=at,
    )
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError as exc:
        # A missing unique-key SELECT FOR UPDATE takes a gap lock in MySQL RR.
        # Let the unique index arbitrate concurrent inserts, and fail closed.
        # The transaction is invalid after flush failure; the caller must roll back.
        original = exc.orig
        args = getattr(original, "args", ())
        duplicate_mysql = bool(args and args[0] == 1062 and "uq_billing_reservation_request" in str(original))
        duplicate_sqlite = "UNIQUE constraint failed: billing_reservation.request_key" in str(original)
        if duplicate_mysql or duplicate_sqlite:
            raise IdempotencyConflict("工具调用键已由并发请求创建，禁止再次外呼") from exc
        raise
    remaining = price
    batches = (db.query(BillingCreditBatch).filter(
        BillingCreditBatch.wallet_id == enterprise_wallet_id,
        or_(BillingCreditBatch.expires_at.is_(None), BillingCreditBatch.expires_at > at),
    ).order_by(case((BillingCreditBatch.expires_at.is_(None), 1), else_=0),
               BillingCreditBatch.expires_at, BillingCreditBatch.id).populate_existing().with_for_update().all())
    for batch in batches:
        take = min(remaining, batch.remaining_milli - batch.reserved_milli - batch.refund_reserved_milli)
        if take <= 0:
            continue
        batch.reserved_milli += take
        db.add(BillingReservationPart(
            reservation_id=reservation.id, wallet_id=enterprise_wallet_id,
            batch_id=batch.id, reserved_milli=take,
        ))
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        raise InsufficientCredits("企业积分批次预占失败")
    for limit in limits:
        limit.reserved_milli += price
        db.add(BillingReservationLimit(
            reservation_id=reservation.id, limit_id=limit.id, reserved_milli=price,
        ))
    db.flush()
    return reservation


def mark_tool_attempt_started(db: Session, request_key: str, *, now=None) -> BillingReservation:
    """Persist this one-way send marker, then require the caller to commit before HTTP."""
    at = _naive_utc(now) if now is not None else _now()
    row = db.query(BillingReservation).filter_by(request_key=request_key).first()
    if row is None or row.billable_kind not in {"search", "fetch"}:
        raise BillingError("Search/Fetch reservation 不存在")
    parts = db.query(BillingReservationPart).filter_by(
        reservation_id=row.id,
    ).order_by(BillingReservationPart.id).all()
    _lock_wallets(db, [int(part.wallet_id) for part in parts])
    reservation = (db.query(BillingReservation).filter_by(request_key=request_key)
                   .populate_existing().with_for_update().one())
    parts = (db.query(BillingReservationPart).filter_by(reservation_id=reservation.id)
             .order_by(BillingReservationPart.id).populate_existing().with_for_update().all())
    if reservation.status != "reserved" or reservation.provider_attempt_started_at is not None:
        raise IdempotencyConflict("工具调用已开始或已结束，禁止再次发送")
    if reservation.tool_successful_calls is not None:
        raise BillingError("工具成功次数已经写入，不能重新发送")
    if any((reservation.input_tokens is not None, reservation.output_tokens is not None,
            reservation.server_tool_search_calls is not None, reservation.server_tool_fetch_calls is not None)):
        raise BillingError("工具 reservation 混入模型 Token 或供应商内置工具计数，不能发送")
    if (reservation.tool_name != reservation.billable_kind
            or type(reservation.tool_cost_cny_micros_per_success) is not int
            or reservation.tool_cost_cny_micros_per_success <= 0
            or type(reservation.tool_price_milli_per_success) is not int
            or reservation.tool_price_milli_per_success <= 0
            or reservation.requested_milli != reservation.tool_price_milli_per_success
            or not reservation.pricing_version
            or _reservation_provider_identity(reservation) is None):
        raise BillingError("工具费率或供应商快照损坏，不能发送")
    reservation.provider_attempt_started_at = at
    db.flush()
    return reservation


def settle_tool(db: Session, *, request_key: str, tool_successful_calls: int,
                nonbillable: bool = False, now=None) -> BillingReservation:
    """Settle one tool attempt from its immutable unit-price snapshot."""
    at = _naive_utc(now) if now is not None else _now()
    if type(tool_successful_calls) is not int or tool_successful_calls not in {0, 1}:
        raise BillingError("Search/Fetch 一次 reservation 的成功次数只能为 0 或 1")
    if type(nonbillable) is not bool or (nonbillable and tool_successful_calls != 0):
        raise BillingError("明确非计费结果必须对应零次成功")
    row = db.query(BillingReservation).filter_by(request_key=request_key).first()
    if row is None or row.billable_kind not in {"search", "fetch"}:
        raise BillingError("Search/Fetch reservation 不存在")
    parts = (db.query(BillingReservationPart).filter_by(reservation_id=row.id)
             .order_by(BillingReservationPart.id).all())
    _lock_wallets(db, [int(part.wallet_id) for part in parts])
    reservation = (db.query(BillingReservation).filter_by(request_key=request_key)
                   .populate_existing().with_for_update().one())
    parts = (db.query(BillingReservationPart).filter_by(reservation_id=reservation.id)
             .order_by(BillingReservationPart.id).populate_existing().with_for_update().all())
    terminal = "released" if nonbillable else "settled"
    terminal_usage = "not_billable" if nonbillable else "reported"
    if reservation.status == terminal and reservation.usage_state == terminal_usage:
        expected_actual = (
            int(reservation.tool_price_milli_per_success or 0) * tool_successful_calls
        )
        if (reservation.tool_successful_calls == tool_successful_calls
                and reservation.actual_milli == expected_actual):
            return reservation
        raise IdempotencyConflict("工具结算键与已记录结果不一致")
    if reservation.status not in {"reserved", "unknown"} or reservation.actual_milli is not None:
        raise BillingError("工具预占状态不允许结算")
    if reservation.provider_attempt_started_at is None:
        raise BillingError("工具尚无已提交的 provider attempt，不能报告外呼结果")
    if _reservation_provider_identity(reservation) is None:
        raise BillingError("工具预占缺少可信供应商身份快照")
    if (reservation.tool_name != reservation.billable_kind
            or type(reservation.tool_cost_cny_micros_per_success) is not int
            or reservation.tool_cost_cny_micros_per_success <= 0
            or type(reservation.tool_price_milli_per_success) is not int
            or reservation.tool_price_milli_per_success <= 0
            or reservation.pricing_version is None
            or reservation.requested_milli != reservation.tool_price_milli_per_success):
        raise BillingError("工具费率快照损坏，预占保持冻结并进入人工对账")
    if any((reservation.input_tokens is not None, reservation.output_tokens is not None,
            reservation.server_tool_search_calls is not None, reservation.server_tool_fetch_calls is not None)):
        raise BillingError("工具 reservation 混入了模型 Token 或供应商内置工具计数")
    if (reservation.personal_wallet_id is not None or not parts
            or sum(int(part.reserved_milli) for part in parts) != reservation.requested_milli
            or any(int(part.wallet_id) != reservation.enterprise_wallet_id for part in parts)):
        raise BillingError("工具预占批次不完整或不属于企业钱包")
    actual = reservation.tool_price_milli_per_success * tool_successful_calls
    remaining = actual
    company_charged = 0
    for part in parts:
        batch = (db.query(BillingCreditBatch).filter_by(id=int(part.batch_id))
                 .populate_existing().with_for_update().one())
        charge = min(remaining, int(part.reserved_milli))
        if batch.reserved_milli < part.reserved_milli or batch.remaining_milli < charge:
            raise BillingError("工具预占积分批次账本不一致")
        batch.reserved_milli -= int(part.reserved_milli)
        batch.remaining_milli -= charge
        part.charged_milli = charge
        company_charged += charge
        remaining -= charge
    if remaining:
        raise BillingError("工具 reservation 明细不足以覆盖原快照结算")
    for item in (db.query(BillingReservationLimit).filter_by(reservation_id=reservation.id)
                 .order_by(BillingReservationLimit.id).populate_existing().with_for_update().all()):
        limit = (db.query(BillingLimit).filter_by(id=int(item.limit_id))
                 .populate_existing().with_for_update().one())
        if limit.reserved_milli < item.reserved_milli:
            raise BillingError("企业工具月额度预占账本不一致")
        limit.reserved_milli -= int(item.reserved_milli)
        limit.used_milli += company_charged
    reservation.tool_successful_calls = tool_successful_calls
    reservation.actual_milli = actual
    reservation.status = terminal
    reservation.usage_state = terminal_usage
    reservation.settled_at = at
    db.flush()
    return reservation


def release_tool_nonbillable(db: Session, request_key: str, *, now=None) -> BillingReservation:
    """Release a sent tool call only after a definitive provider nonbillable response."""
    return settle_tool(db, request_key=request_key, tool_successful_calls=0, nonbillable=True, now=now)


def settle(db: Session, *, request_key: str, actual_milli: int, input_tokens=None,
           output_tokens=None, server_tool_search_calls=None, server_tool_fetch_calls=None,
           now=None) -> BillingReservation:
    """Settle only trusted usage priced by the reservation snapshot.

    Unknown or legacy rows without a usable rate snapshot stay frozen until a
    separate audited reconciliation path is implemented.
    """
    now = _naive_utc(now) if now is not None else _now()
    if type(actual_milli) is not int or actual_milli < 0:
        raise BillingError("实际积分无效")
    if (server_tool_search_calls is None) != (server_tool_fetch_calls is None):
        raise BillingError("服务端工具用量必须同时提供")
    if server_tool_search_calls is not None and (
        type(server_tool_search_calls) is not int or type(server_tool_fetch_calls) is not int
        or not 0 <= server_tool_search_calls <= _MAX_DB_INTEGER
        or not 0 <= server_tool_fetch_calls <= _MAX_DB_INTEGER
    ):
        raise BillingError("服务端工具用量必须为非负整数")
    if ((server_tool_search_calls or 0) > 0 or (server_tool_fetch_calls or 0) > 0):
        raise BillingError("Search/Fetch 尚未计价，不能自动结算")
    res = db.query(BillingReservation).filter_by(request_key=request_key).first()
    if res is None:
        raise BillingError("预占记录不存在")
    if res.billable_kind != "model":
        raise BillingError("Search/Fetch reservation 必须使用工具费率结算")
    parts = db.query(BillingReservationPart).filter_by(reservation_id=res.id).order_by(BillingReservationPart.id).all()
    locked_wallets = _lock_wallets(db, _reservation_wallet_ids(res, parts))
    # Match reserve's wallet-first locking order to avoid deadlocks.
    res = db.query(BillingReservation).filter_by(request_key=request_key).populate_existing().with_for_update().one()
    parts = (db.query(BillingReservationPart).filter_by(reservation_id=res.id)
             .order_by(BillingReservationPart.id).populate_existing().with_for_update().all())
    _require_locked_reservation_wallets(res, parts, locked_wallets)
    if res.status == "settled":
        if (res.actual_milli != actual_milli or (input_tokens is not None and
                (res.input_tokens, res.output_tokens) != (input_tokens, output_tokens))
                or (server_tool_search_calls is not None and
                    (res.server_tool_search_calls, res.server_tool_fetch_calls) !=
                    (server_tool_search_calls, server_tool_fetch_calls))):
            raise IdempotencyConflict("结算键与原请求不一致")
        return res
    if res.status not in {"reserved", "unknown"} or actual_milli > res.requested_milli:
        raise BillingError("结算状态无效或实际用量超出预占")
    if _reservation_provider_identity(res) is None:
        raise BillingError("预占缺少供应商身份快照，需人工对账")
    if ((res.server_tool_search_calls or 0) > 0 or (res.server_tool_fetch_calls or 0) > 0):
        raise BillingError("已记录未计价 Search/Fetch 用量，保留预占并等待人工对账")
    if input_tokens is None or output_tokens is None:
        raise BillingError("自动结算必须提供输入和输出 Token 用量")
    rates = _reservation_rates(res)
    if rates is None:
        raise BillingError("预占缺少费率快照，需人工对账")
    if _quote_with_rates(rates, input_tokens, output_tokens) != actual_milli:
        raise BillingError("实际积分与供应商用量不一致")
    remaining = actual_milli
    company_charged = 0
    for part in parts:
        batch = (db.query(BillingCreditBatch).filter_by(id=part.batch_id)
                 .populate_existing().with_for_update().one())
        charge = min(remaining, part.reserved_milli)
        if batch.reserved_milli < part.reserved_milli or batch.remaining_milli < charge:
            raise BillingError("积分账本不一致")
        batch.reserved_milli -= part.reserved_milli
        batch.remaining_milli -= charge
        part.charged_milli = charge
        wallet = db.query(BillingWallet).filter_by(id=part.wallet_id).one()
        if wallet.owner_type == "enterprise":
            company_charged += charge
        remaining -= charge
    if remaining:
        raise BillingError("预占明细不足")
    for item in (db.query(BillingReservationLimit).filter_by(reservation_id=res.id)
                 .order_by(BillingReservationLimit.id).populate_existing().with_for_update().all()):
        limit = (db.query(BillingLimit).filter_by(id=item.limit_id)
                 .populate_existing().with_for_update().one())
        if limit.reserved_milli < item.reserved_milli:
            raise BillingError("企业额度账本不一致")
        limit.reserved_milli -= item.reserved_milli
        limit.used_milli += company_charged
    res.status = "settled"
    res.actual_milli = actual_milli
    res.input_tokens = input_tokens
    res.output_tokens = output_tokens
    if server_tool_search_calls is not None:
        res.server_tool_search_calls = server_tool_search_calls
        res.server_tool_fetch_calls = server_tool_fetch_calls
    res.usage_state = "reported" if input_tokens is not None else "unknown"
    res.settled_at = now
    db.flush()
    return res


def mark_unknown(db: Session, request_key: str, *, server_tool_search_calls=None,
                 server_tool_fetch_calls=None) -> BillingReservation:
    if (server_tool_search_calls is None) != (server_tool_fetch_calls is None):
        raise BillingError("服务端工具用量必须同时提供")
    if server_tool_search_calls is not None and (
        type(server_tool_search_calls) is not int or type(server_tool_fetch_calls) is not int
        or not 0 <= server_tool_search_calls <= _MAX_DB_INTEGER
        or not 0 <= server_tool_fetch_calls <= _MAX_DB_INTEGER
    ):
        raise BillingError("服务端工具用量必须为非负整数")
    res = (db.query(BillingReservation).filter_by(request_key=request_key)
           .populate_existing().with_for_update().first())
    if res is None or res.status not in {"reserved", "unknown"}:
        raise BillingError("预占状态无效")
    if res.billable_kind in {"search", "fetch"}:
        if server_tool_search_calls is not None or server_tool_fetch_calls is not None:
            raise BillingError("独立 Search/Fetch reservation 不得记录供应商内置工具计数")
        if res.provider_attempt_started_at is None:
            raise BillingError("未提交 provider attempt 的工具请求不能标记为 unknown")
    if server_tool_search_calls is not None:
        old_counts = (res.server_tool_search_calls, res.server_tool_fetch_calls)
        new_counts = (server_tool_search_calls, server_tool_fetch_calls)
        if old_counts != (None, None) and old_counts != new_counts:
            raise IdempotencyConflict("未知用量记录与已有 Search/Fetch 计数不一致")
        res.server_tool_search_calls, res.server_tool_fetch_calls = new_counts
    res.status = "unknown"
    res.usage_state = "unknown"
    db.flush()
    return res


def release_not_sent(db: Session, request_key: str, *, now=None) -> BillingReservation:
    """Release a reservation only when the caller proves the provider was never invoked."""
    return _release_not_sent(db, request_key, allowed_states={"reserved"}, now=now)


def release_verified_not_sent(db: Session, request_key: str, *, now=None) -> BillingReservation:
    """Release a possibly-sent reservation only after an audited two-person review proves no send."""
    return _release_not_sent(db, request_key, allowed_states={"reserved", "unknown"}, now=now)


def _release_not_sent(db: Session, request_key: str, *, allowed_states: set[str], now=None) -> BillingReservation:
    now = _naive_utc(now) if now is not None else _now()
    res = db.query(BillingReservation).filter_by(request_key=request_key).first()
    if res is None:
        raise BillingError("待释放预占不存在")
    parts = db.query(BillingReservationPart).filter_by(
        reservation_id=int(res.id),
    ).order_by(BillingReservationPart.id).all()
    locked_wallets = _lock_wallets(db, _reservation_wallet_ids(res, parts))
    # Match reserve/settle wallet-first locking order.
    res = db.query(BillingReservation).filter_by(
        request_key=request_key,
    ).populate_existing().with_for_update().one()
    parts = (db.query(BillingReservationPart).filter_by(reservation_id=res.id)
             .order_by(BillingReservationPart.id).populate_existing().with_for_update().all())
    _require_locked_reservation_wallets(res, parts, locked_wallets)
    if res.status == "released" and res.usage_state == "not_sent":
        return res
    if res.status not in allowed_states or res.actual_milli is not None:
        raise BillingError("当前预占状态不允许按未发送证据释放")
    if any((
        res.input_tokens is not None,
        res.output_tokens is not None,
        res.server_tool_search_calls is not None,
        res.server_tool_fetch_calls is not None,
    )):
        raise BillingError("预占已记录供应商用量，不能按未发送证据释放")
    if res.provider_attempt_started_at is not None:
        raise BillingError("provider attempt 已持久化；不能再按未发送释放")

    for part in parts:
        batch = db.query(BillingCreditBatch).filter_by(
            id=int(part.batch_id),
        ).populate_existing().with_for_update().one()
        if batch.reserved_milli < part.reserved_milli or part.charged_milli is not None:
            raise BillingError("预占批次账本不一致，不能释放")
        batch.reserved_milli -= int(part.reserved_milli)
    for item in db.query(BillingReservationLimit).filter_by(
        reservation_id=int(res.id),
    ).order_by(BillingReservationLimit.id).populate_existing().with_for_update().all():
        limit = db.query(BillingLimit).filter_by(
            id=int(item.limit_id),
        ).populate_existing().with_for_update().one()
        if limit.reserved_milli < item.reserved_milli:
            raise BillingError("企业额度预占账本不一致，不能释放")
        limit.reserved_milli -= int(item.reserved_milli)
    res.status = "released"
    res.usage_state = "not_sent"
    res.settled_at = now
    db.flush()
    return res
