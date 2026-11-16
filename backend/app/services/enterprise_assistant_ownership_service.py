"""Detached ownership primitives; NOT an authorization to send or charge.

Only a trusted future integration may call these in its existing claim transaction.
Issuing an owner for a legacy live generation is unsafe and is not enabled here.
Never return secrets to users, write them to checkpoints, or log them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError
from app.models.billing_ledger import BillingReservation, BillingWallet
from app.models.enterprise_assistant_ownership import (
    EnterpriseAssistantModelAttemptOwner,
    EnterpriseAssistantRunOwner,
    EnterpriseAssistantToolOwner,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import billing_search_fetch_service
from app.services import enterprise_assistant_billing_group as billing_group

# No production caller may mint claims until claim atomicity and old-worker isolation are integrated.
_FOUNDATION_ACTIVATED = False


def _require_foundation_activation() -> None:
    if not _FOUNDATION_ACTIVATED:
        raise ForbiddenError("执行所有权基础尚未启用")


class OwnerSecret:
    """In-process capability; the database stores only its digest."""

    __slots__ = ("_value",)

    def __init__(self, value: bytes):
        if not isinstance(value, bytes) or len(value) != 32:
            raise ValueError("无效的所有权凭据")
        self._value = value

    def __repr__(self) -> str:
        return "<OwnerSecret redacted>"


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _proof(secret: OwnerSecret) -> bytes:
    if not isinstance(secret, OwnerSecret):
        raise ForbiddenError("执行所有权无效")
    return _digest(secret._value)


def _locked_scope(db: Session, *, run_id: int, enterprise_id: int, conversation_id: int, generation: int):
    if any(type(value) is not int or value <= 0 for value in (run_id, enterprise_id, conversation_id, generation)):
        raise ForbiddenError("执行所有权范围无效")
    run = (
        db.query(EnterpriseAssistantRun)
        .filter_by(
            id=run_id,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    state = (
        db.query(EnterpriseAssistantRuntimeState)
        .filter_by(
            assistant_run_id=run_id,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
        if run is not None
        else None
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if (
        run is None
        or state is None
        or int(state.lease_generation) != generation
        or state.stop_requested
        or run.status != "running"
        or state.status
        not in {
            "running",
            "waiting_approval",
            "waiting_input",
            "waiting_clarification",
            "approving",
            "answering",
            "rejecting",
        }
        or state.lease_expires_at is None
        or state.lease_expires_at <= now
        or state.initiator_user_id != run.initiator_user_id
        or state.source_message_id != run.source_message_id
        or state.group_id != run.group_id
    ):
        raise ForbiddenError("执行所有权已失效")
    return run, state


def _run_owner(db: Session, *, run_id: int, generation: int, secret: OwnerSecret):
    row = (
        db.query(EnterpriseAssistantRunOwner)
        .filter_by(
            assistant_run_id=run_id,
            generation=generation,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if row is None or not hmac.compare_digest(bytes(row.owner_digest), _proof(secret)):
        raise ForbiddenError("运行所有权无效")
    return row


def _ensure_outer_transaction(db: Session) -> None:
    # SQLite has no row-level FOR UPDATE and sqlite3 delays BEGIN until a write.
    # Acquire its write reservation before any scope read. An existing deferred
    # transaction is upgraded with a zero-row UPDATE; a stale snapshot fails
    # closed with SQLITE_BUSY_SNAPSHOT rather than authorizing old state.
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        return
    if connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql(
            "UPDATE enterprise_assistant_runtime_state SET lease_generation=lease_generation WHERE 0"
        )
    else:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def claim_run(db: Session, *, run_id: int, enterprise_id: int, conversation_id: int, generation: int) -> OwnerSecret:
    """Claim once; caller must already own the *new-generation* claim transaction.

    This disconnected primitive cannot establish that an old worker is stopped;
    using it on a pre-existing live generation would be unsafe. The returned
    capability is valid only if the enclosing transaction successfully commits.
    """
    _require_foundation_activation()
    _ensure_outer_transaction(db)
    _reject_pending_scope_changes(db, run_id=run_id)
    with db.no_autoflush:
        _locked_scope(
            db, run_id=run_id, enterprise_id=enterprise_id, conversation_id=conversation_id, generation=generation
        )
    secret = OwnerSecret(secrets.token_bytes(32))
    try:
        with db.begin_nested():
            db.add(
                EnterpriseAssistantRunOwner(assistant_run_id=run_id, generation=generation, owner_digest=_proof(secret))
            )
            db.flush()
    except IntegrityError as exc:
        raise ConflictError("该运行代次已有执行者") from exc
    return secret


def assert_run_owner(
    db: Session, *, run_id: int, enterprise_id: int, conversation_id: int, generation: int, secret: OwnerSecret
) -> None:
    _require_foundation_activation()
    _ensure_outer_transaction(db)
    _reject_pending_scope_changes(db, run_id=run_id)
    with db.no_autoflush:
        _locked_scope(
            db, run_id=run_id, enterprise_id=enterprise_id, conversation_id=conversation_id, generation=generation
        )
        _run_owner(db, run_id=run_id, generation=generation, secret=secret)


def _identity(run: EnterpriseAssistantRun, *, call_id: str, tool_name: str, arguments: Mapping[str, Any]):
    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 120:
        raise ConflictError("工具调用标识无效")
    try:
        call_bytes = call_id.encode("utf-8")
        suffix = hashlib.sha256(
            f"{call_id}:{'search_enterprise_web' if tool_name == 'search' else 'fetch_enterprise_web_page'}".encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        normalized = billing_search_fetch_service.validate_arguments(tool_name, arguments)
        arguments_bytes = json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        request_bytes = f"enterprise-assistant:{run.public_id}:tool:{tool_name}:{suffix}".encode("utf-8")
    except (UnicodeError, TypeError, ValueError) as exc:
        raise ConflictError("工具调用参数无效") from exc
    if len(call_bytes) > 480 or len(request_bytes) > 640 or len(arguments_bytes) > 16384:
        raise ConflictError("工具调用参数过长")
    return call_bytes, request_bytes, arguments_bytes


def _wallet(db: Session, *, wallet_id: int, enterprise_id: int):
    if type(wallet_id) is not int or wallet_id <= 0:
        raise ForbiddenError("企业钱包归属不匹配")
    wallet = (
        db.query(BillingWallet)
        .filter_by(id=wallet_id, owner_type="enterprise", owner_id=enterprise_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if wallet is None or wallet.owner_type != "enterprise" or wallet.owner_id != enterprise_id:
        raise ForbiddenError("企业钱包归属不匹配")


def _reject_pending_scope_changes(
    db: Session, *, run_id: int, wallet_id: int | None = None, reservation_id: int | None = None,
    allowed_new_reservation: BillingReservation | None = None,
) -> None:
    """Do not refresh or implicitly flush caller work outside this primitive.

    Savepoints flush all pending ORM objects even under no_autoflush. Reject all
    such work, irrespective of current (possibly changed) identifiers. The
    single new hold explicitly supplied to model claim is its intended input.
    """
    for row in set(db.new).union(db.dirty, db.deleted):
        if row is allowed_new_reservation and row in db.new:
            continue
        raise ForbiddenError("当前事务有未刷新的所有权范围更改，禁止覆盖")


def claim_tool(
    db: Session,
    *,
    run_id: int,
    enterprise_id: int,
    conversation_id: int,
    generation: int,
    run_secret: OwnerSecret,
    call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    wallet_id: int,
) -> OwnerSecret:
    _require_foundation_activation()
    _ensure_outer_transaction(db)
    _reject_pending_scope_changes(db, run_id=run_id, wallet_id=wallet_id)
    with db.no_autoflush:
        run, _state = _locked_scope(
            db, run_id=run_id, enterprise_id=enterprise_id, conversation_id=conversation_id, generation=generation
        )
        parent = _run_owner(db, run_id=run_id, generation=generation, secret=run_secret)
        call_bytes, request_bytes, arguments_bytes = _identity(
            run, call_id=call_id, tool_name=tool_name, arguments=arguments
        )
        # Acquire the final wallet lock before inserting the owner row. InnoDB's
        # foreign-key check itself takes a shared parent lock; inserting first and
        # upgrading later can deadlock two independent runs using the same wallet.
        # The caller owns the transaction and must commit before network I/O;
        # this detached primitive cannot enforce that external lifecycle.
        _wallet(db, wallet_id=wallet_id, enterprise_id=enterprise_id)
    secret = OwnerSecret(secrets.token_bytes(32))
    try:
        with db.begin_nested():
            db.add(
                EnterpriseAssistantToolOwner(
                    assistant_run_id=run_id,
                    run_owner_id=parent.id,
                    generation=generation,
                    enterprise_id=enterprise_id,
                    conversation_id=conversation_id,
                    initiator_user_id=run.initiator_user_id,
                    billing_group_id=billing_group.frozen(run),
                    wallet_id=wallet_id,
                    tool_name=tool_name,
                    call_digest=_digest(call_bytes),
                    call_bytes=call_bytes,
                    request_digest=_digest(request_bytes),
                    request_bytes=request_bytes,
                    arguments_digest=_digest(arguments_bytes),
                    owner_digest=_proof(secret),
                )
            )
            db.flush()
    except IntegrityError as exc:
        raise ConflictError("该工具调用或账单键已有执行者") from exc
    return secret


def assert_tool_owner(
    db: Session,
    *,
    run_id: int,
    enterprise_id: int,
    conversation_id: int,
    generation: int,
    run_secret: OwnerSecret,
    tool_secret: OwnerSecret,
    call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    wallet_id: int,
) -> None:
    _require_foundation_activation()
    _ensure_outer_transaction(db)
    _reject_pending_scope_changes(db, run_id=run_id, wallet_id=wallet_id)
    with db.no_autoflush:
        run, _state = _locked_scope(
            db, run_id=run_id, enterprise_id=enterprise_id, conversation_id=conversation_id, generation=generation
        )
        parent = _run_owner(db, run_id=run_id, generation=generation, secret=run_secret)
        call_bytes, request_bytes, arguments_bytes = _identity(
            run, call_id=call_id, tool_name=tool_name, arguments=arguments
        )
        row = (
            db.query(EnterpriseAssistantToolOwner)
            .filter_by(
                assistant_run_id=run_id,
                call_digest=_digest(call_bytes),
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if (
            row is None
            or row.run_owner_id != parent.id
            or row.generation != generation
            or row.enterprise_id != enterprise_id
            or row.conversation_id != conversation_id
            or row.initiator_user_id != run.initiator_user_id
            or row.billing_group_id != billing_group.frozen(run)
            or row.wallet_id != wallet_id
            or row.tool_name != tool_name
            or bytes(row.call_bytes) != call_bytes
            or bytes(row.request_digest) != _digest(request_bytes)
            or bytes(row.request_bytes) != request_bytes
            or not hmac.compare_digest(bytes(row.arguments_digest), _digest(arguments_bytes))
            or not hmac.compare_digest(bytes(row.owner_digest), _proof(tool_secret))
        ):
            raise ForbiddenError("工具执行所有权无效")
        _wallet(db, wallet_id=wallet_id, enterprise_id=enterprise_id)


def _model_binding_identity(run, state, reservation: BillingReservation, round_key: str, payload_digest: bytes):
    """Validate scope only; this does not validate funding or authorize a request."""
    # An opaque identifier avoids persisting arbitrary prompt text. Its
    # authoritative relationship to a checkpoint is a future integration gate.
    if not isinstance(round_key, str) or not re.fullmatch(r"round:[0-9a-f]{32}", round_key):
        raise ForbiddenError("模型轮次标识无效")
    if not isinstance(payload_digest, bytes) or len(payload_digest) != 32:
        raise ForbiddenError("模型请求摘要无效")
    try:
        round_bytes = round_key.encode("utf-8")
        request_bytes = reservation.request_key.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise ForbiddenError("模型请求标识无效") from exc
    if not 1 <= len(round_bytes) <= 480 or not 1 <= len(request_bytes) <= 640:
        raise ForbiddenError("模型请求标识无效")
    invocation = state.billing_invocation_count
    if (
        type(invocation) is not int
        or invocation <= 0
        or not re.fullmatch(
            re.escape(f"enterprise-assistant:{run.public_id}:{invocation}:") + r"[1-9][0-9]*",
            reservation.request_key,
        )
        or len(reservation.request_key) > 160
    ):
        raise ForbiddenError("模型请求键与当前运行不匹配")
    if not isinstance(reservation.model_name, str) or not 1 <= len(reservation.model_name) <= 80:
        raise ForbiddenError("模型计费身份无效")
    expected_group = billing_group.frozen(run)
    expected_payer = None if run.trigger_kind == "proactive" else int(run.initiator_user_id)
    expected_usage = "enterprise_auto" if run.trigger_kind == "proactive" else "member"
    if (
        reservation.billable_kind != "model"
        or reservation.enterprise_id != run.enterprise_id
        or reservation.group_id != expected_group
        or reservation.user_id != expected_payer
        or reservation.usage_kind != expected_usage
        or type(reservation.enterprise_wallet_id) is not int
        or reservation.enterprise_wallet_id <= 0
        or reservation.personal_wallet_id is not None
        or type(reservation.allow_personal) is not int
        or reservation.allow_personal != 0
        or type(reservation.requested_milli) is not int
        or reservation.requested_milli <= 0
    ):
        raise ForbiddenError("模型预占计费归属不匹配")
    fields = (
        reservation.request_key,
        reservation.model_name,
        reservation.billable_kind,
        reservation.enterprise_id,
        reservation.group_id,
        reservation.user_id,
        reservation.usage_kind,
        reservation.enterprise_wallet_id,
        reservation.personal_wallet_id,
        reservation.allow_personal,
        reservation.requested_milli,
        reservation.provider_identity_version,
        reservation.provider_name,
        reservation.provider_endpoint_host,
        reservation.credential_source,
        reservation.pricing_version,
        reservation.input_rate_numerator,
        reservation.input_rate_denominator,
        reservation.output_rate_numerator,
        reservation.output_rate_denominator,
    )
    billing_digest = _digest(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return round_bytes, request_bytes, billing_digest


def claim_model_attempt(
    db: Session,
    *,
    run_id: int,
    enterprise_id: int,
    conversation_id: int,
    generation: int,
    run_secret: OwnerSecret,
    round_key: str,
    payload_digest: bytes,
    reservation: BillingReservation,
) -> OwnerSecret:
    """Bind a new pending hold atomically; NOT evidence of a valid reserve or send right.

    The caller must already have created the run owner in its new-generation
    transaction. A pending ORM object alone is not proof that trusted reserve,
    wallet limits, checkpoint round or pricing were validated. Production use
    is disabled and needs a proven integration with those contracts.
    """
    _require_foundation_activation()
    _ensure_outer_transaction(db)
    if not isinstance(reservation, BillingReservation) or reservation not in db.new or not inspect(reservation).pending:
        raise ForbiddenError("只能绑定本事务尚未写入的新模型预占")
    _reject_pending_scope_changes(
        db, run_id=run_id, wallet_id=reservation.enterprise_wallet_id, allowed_new_reservation=reservation
    )
    with db.no_autoflush:
        run, _state = _locked_scope(
            db,
            run_id=run_id,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            generation=generation,
        )
        parent = _run_owner(db, run_id=run_id, generation=generation, secret=run_secret)
        round_bytes, request_bytes, billing_digest = _model_binding_identity(
            run, _state, reservation, round_key, payload_digest
        )
        if (
            reservation.status != "reserved"
            or reservation.usage_state != "pending"
            or reservation.provider_attempt_started_at is not None
            or reservation.actual_milli is not None
            or reservation.input_tokens is not None
            or reservation.output_tokens is not None
            or reservation.server_tool_search_calls is not None
            or reservation.server_tool_fetch_calls is not None
        ):
            raise ForbiddenError("模型预占并非全新未发送状态")
        existing = (
            db.query(EnterpriseAssistantModelAttemptOwner.id)
            .filter_by(assistant_run_id=run_id, round_digest=_digest(round_bytes))
            .first()
        )
        if existing is not None:
            raise ConflictError("该模型轮次已有执行者")
        wallet = (
            db.query(BillingWallet)
            .filter_by(id=reservation.enterprise_wallet_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if wallet is None or wallet.owner_type != "enterprise" or wallet.owner_id != enterprise_id:
            raise ForbiddenError("企业钱包归属不匹配")
    secret = OwnerSecret(secrets.token_bytes(32))
    # No implicit commit and no nested savepoint: uniqueness failures require
    # the caller to roll back its enclosing transaction, including the hold.
    db.flush([reservation])
    db.add(
        EnterpriseAssistantModelAttemptOwner(
            assistant_run_id=run_id,
            run_owner_id=parent.id,
            reservation_id=reservation.id,
            generation=generation,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            initiator_user_id=run.initiator_user_id,
            billing_group_id=reservation.group_id,
            wallet_id=reservation.enterprise_wallet_id,
            payer_user_id=reservation.user_id,
            usage_kind=reservation.usage_kind,
            model_name=reservation.model_name,
            round_digest=_digest(round_bytes),
            round_bytes=round_bytes,
            request_digest=_digest(request_bytes),
            request_bytes=request_bytes,
            payload_digest=payload_digest,
            billing_digest=billing_digest,
            owner_digest=_proof(secret),
        )
    )
    db.flush()
    return secret


def assert_model_attempt(
    db: Session,
    *,
    run_id: int,
    enterprise_id: int,
    conversation_id: int,
    generation: int,
    run_secret: OwnerSecret,
    attempt_secret: OwnerSecret,
    round_key: str,
    payload_digest: bytes,
    reservation_id: int,
) -> None:
    """Check the binding under locks held by the caller; not an accounting transition."""
    _require_foundation_activation()
    _ensure_outer_transaction(db)
    if type(reservation_id) is not int or reservation_id <= 0:
        raise ForbiddenError("模型预占身份无效")
    _reject_pending_scope_changes(db, run_id=run_id, reservation_id=reservation_id)
    with db.no_autoflush:
        run, _state = _locked_scope(
            db,
            run_id=run_id,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            generation=generation,
        )
        parent = _run_owner(db, run_id=run_id, generation=generation, secret=run_secret)
        # Locate a real row without locking a missing InnoDB unique-key gap.
        candidate = (
            db.query(EnterpriseAssistantModelAttemptOwner.id)
            .filter_by(reservation_id=reservation_id, assistant_run_id=run_id)
            .first()
        )
        if candidate is None:
            raise ForbiddenError("模型执行所有权无效")
        binding = (
            db.query(EnterpriseAssistantModelAttemptOwner)
            .filter_by(id=candidate.id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        _reject_pending_scope_changes(db, run_id=run_id, wallet_id=binding.wallet_id, reservation_id=reservation_id)
        wallet = db.query(BillingWallet).filter_by(id=binding.wallet_id).populate_existing().with_for_update().first()
        hold = db.query(BillingReservation).filter_by(id=reservation_id).populate_existing().with_for_update().first()
        if wallet is None or wallet.owner_type != "enterprise" or wallet.owner_id != enterprise_id or hold is None:
            raise ForbiddenError("模型执行所有权无效")
        round_bytes, request_bytes, billing_digest = _model_binding_identity(
            run, _state, hold, round_key, payload_digest
        )
        if (
            binding.run_owner_id != parent.id
            or binding.generation != generation
            or binding.enterprise_id != enterprise_id
            or binding.conversation_id != conversation_id
            or binding.initiator_user_id != run.initiator_user_id
            or binding.billing_group_id != hold.group_id
            or binding.wallet_id != hold.enterprise_wallet_id
            or binding.payer_user_id != hold.user_id
            or binding.usage_kind != hold.usage_kind
            or binding.model_name != hold.model_name
            or bytes(binding.round_digest) != _digest(round_bytes)
            or bytes(binding.round_bytes) != round_bytes
            or bytes(binding.request_digest) != _digest(request_bytes)
            or bytes(binding.request_bytes) != request_bytes
            or not hmac.compare_digest(bytes(binding.payload_digest), payload_digest)
            or not hmac.compare_digest(bytes(binding.billing_digest), billing_digest)
            or not hmac.compare_digest(bytes(binding.owner_digest), _proof(attempt_secret))
            or hold.status != "reserved"
        ):
            raise ForbiddenError("模型执行所有权无效")
