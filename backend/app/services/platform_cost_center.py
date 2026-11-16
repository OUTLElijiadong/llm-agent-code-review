"""Detached platform-cost reservation foundation, disabled and not wired to sending.

Caller owns the outer transaction. This module creates no budget or migration,
performs no network I/O, and provides no release or settlement operation.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing_ledger import BillingRateCatalog, BillingRateCatalogItem
from app.models.platform_cost_center import (
    PlatformCostAttempt,
    PlatformCostBudget,
    PlatformCostCenter,
    PlatformCostSettlement,
)
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service, billing_runtime_gate, rbac_service

# Deliberately not controlled by configuration or connected to any production caller.
_FOUNDATION_ENABLED = False
_MAX_INTEGER = 2**63 - 1
_KEY_FACTORY = object()


@dataclass(frozen=True)
class AttemptKey:
    """Opaque internal value; not an API authentication or owner credential."""

    _value: bytes = field(repr=False)
    _factory: InitVar[object]

    def __post_init__(self, _factory: object) -> None:
        if _factory is not _KEY_FACTORY:
            raise ValueError('尝试标识只能由服务端生成')


def new_attempt_key() -> AttemptKey:
    return AttemptKey(secrets.token_bytes(16), _KEY_FACTORY)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _require_enabled() -> None:
    if not _FOUNDATION_ENABLED:
        raise ledger.BillingError('平台成本基础尚未启用')


def _require_no_pending_orm_changes(db: Session) -> None:
    # Locking current-reads use populate_existing and must not silently replace
    # an unflushed caller's authorization, budget, or other ORM changes.
    if db.new or db.dirty or db.deleted:
        raise ledger.BillingError('调用者会话存在未提交的 ORM 变更')


def _start_write_transaction(db: Session) -> None:
    # SQLite SELECT has no row lock; acquire the writer before any authorization
    # or budget reads. Never commit or roll back the caller's outer transaction.
    connection = db.connection()
    if connection.dialect.name != 'sqlite':
        return
    if connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('UPDATE platform_cost_budget SET status = status WHERE 0')
    else:
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def _require_admin(db: Session, actor_id: int) -> None:
    if type(actor_id) is not int or actor_id <= 0 or not rbac_service.is_admin_user(db, actor_id, lock=True):
        raise ledger.BillingError('当前账号没有平台管理员权限')


def _key_bytes(attempt_key: AttemptKey) -> bytes:
    if not isinstance(attempt_key, AttemptKey) or not isinstance(attempt_key._value, bytes):
        raise ledger.BillingError('模型尝试标识无效')
    if len(attempt_key._value) != 16:
        raise ledger.BillingError('模型尝试标识无效')
    return attempt_key._value


def _payload_bounds(payload: Mapping, model_name: str) -> tuple[int, int, bytes]:
    if not isinstance(payload, Mapping) or payload.get('model') != model_name:
        raise ledger.BillingError('模型请求与可信模型不一致')
    if {'max_tokens', 'n'}.intersection(payload):
        raise ledger.BillingError('模型请求包含不支持的输出上界')
    if payload.get('tools') not in (None, []) or billing_runtime_gate._STATEFUL_REQUEST_FIELDS.intersection(payload):
        raise ledger.BillingError('平台成本基础尚不支持服务端工具或状态化请求')
    output_limit = payload.get('max_output_tokens')
    if type(output_limit) is not int or not 0 < output_limit <= settings.deepseek_max_output_tokens:
        raise ledger.BillingError('模型输出上界无效')
    input_limit = billing_runtime_gate._request_input_limit(
        payload, int(settings.deepseek_context_window_tokens), model_name=model_name,
    )
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
    ).encode('utf-8')
    return input_limit, output_limit, hashlib.sha256(raw).digest()


def _cost_snapshot(db: Session, model_name: str, now: datetime):
    # Reuse the existing allowlist, active catalogue, approvals and source-age checks.
    try:
        version, _, _ = billing_rate_catalog_service.rate_snapshot(db, model_name, now=now, lock_for_use=True)
    except billing_rate_catalog_service.RateCatalogError as exc:
        raise ledger.BillingError(str(exc)) from exc
    catalog = db.query(BillingRateCatalog).filter_by(version_key=version).one_or_none()
    item = (db.query(BillingRateCatalogItem).filter_by(catalog_id=catalog.id, model_name=model_name).one_or_none()
            if catalog is not None else None)
    if item is None or any(type(value) is not int or value <= 0 for value in (
        item.provider_input_cny_micros_per_million, item.provider_output_cny_micros_per_million,
    )):
        raise ledger.BillingError('供应商人民币成本未核验')
    return version, item.provider_input_cny_micros_per_million, item.provider_output_cny_micros_per_million


def _assert_budget_integrity(db: Session, budget: PlatformCostBudget) -> None:
    """Under the budget lock, compare synthetic details with its aggregates.

    This intentionally locks all same-budget history. It is test-only and does
    not prove provider provenance, external-writer isolation, or scalability.
    """
    attempts = (db.query(PlatformCostAttempt).filter_by(budget_id=budget.id)
                .order_by(PlatformCostAttempt.id).populate_existing().with_for_update().all())
    receipts = (db.query(PlatformCostSettlement)
                .join(PlatformCostAttempt, PlatformCostAttempt.id == PlatformCostSettlement.attempt_id)
                .filter(PlatformCostAttempt.budget_id == budget.id)
                .order_by(PlatformCostSettlement.attempt_id).populate_existing().with_for_update().all())
    by_attempt = {receipt.attempt_id: receipt for receipt in receipts}
    if len(by_attempt) != len(receipts):
        raise ledger.BillingError('平台回执明细与预算不一致')
    held_total = 0
    spent_total = 0
    for attempt in attempts:
        held = attempt.reserved_cny_micros
        if type(held) is not int or held <= 0:
            raise ledger.BillingError('平台预占明细与预算不一致')
        receipt = by_attempt.pop(attempt.id, None)
        if attempt.status in {'reserved', 'unknown'}:
            if receipt is not None:
                raise ledger.BillingError('未结算尝试含回执，预算不一致')
            held_total += held
            continue
        if attempt.status != 'settled' or receipt is None:
            raise ledger.BillingError('已结算尝试缺少回执，预算不一致')
        rates = (attempt.input_cost_cny_micros_per_million, attempt.output_cost_cny_micros_per_million)
        if (any(type(rate) is not int or rate <= 0 for rate in rates)
                or type(receipt.input_tokens) is not int or type(receipt.output_tokens) is not int
                or not 0 <= receipt.input_tokens <= attempt.input_limit
                or not 0 <= receipt.output_tokens <= attempt.output_limit
                or not isinstance(receipt.receipt_digest, bytes) or len(receipt.receipt_digest) != 32):
            raise ledger.BillingError('合成回执无效，预算不一致')
        amount = (receipt.input_tokens * rates[0] + receipt.output_tokens * rates[1] + 999_999) // 1_000_000
        if type(receipt.actual_cny_micros) is not int or receipt.actual_cny_micros != amount or amount > held:
            raise ledger.BillingError('合成回执成本与预算不一致')
        spent_total += amount
    if by_attempt or held_total != budget.reserved_cny_micros or spent_total != budget.spent_cny_micros:
        raise ledger.BillingError('平台预占或支出明细与预算不一致')


def reserve_model_attempt(db: Session, *, actor_id: int, attempt_key: AttemptKey, model_name: str,
                          provider_name: str, base_url: str, credential_source: str,
                          payload: Mapping) -> PlatformCostAttempt:
    """Reserve a bounded provider cost; caller must supply an authenticated server actor.

    This primitive cannot prove the caller's authentication or the attempt's
    durable uniqueness, and does not authorize or send a provider request.
    """
    _require_enabled()
    _require_no_pending_orm_changes(db)
    _start_write_transaction(db)
    _require_admin(db, actor_id)
    # Every future budget writer must lock this same singleton first; no writer
    # or provisioning API is provided by this detached foundation.
    center = (db.query(PlatformCostCenter).filter_by(id=1).populate_existing().with_for_update().one_or_none())
    if center is None or center.status != 'active':
        raise ledger.BillingError('平台成本中心未启用')
    key = _key_bytes(attempt_key)
    digest = hashlib.sha256(key).digest()
    provider = ledger.resolve_billing_provider_identity(
        provider_name=provider_name, base_url=base_url, credential_source=credential_source, model_name=model_name,
    )
    input_limit, output_limit, payload_digest = _payload_bounds(payload, model_name)
    now = _now()
    version, input_rate, output_rate = _cost_snapshot(db, model_name, now)
    budgets = (db.query(PlatformCostBudget).filter(
        PlatformCostBudget.center_id == center.id,
        PlatformCostBudget.status == 'active', PlatformCostBudget.period_start <= now,
        PlatformCostBudget.period_end > now,
    ).order_by(PlatformCostBudget.id).populate_existing().with_for_update().limit(2).all())
    if len(budgets) != 1:
        raise ledger.BillingError('平台预算未配置或存在重叠')
    _assert_budget_integrity(db, budgets[0])
    # A wait on either the centre, rate or budget lock may cross the expiry.
    now = _now()
    budget = budgets[0]
    if budget.status != 'active' or not budget.period_start <= now < budget.period_end:
        raise ledger.BillingError('平台预算已过期')
    if _cost_snapshot(db, model_name, now) != (version, input_rate, output_rate):
        raise ledger.BillingError('平台费率在等待期间发生变化')
    now = _now()
    if not budget.period_start <= now < budget.period_end:
        raise ledger.BillingError('平台预算已过期')
    cost = (input_limit * input_rate + output_limit * output_rate + 999_999) // 1_000_000
    if not 0 < cost <= _MAX_INTEGER:
        raise ledger.BillingError('平台成本上界无效')
    budget_id = budgets[0].id
    try:
        with db.begin_nested():
            changed = db.execute(update(PlatformCostBudget).where(
                PlatformCostBudget.id == budget_id,
                PlatformCostBudget.status == 'active',
                PlatformCostBudget.period_start <= now,
                PlatformCostBudget.period_end > now,
                PlatformCostBudget.reserved_cny_micros + PlatformCostBudget.spent_cny_micros + cost
                <= PlatformCostBudget.limit_cny_micros,
            ).values(reserved_cny_micros=PlatformCostBudget.reserved_cny_micros + cost)).rowcount
            if changed != 1:
                raise ledger.BillingError('平台预算不足或已失效')
            attempt = PlatformCostAttempt(
                budget_id=budget_id, actor_id=actor_id, attempt_digest=digest, attempt_bytes=key,
                payload_digest=payload_digest, status='reserved', model_name=model_name,
                provider_name=provider.provider_name, provider_endpoint_host=provider.endpoint_host,
                credential_source=provider.credential_source, pricing_version=version,
                input_cost_cny_micros_per_million=input_rate, output_cost_cny_micros_per_million=output_rate,
                input_limit=input_limit, output_limit=output_limit, reserved_cny_micros=cost, created_at=now,
            )
            db.add(attempt)
            db.flush()
    except IntegrityError as exc:
        raise ledger.BillingError('模型尝试已存在或预算状态无效') from exc
    return attempt


def mark_unknown(db: Session, *, actor_id: int, attempt_key: AttemptKey) -> None:
    """Reject writes until attempt ownership and cancellation fencing exist.

    Actor and attempt key alone do not identify an execution owner. Preserve the
    reservation untouched; an eventual owner-aware path needs its own atomic
    financial and cancellation contracts before this can be safely enabled.
    """
    _require_enabled()
    raise ledger.BillingError('平台模型尝试所有权尚未接入，预占保持冻结')
