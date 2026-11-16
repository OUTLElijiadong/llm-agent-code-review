"""Detached ownership experiment; not a production transport or migration.

It creates a new test run and its run owner atomically, never adopts an old run,
never takes over a generation, and never sends or performs real provider settlement.
Synthetic settlement mutates only the separately enabled detached test ledger.
The outer session is owned by the caller, including its commit/rollback.
Authenticated actor/session provenance and old-worker isolation are future gates.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from dataclasses import InitVar, dataclass, field

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.agent_response_run import AgentResponseRun
from app.models.platform_cost_center import (
    PlatformAttemptOwner,
    PlatformCostAttempt,
    PlatformCostBudget,
    PlatformCostCenter,
    PlatformCostSettlement,
    PlatformRunOwner,
)
from app.services import platform_cost_center as platform
from app.services.billing_ledger_service import BillingError

_SECRET_FACTORY = object()
# A second, code-only test gate: enabling the detached reservation experiment
# alone never enables synthetic settlement. This is not a provider trust gate.
_SYNTHETIC_SETTLEMENT_ENABLED = False


@dataclass(frozen=True)
class OwnerSecret:
    _value: bytes = field(repr=False)
    _factory: InitVar[object]

    def __post_init__(self, _factory: object) -> None:
        if _factory is not _SECRET_FACTORY:
            raise ValueError('所有权凭据只能由服务端生成')


def _new_secret() -> OwnerSecret:
    return OwnerSecret(secrets.token_bytes(32), _SECRET_FACTORY)


def _proof(secret: OwnerSecret, *, scope: str, run_pk: int, generation: int, attempt_id: int = 0) -> bytes:
    prefix = f'platform:{scope}:{run_pk}:{generation}:{attempt_id}:'.encode('ascii')
    return hashlib.sha256(prefix + secret._value).digest()


def _matches(secret: OwnerSecret, digest: bytes, *, scope: str, run_pk: int,
             generation: int, attempt_id: int = 0) -> bool:
    return (isinstance(secret, OwnerSecret) and isinstance(secret._value, bytes) and len(secret._value) == 32
            and hmac.compare_digest(_proof(secret, scope=scope, run_pk=run_pk, generation=generation,
                                           attempt_id=attempt_id), bytes(digest)))


def _begin(db: Session, actor_id: int) -> None:
    platform._require_enabled()
    platform._require_no_pending_orm_changes(db)
    platform._start_write_transaction(db)
    if (db.connection().dialect.name == 'sqlite'
            and db.connection().exec_driver_sql('PRAGMA foreign_keys').scalar_one() != 1):
        raise BillingError('数据库外键约束未启用')
    platform._require_admin(db, actor_id)


def _lock_run(db: Session, *, actor_id: int, run_pk: int, session_key: str):
    if type(run_pk) is not int or run_pk <= 0 or not isinstance(session_key, str):
        raise BillingError('运行范围无效')
    run = (db.query(AgentResponseRun).filter_by(id=run_pk)
           .populate_existing().with_for_update().one_or_none())
    if (run is None or run.user_id != actor_id or run.surface != 'admin'
            or run.session_key != session_key):
        raise BillingError('运行归属不匹配')
    ownership = (db.query(PlatformRunOwner).filter_by(run_pk=run_pk)
                 .populate_existing().with_for_update().one_or_none())
    if ownership is None or ownership.actor_id != actor_id:
        raise BillingError('运行所有权不存在')
    return run, ownership


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate checkpoint key')
        result[key] = value
    return result


def _reject_constant(_value: str):
    raise ValueError('nonstandard checkpoint constant')


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite checkpoint number')
    return result


def _running_checkpoint(run: AgentResponseRun) -> dict:
    try:
        checkpoint = json.loads(run.checkpoint_json, object_pairs_hook=_unique_object,
                                parse_constant=_reject_constant, parse_float=_finite_float)
    except (TypeError, ValueError) as exc:
        raise BillingError('运行检查点无效') from exc
    if (not isinstance(checkpoint, dict)
            or ('stop_requested' in checkpoint and type(checkpoint['stop_requested']) is not bool)
            or checkpoint.get('stop_requested') is True
            or checkpoint.get('status') != 'running'):
        raise BillingError('运行已停止或检查点无效')
    return checkpoint


def _require_live(run: AgentResponseRun, ownership: PlatformRunOwner, secret: OwnerSecret) -> None:
    _running_checkpoint(run)
    if (ownership.status != 'active' or run.status != 'running'
            or not _matches(secret, ownership.owner_digest, scope='run', run_pk=run.id,
                            generation=ownership.generation)):
        raise BillingError('运行所有权已失效')


def create_owned_run(db: Session, *, actor_id: int, run_id: str, session_key: str) -> tuple[int, OwnerSecret]:
    """Create a fresh isolated run and owner together; cannot adopt existing runs."""
    _begin(db, actor_id)
    if (not isinstance(run_id, str) or not 0 < len(run_id) <= 80
            or not isinstance(session_key, str) or not 0 < len(session_key) <= 128):
        raise BillingError('运行范围无效')
    secret = _new_secret()
    try:
        with db.begin_nested():
            run = AgentResponseRun(run_id=run_id, user_id=actor_id, surface='admin', session_key=session_key,
                                   status='running', checkpoint_json=json.dumps({'status': 'running'}), version=1)
            db.add(run)
            db.flush()
            db.add(PlatformRunOwner(run_pk=run.id, actor_id=actor_id, generation=1, status='active',
                                    owner_digest=_proof(secret, scope='run', run_pk=run.id, generation=1)))
            db.flush()
    except IntegrityError as exc:
        raise BillingError('运行已存在或所有权创建失败') from exc
    return int(run.id), secret


def reserve_owned_attempt(db: Session, *, actor_id: int, run_pk: int, session_key: str,
                          run_secret: OwnerSecret, **reservation):
    """Reserve and bind an independent attempt secret in the same transaction."""
    _begin(db, actor_id)
    run, run_owner = _lock_run(db, actor_id=actor_id, run_pk=run_pk, session_key=session_key)
    _require_live(run, run_owner, run_secret)
    secret = _new_secret()
    try:
        with db.begin_nested():
            attempt = platform.reserve_model_attempt(db, actor_id=actor_id, **reservation)
            db.add(PlatformAttemptOwner(attempt_id=attempt.id, run_owner_id=run_owner.id,
                                        generation=run_owner.generation,
                                        owner_digest=_proof(secret, scope='attempt', run_pk=run_pk,
                                                            generation=run_owner.generation, attempt_id=attempt.id)))
            db.flush()
    except IntegrityError as exc:
        raise BillingError('模型尝试所有权创建失败') from exc
    return attempt, secret


def mark_owned_attempt_unknown(db: Session, *, actor_id: int, run_pk: int, session_key: str,
                               run_secret: OwnerSecret, attempt_id: int, attempt_secret: OwnerSecret) -> None:
    _begin(db, actor_id)
    run, run_owner = _lock_run(db, actor_id=actor_id, run_pk=run_pk, session_key=session_key)
    _require_live(run, run_owner, run_secret)
    binding = (db.query(PlatformAttemptOwner).filter_by(attempt_id=attempt_id)
               .populate_existing().with_for_update().one_or_none())
    if (binding is None or binding.run_owner_id != run_owner.id or binding.generation != run_owner.generation
            or not _matches(attempt_secret, binding.owner_digest, scope='attempt', run_pk=run_pk,
                            generation=run_owner.generation, attempt_id=attempt_id)):
        raise BillingError('模型尝试所有权已失效')
    attempt = (db.query(PlatformCostAttempt).filter_by(id=attempt_id)
               .populate_existing().with_for_update().one_or_none())
    if attempt is None or attempt.actor_id != actor_id or attempt.status not in {'reserved', 'unknown'}:
        raise BillingError('模型尝试状态无效')
    if attempt.status == 'reserved':
        attempt.status = 'unknown'
        db.flush()


def cancel_owned_run(db: Session, *, actor_id: int, run_pk: int, session_key: str) -> None:
    """Cancel and fence this experiment; all its reserved attempts remain frozen."""
    _begin(db, actor_id)
    run, run_owner = _lock_run(db, actor_id=actor_id, run_pk=run_pk, session_key=session_key)
    if run_owner.status != 'active' or run.status != 'running':
        raise BillingError('运行已停止或所有权失效')
    checkpoint = _running_checkpoint(run)
    bindings = (db.query(PlatformAttemptOwner).filter_by(run_owner_id=run_owner.id)
                .order_by(PlatformAttemptOwner.attempt_id).populate_existing().with_for_update().all())
    attempts = []
    for binding in bindings:
        attempt = (db.query(PlatformCostAttempt).filter_by(id=binding.attempt_id)
                   .populate_existing().with_for_update().one_or_none())
        if attempt is None or attempt.actor_id != actor_id or attempt.status not in {'reserved', 'unknown', 'settled'}:
            raise BillingError('模型尝试状态无效')
        attempts.append(attempt)
    checkpoint['stop_requested'] = True
    run.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, allow_nan=False)
    run.status = 'cancelled'
    run.version = int(run.version or 0) + 1
    run_owner.status = 'cancelled'
    run_owner.generation += 1
    for attempt in attempts:
        if attempt.status == 'reserved':
            attempt.status = 'unknown'
    db.flush()


def settle_owned_attempt(db: Session, *, actor_id: int, run_pk: int, session_key: str,
                         run_secret: OwnerSecret, attempt_id: int, attempt_secret: OwnerSecret,
                         receipt_digest: bytes, input_tokens: int, output_tokens: int) -> int:
    """Settle a bounded synthetic receipt; never authenticates a provider response.

    Caller owns the outer transaction; aborts must not be interpreted as proof
    of non-delivery, nor used to release or automatically retry an attempt.
    """
    if not _SYNTHETIC_SETTLEMENT_ENABLED:
        raise BillingError('合成结算实验未启用')
    _begin(db, actor_id)
    run, run_owner = _lock_run(db, actor_id=actor_id, run_pk=run_pk, session_key=session_key)
    _require_live(run, run_owner, run_secret)
    center = (db.query(PlatformCostCenter).filter_by(id=1).populate_existing().with_for_update().one_or_none())
    if center is None:
        raise BillingError('平台成本中心不存在')
    if type(attempt_id) is not int or attempt_id <= 0:
        raise BillingError('模型尝试无效')
    # The attempt's budget binding is immutable; this snapshot only selects the
    # budget lock. Recheck the binding under an exclusive current read below.
    budget_id = db.query(PlatformCostAttempt.budget_id).filter_by(id=attempt_id).scalar()
    if budget_id is None:
        raise BillingError('模型尝试不存在')
    budget = (db.query(PlatformCostBudget).filter_by(id=budget_id, center_id=center.id)
              .populate_existing().with_for_update().one_or_none())
    if budget is None:
        raise BillingError('模型尝试预算不存在')
    platform._assert_budget_integrity(db, budget)
    binding = (db.query(PlatformAttemptOwner).filter_by(attempt_id=attempt_id)
               .populate_existing().with_for_update().one_or_none())
    if (binding is None or binding.run_owner_id != run_owner.id or binding.generation != run_owner.generation
            or not _matches(attempt_secret, binding.owner_digest, scope='attempt', run_pk=run_pk,
                            generation=run_owner.generation, attempt_id=attempt_id)):
        raise BillingError('模型尝试所有权已失效')
    attempt = (db.query(PlatformCostAttempt).filter_by(id=attempt_id)
               .populate_existing().with_for_update().one_or_none())
    if (attempt is None or attempt.actor_id != actor_id or attempt.budget_id != budget.id
            or attempt.status not in {'reserved', 'settled'}):
        raise BillingError('模型尝试不允许结算')
    if (not isinstance(receipt_digest, bytes) or len(receipt_digest) != 32
            or type(input_tokens) is not int or type(output_tokens) is not int
            or not 0 <= input_tokens <= attempt.input_limit or not 0 <= output_tokens <= attempt.output_limit):
        raise BillingError('模型回执或用量无效')
    rates = (attempt.input_cost_cny_micros_per_million, attempt.output_cost_cny_micros_per_million)
    if any(type(rate) is not int or rate <= 0 for rate in rates):
        raise BillingError('冻结成本无效')
    actual = (input_tokens * rates[0] + output_tokens * rates[1] + 999_999) // 1_000_000
    held = attempt.reserved_cny_micros
    if type(held) is not int or not 0 <= actual <= held <= platform._MAX_INTEGER:
        raise BillingError('实际费用超出预占')
    receipt = (db.query(PlatformCostSettlement).filter_by(attempt_id=attempt_id)
               .populate_existing().with_for_update().one_or_none())
    if attempt.status == 'settled':
        if (receipt is None or bytes(receipt.receipt_digest) != receipt_digest
                or receipt.input_tokens != input_tokens or receipt.output_tokens != output_tokens
                or receipt.actual_cny_micros != actual):
            raise BillingError('模型回执重放不一致')
        return actual
    if receipt is not None:
        raise BillingError('模型回执与预占状态不一致')
    try:
        with db.begin_nested():
            db.add(PlatformCostSettlement(attempt_id=attempt_id, receipt_digest=receipt_digest,
                                          input_tokens=input_tokens, output_tokens=output_tokens,
                                          actual_cny_micros=actual, created_at=platform._now()))
            db.flush()
            changed = db.execute(update(PlatformCostBudget).where(
                PlatformCostBudget.id == budget.id,
                PlatformCostBudget.reserved_cny_micros >= held,
                PlatformCostBudget.reserved_cny_micros + PlatformCostBudget.spent_cny_micros - held + actual
                <= PlatformCostBudget.limit_cny_micros,
            ).values(
                reserved_cny_micros=PlatformCostBudget.reserved_cny_micros - held,
                spent_cny_micros=PlatformCostBudget.spent_cny_micros + actual,
            ).execution_options(synchronize_session=False)).rowcount
            if changed != 1:
                raise BillingError('平台预占与预算不一致')
            attempt.status = 'settled'
            db.flush()
    except IntegrityError as exc:
        raise BillingError('模型回执冲突或成本状态无效') from exc
    db.expire(budget)
    return actual
