"""Audited two-person review for uncertain model billing reservations.

This service never invents rates. Successful settlement delegates to the normal
ledger path, which recomputes from the immutable reservation snapshot.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.billing_ledger import (
    BillingReservation,
    BillingReservationPart,
    BillingReservationReconciliationEvent,
    BillingToolExecutionResult,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import billing_ledger_service as ledger

_HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_DB_INTEGER = 2**63 - 1
_RESOLUTIONS = {"settle", "release_not_sent"}
STALE_TOOL_ATTEMPT_AGE = timedelta(minutes=30)


class ReconciliationError(ValueError):
    """A reconciliation proposal or decision is invalid or stale."""


class ReconciliationBlocked(ReconciliationError):
    """A state-based action was blocked and must be committed to the audit history."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _required_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ReconciliationError(f"{label}无效")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise ReconciliationError(f"{label}必须为 1 至 {maximum} 个有效字符")
    return normalized


def _validate_evidence(evidence_reference: str, evidence_sha256: str, note: str) -> tuple[str, str, str]:
    reference = _required_text(evidence_reference, "外部证据引用", 240)
    digest = evidence_sha256.strip().lower() if isinstance(evidence_sha256, str) else ""
    if not _HEX_SHA256.fullmatch(digest):
        raise ReconciliationError("证据 SHA-256 必须为 64 位十六进制摘要")
    return reference, digest, _required_text(note, "核验说明", 500)


def _events(db: Session, reservation_id: int, *, lock: bool = False):
    query = db.query(BillingReservationReconciliationEvent).filter_by(
        reservation_id=reservation_id,
    ).order_by(BillingReservationReconciliationEvent.id)
    if lock:
        query = query.with_for_update()
    return query.all()


def _pending(events: list[BillingReservationReconciliationEvent]):
    for event in reversed(events):
        if event.decision == "blocked":
            continue
        return event if event.decision == "proposed" else None
    return None


def _block_action(db: Session, *, reservation_id: int, actor_id: int, resolution: str | None,
                  reason: str, proposal: BillingReservationReconciliationEvent | None = None):
    db.add(BillingReservationReconciliationEvent(
        reservation_id=reservation_id,
        related_event_id=proposal.id if proposal else None,
        actor_id=actor_id,
        decision="blocked",
        resolution=resolution,
        # A blocked event references the proposal by ID but never duplicates
        # provider identifiers, usage, amounts, or user-supplied evidence.
        provider_request_id=None,
        input_tokens=None,
        output_tokens=None,
        actual_milli=None,
        tool_successful_calls=None,
        evidence_reference=None,
        evidence_sha256=None,
        note=reason[:500],
        created_at=_now(),
    ))
    db.flush()
    raise ReconciliationBlocked(reason)


def _block_after_ledger_rollback(db: Session, *, reservation_id: int, actor_id: int,
                                 proposal_id: int, resolution: str, reason: str):
    """Rollback any partial ledger work, then persist only the blocked audit event."""
    db.rollback()
    _reservation_for_update(db, reservation_id, wallet_first=True)
    proposal = next((event for event in _events(db, reservation_id, lock=True)
                     if event.id == proposal_id and event.decision == "proposed"), None)
    if proposal is None:
        raise ReconciliationError("原提案已不存在，账本操作已回滚")
    _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                  resolution=resolution, reason=reason, proposal=proposal)


def list_unresolved_reservations(db: Session, *, status: str | None = None,
                                 billable_kind: str | None = None,
                                 offset: int = 0, limit: int = 100):
    if status not in {None, "reserved", "unknown", "reviewed"}:
        raise ReconciliationError("预占状态筛选无效")
    if billable_kind not in {None, "model", "search", "fetch"}:
        raise ReconciliationError("计费类别筛选无效")
    if type(offset) is not int or not 0 <= offset <= 100_000:
        raise ReconciliationError("分页位置超出允许范围")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ReconciliationError("每页数量超出允许范围")
    query = db.query(BillingReservation)
    if billable_kind is not None:
        query = query.filter(BillingReservation.billable_kind == billable_kind)
    if status == "reviewed":
        reviewed_reservations = db.query(BillingReservationReconciliationEvent.reservation_id).distinct()
        query = query.filter(BillingReservation.id.in_(reviewed_reservations))
    else:
        query = query.filter(BillingReservation.status.in_(("reserved", "unknown")))
    if status in {"reserved", "unknown"}:
        query = query.filter(BillingReservation.status == status)
    total = query.count()
    rows = query.order_by(BillingReservation.created_at.desc(), BillingReservation.id.desc()) \
        .offset(offset).limit(limit).all()
    return rows, total


def list_reconciliation_events(db: Session, *, reservation_id: int):
    if db.get(BillingReservation, reservation_id) is None:
        raise ReconciliationError("计费预占记录不存在")
    return _events(db, reservation_id)


def can_recover_stale_tool_attempt(
    db: Session, reservation: BillingReservation, *, now: datetime | None = None,
) -> bool:
    at = now.astimezone(timezone.utc).replace(tzinfo=None) if now and now.tzinfo else (now or _now())
    if (reservation.status != "reserved"
            or reservation.billable_kind not in {"search", "fetch"}
            or reservation.provider_attempt_started_at is None
            or reservation.provider_attempt_started_at > at - STALE_TOOL_ATTEMPT_AGE):
        return False
    match = re.fullmatch(
        r"enterprise-assistant:([A-Za-z0-9_-]{1,80}):tool:(search|fetch):([0-9a-f]{32})",
        reservation.request_key,
    )
    if match is None or match.group(2) != reservation.billable_kind:
        return False
    run = db.query(EnterpriseAssistantRun).filter_by(
        public_id=match.group(1),
        enterprise_id=reservation.enterprise_id,
        initiator_user_id=reservation.user_id,
    ).one_or_none()
    state = (
        db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one_or_none()
        if run is not None else None
    )
    return state is not None and (state.lease_expires_at is None or state.lease_expires_at <= at)


def recover_stale_tool_attempt(db: Session, *, reservation_id: int, actor_id: int,
                               actor_name: str, note: str, now: datetime | None = None):
    """Move a provably abandoned Search/Fetch hold to unknown; never release it."""
    if type(actor_id) is not int or actor_id <= 0:
        raise ReconciliationError("审核账号无效")
    normalized_note = _required_text(note, "恢复说明", 500)
    at = now.astimezone(timezone.utc).replace(tzinfo=None) if now and now.tzinfo else (now or _now())
    # Locate the runtime without a lock, then take run/state before wallet and
    # reservation, matching normal model and Search/Fetch execution. The
    # reservation is locked and its identity rechecked below before mutation.
    candidate = db.query(BillingReservation).filter_by(id=reservation_id).first()
    if candidate is None:
        raise ReconciliationError("模型预占记录不存在")
    identity = (candidate.request_key, candidate.billable_kind, candidate.enterprise_id, candidate.user_id)
    match = re.fullmatch(
        r"enterprise-assistant:([A-Za-z0-9_-]{1,80}):tool:(search|fetch):[0-9a-f]{32}",
        candidate.request_key,
    )
    if match is None or match.group(2) != candidate.billable_kind:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="请求键不属于受支持的企业小菱 Search/Fetch 运行")
    run = db.query(EnterpriseAssistantRun).filter_by(
        public_id=match.group(1),
        enterprise_id=candidate.enterprise_id,
        initiator_user_id=candidate.user_id,
    ).with_for_update().one_or_none()
    state = (
        db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).with_for_update().one_or_none()
        if run is not None else None
    )
    if state is None or (state.lease_expires_at is not None and state.lease_expires_at > at):
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="企业小菱运行租约仍有效或运行证据缺失；不得提前恢复")
    reservation, parts = _reservation_for_update(db, reservation_id, wallet_first=True)
    if (reservation.request_key, reservation.billable_kind, reservation.enterprise_id, reservation.user_id) != identity:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="预占身份已变化；禁止根据旧运行信息恢复")
    if reservation.status != "reserved" or reservation.billable_kind not in {"search", "fetch"}:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="仅可恢复仍预占的 Search/Fetch 请求")
    if reservation.provider_attempt_started_at is None:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="该 Search/Fetch 请求尚未提交外呼标记，不属于陈旧外呼恢复")
    if reservation.provider_attempt_started_at > at - STALE_TOOL_ATTEMPT_AGE:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="外呼标记不足 30 分钟，仍在安全等待窗口内")
    if (reservation.tool_name != reservation.billable_kind
            or type(reservation.tool_price_milli_per_success) is not int
            or reservation.tool_price_milli_per_success <= 0
            or reservation.actual_milli is not None
            or reservation.tool_successful_calls is not None
            or reservation.input_tokens is not None
            or reservation.output_tokens is not None
            or reservation.server_tool_search_calls is not None
            or reservation.server_tool_fetch_calls is not None):
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="预占已有结果字段或费率快照异常；保持冻结并升级处理")
    try:
        _require_complete_hold(reservation, parts)
    except ReconciliationError as exc:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason=str(exc))

    cached_result = db.query(BillingToolExecutionResult.id).filter_by(
        request_key=reservation.request_key,
    ).limit(1).first()
    if cached_result is not None:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id, resolution=None,
                      reason="已存在原子保存的工具结果；禁止将账单状态转为未知")
    # Fence any delayed worker that resumes after the absolute upstream deadline
    # or after a process pause. Its checkpoint store and post-response authorization
    # both require the prior generation and will reject the late result.
    state.lease_generation = int(state.lease_generation or 0) + 1
    state.status = "awaiting_reconciliation"
    state.error_code = "enterprise_runtime_usage_unsettled"
    state.lease_expires_at = None
    state.version = int(state.version or 0) + 1
    run.status = "awaiting_reconciliation"
    run.error_code = state.error_code
    ledger.mark_unknown(db, reservation.request_key)
    db.add(BillingReservationReconciliationEvent(
        reservation_id=reservation.id,
        related_event_id=None,
        actor_id=actor_id,
        decision="recovered",
        resolution=None,
        provider_request_id=None,
        input_tokens=None,
        output_tokens=None,
        actual_milli=None,
        tool_successful_calls=None,
        evidence_reference=None,
        evidence_sha256=None,
        note=(f"陈旧外呼安全恢复：reserved 转为 unknown，不释放或扣除余额。{normalized_note}")[:500],
        created_at=at,
    ))
    return reservation


def _reservation_for_update(db: Session, reservation_id: int, *, wallet_first: bool):
    row = db.query(BillingReservation).filter_by(id=reservation_id).first()
    if row is None:
        raise ReconciliationError("模型预占记录不存在")
    parts = db.query(BillingReservationPart).filter_by(
        reservation_id=reservation_id,
    ).order_by(BillingReservationPart.id).all()
    locked_wallets = None
    if wallet_first:
        try:
            locked_wallets = ledger._lock_wallets(db, ledger._reservation_wallet_ids(row, parts))
        except ledger.BillingError as exc:
            raise ReconciliationError(str(exc)) from exc
    row = db.query(BillingReservation).filter_by(id=reservation_id).populate_existing() \
        .with_for_update().first()
    if row is None:
        raise ReconciliationError("模型预占记录不存在")
    parts = (db.query(BillingReservationPart).filter_by(reservation_id=reservation_id)
             .order_by(BillingReservationPart.id).populate_existing().with_for_update().all())
    if locked_wallets is not None:
        try:
            ledger._require_locked_reservation_wallets(row, parts, locked_wallets)
        except ledger.BillingError as exc:
            raise ReconciliationError(str(exc)) from exc
    return row, parts


def _require_complete_hold(reservation: BillingReservation, parts: list[BillingReservationPart]) -> None:
    if any(part.charged_milli is not None for part in parts):
        raise ReconciliationError("预占明细已经部分扣款，必须保持冻结并升级处理")
    if sum(int(part.reserved_milli) for part in parts) != int(reservation.requested_milli):
        raise ReconciliationError("预占明细与原始请求金额不一致，不能自动调整账本")


def propose_reconciliation(db: Session, *, reservation_id: int, actor_id: int,
                           resolution: str, evidence_reference: str,
                           evidence_sha256: str, note: str,
                           provider_request_id: str | None,
                           input_tokens: int | None, output_tokens: int | None,
                           tool_successful_calls: int | None = None):
    if type(actor_id) is not int or actor_id <= 0:
        raise ReconciliationError("审核账号无效")
    if resolution not in _RESOLUTIONS:
        raise ReconciliationError("对账处理方式无效")
    reservation, parts = _reservation_for_update(db, reservation_id, wallet_first=False)
    try:
        evidence_reference, evidence_sha256, note = _validate_evidence(
            evidence_reference, evidence_sha256, note,
        )
    except ReconciliationError as exc:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=resolution, reason=str(exc))
    if reservation.status != "unknown":
        _block_action(
            db, reservation_id=reservation_id, actor_id=actor_id, resolution=resolution,
            reason="只有状态为 unknown 的终态预占可提交人工提案；reserved 可能仍在上游处理中",
        )
    try:
        _require_complete_hold(reservation, parts)
    except ReconciliationError as exc:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=resolution, reason=str(exc))
    pending = _pending(_events(db, reservation_id, lock=True))
    if pending:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=resolution, reason="该预占已有待复核提案", proposal=pending)

    is_tool = reservation.billable_kind in {"search", "fetch"}
    normalized_request_id = None
    input_value = output_value = actual_milli = None
    successful_tool_calls = None
    if resolution == "settle":
        try:
            normalized_request_id = _required_text(provider_request_id or "", "供应商请求 ID", 128)
        except ReconciliationError as exc:
            _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                          resolution=resolution, reason=str(exc))
        if is_tool:
            if (type(tool_successful_calls) is not int or tool_successful_calls not in {0, 1}
                    or input_tokens is not None or output_tokens is not None):
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution,
                              reason="工具对账必须明确提供 0/1 成功次数，且不得伪造模型 Token")
            if reservation.provider_attempt_started_at is None:
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution, reason="工具没有已提交的 provider attempt，不能按已发送结算")
            if reservation.server_tool_search_calls is not None or reservation.server_tool_fetch_calls is not None:
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution, reason="工具 reservation 混入了供应商内置工具计数")
            price = reservation.tool_price_milli_per_success
            if (reservation.billable_kind != reservation.tool_name or type(price) is not int or price <= 0
                    or not reservation.pricing_version or not reservation.provider_identity_version):
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution, reason="工具费率或供应商快照缺失，预占继续冻结")
            actual_milli = price * tool_successful_calls
            successful_tool_calls = tool_successful_calls
        else:
            if tool_successful_calls is not None:
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution, reason="模型 Token reservation 不得附带 Search/Fetch 成功次数")
            if type(input_tokens) is not int or type(output_tokens) is not int \
                    or not 0 <= input_tokens <= _MAX_DB_INTEGER or not 0 <= output_tokens <= _MAX_DB_INTEGER:
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution, reason="供应商输入和输出 Token 必须为非负整数")
            if reservation.server_tool_search_calls is None or reservation.server_tool_fetch_calls is None:
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution,
                              reason="缺少供应商 Search/Fetch 用量计数，不能推断为零；预占继续冻结")
            if ((reservation.server_tool_search_calls or 0) > 0
                    or (reservation.server_tool_fetch_calls or 0) > 0):
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution,
                              reason="存在未计价 Search/Fetch 用量，不能结算，预占继续冻结")
            try:
                actual_milli = ledger.quote_reservation_milli(
                    db, request_key=reservation.request_key,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                )
            except ledger.BillingError as exc:
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution, reason=f"缺少有效供应商身份或费率快照：{exc}")
            if actual_milli > reservation.requested_milli:
                _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                              resolution=resolution, reason="供应商用量按原费率计算后超过原预占，不能结算")
            input_value, output_value = input_tokens, output_tokens
    elif (provider_request_id is not None or input_tokens is not None or output_tokens is not None
          or tool_successful_calls is not None):
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=resolution, reason="未发送释放提案不得附带供应商请求 ID 或 Token 用量")
    if resolution == "release_not_sent" and reservation.provider_attempt_started_at is not None:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=resolution, reason="工具 provider attempt 已持久化，不能提案为从未发送")
    if resolution == "release_not_sent" and any((
        reservation.input_tokens is not None,
        reservation.output_tokens is not None,
        reservation.server_tool_search_calls is not None,
        reservation.server_tool_fetch_calls is not None,
    )):
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=resolution, reason="预占已留有供应商用量证据，不得按未发送释放")

    event = BillingReservationReconciliationEvent(
        reservation_id=reservation.id,
        related_event_id=None,
        actor_id=actor_id,
        decision="proposed",
        resolution=resolution,
        provider_request_id=normalized_request_id,
        input_tokens=input_value,
        output_tokens=output_value,
        actual_milli=actual_milli,
        tool_successful_calls=successful_tool_calls,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha256,
        note=note,
        created_at=_now(),
    )
    db.add(event)
    db.flush()
    return event


def decide_reconciliation(db: Session, *, reservation_id: int, actor_id: int,
                         decision: str, note: str):
    if type(actor_id) is not int or actor_id <= 0:
        raise ReconciliationError("审核账号无效")
    if decision not in {"approve", "reject"}:
        raise ReconciliationError("审核决定无效")
    normalized_note = _required_text(note, "复核说明", 500)
    reservation, _parts = _reservation_for_update(db, reservation_id, wallet_first=True)
    events = _events(db, reservation_id, lock=True)
    proposal = _pending(events)
    if proposal is None:
        prior_proposal = next((event for event in reversed(events) if event.decision == "proposed"), None)
        if prior_proposal:
            _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                          resolution=prior_proposal.resolution,
                          reason="没有待复核的对账提案，账本未变更", proposal=prior_proposal)
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=None, reason="没有待复核的对账提案，账本未变更")
    if proposal.actor_id == actor_id:
        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                      resolution=proposal.resolution, reason="提案人不能审批本人提案", proposal=proposal)
    if decision == "approve":
        if reservation.status != "unknown":
            _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                          resolution=proposal.resolution,
                          reason="模型预占状态已变化，必须刷新后重新核对", proposal=proposal)
        try:
            if proposal.resolution == "settle":
                if reservation.billable_kind in {"search", "fetch"}:
                    count = proposal.tool_successful_calls
                    if (type(count) is not int or count not in {0, 1}
                            or reservation.provider_attempt_started_at is None
                            or reservation.tool_name != reservation.billable_kind
                            or type(reservation.tool_price_milli_per_success) is not int):
                        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                                      resolution=proposal.resolution,
                                      reason="工具 attempt、成功次数或费率快照已变化，不能批准结算",
                                      proposal=proposal)
                    actual_milli = reservation.tool_price_milli_per_success * count
                    if actual_milli != proposal.actual_milli:
                        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                                      resolution=proposal.resolution,
                                      reason="工具提案金额与原单位费率快照重算结果不一致", proposal=proposal)
                    reservation = ledger.settle_tool(
                        db, request_key=reservation.request_key, tool_successful_calls=count,
                    )
                else:
                    if reservation.server_tool_search_calls is None or reservation.server_tool_fetch_calls is None:
                        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                                      resolution=proposal.resolution,
                                      reason="提案提交后仍缺少供应商 Search/Fetch 用量计数，不能批准结算",
                                      proposal=proposal)
                    actual_milli = ledger.quote_reservation_milli(
                        db, request_key=reservation.request_key,
                        input_tokens=int(proposal.input_tokens), output_tokens=int(proposal.output_tokens),
                    )
                    if actual_milli != proposal.actual_milli:
                        _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                                      resolution=proposal.resolution,
                                      reason="提案金额与原费率快照重算结果不一致", proposal=proposal)
                    reservation = ledger.settle(
                        db, request_key=reservation.request_key, actual_milli=actual_milli,
                        input_tokens=int(proposal.input_tokens), output_tokens=int(proposal.output_tokens),
                        server_tool_search_calls=reservation.server_tool_search_calls,
                        server_tool_fetch_calls=reservation.server_tool_fetch_calls,
                    )
            else:
                if any((
                    reservation.input_tokens is not None,
                    reservation.output_tokens is not None,
                    reservation.server_tool_search_calls is not None,
                    reservation.server_tool_fetch_calls is not None,
                    reservation.provider_attempt_started_at is not None,
                )):
                    _block_action(db, reservation_id=reservation_id, actor_id=actor_id,
                                  resolution=proposal.resolution,
                                  reason="提案提交后已记录供应商用量，不能批准未发送释放",
                                  proposal=proposal)
                reservation = ledger.release_verified_not_sent(
                    db, reservation.request_key,
                )
        except ledger.BillingError as exc:
            _block_after_ledger_rollback(
                db, reservation_id=reservation_id, actor_id=actor_id,
                proposal_id=proposal.id, resolution=proposal.resolution,
                reason=f"账本状态不允许执行该提案：{exc}",
            )

    event = BillingReservationReconciliationEvent(
        reservation_id=reservation.id,
        related_event_id=proposal.id,
        actor_id=actor_id,
        decision="approved" if decision == "approve" else "rejected",
        resolution=proposal.resolution,
        provider_request_id=proposal.provider_request_id,
        input_tokens=proposal.input_tokens,
        output_tokens=proposal.output_tokens,
        tool_successful_calls=proposal.tool_successful_calls,
        actual_milli=reservation.actual_milli if decision == "approve" and proposal.resolution == "settle"
        else proposal.actual_milli,
        evidence_reference=proposal.evidence_reference,
        evidence_sha256=proposal.evidence_sha256,
        note=normalized_note,
        created_at=_now(),
    )
    db.add(event)
    db.flush()
    return reservation, event
