"""Audited two-person cancellation for future rate catalogs."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.billing_ledger import BillingRateCancelRequest, BillingRateCatalog, BillingRateCatalogEvent
from app.services.billing_rate_catalog_service import RateCatalogError


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _note(note: str):
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
        raise RateCatalogError("说明必须为 1 至 500 个字符")
    return note.strip()


def _catalog(db: Session, catalog_id: int):
    row = db.query(BillingRateCatalog).filter_by(id=catalog_id).populate_existing().with_for_update().first()
    if row is None:
        raise RateCatalogError("费率目录不存在")
    return row


def propose(db: Session, *, catalog_id: int, actor_id: int, note: str):
    note = _note(note)
    catalog = _catalog(db, catalog_id)
    at = _now()
    if catalog.status != "approved" or catalog.effective_at <= at:
        raise RateCatalogError("仅可申请取消尚未生效的已批准目录")
    candidate = db.query(BillingRateCancelRequest.id).filter_by(active_catalog_id=catalog_id).first()
    if candidate is not None:
        prior = (db.query(BillingRateCancelRequest).filter_by(id=candidate.id)
                 .populate_existing().with_for_update().one())
        if prior.proposed_by == actor_id and prior.proposal_note == note:
            return prior
        raise RateCatalogError("该目录已有待审取消申请")
    row = BillingRateCancelRequest(catalog_id=catalog_id, active_catalog_id=catalog_id, status="pending",
                                   proposed_by=actor_id, proposal_note=note, proposed_at=at)
    db.add(row)
    db.flush()
    return row


def decide(db: Session, *, request_id: int, actor_id: int, decision: str, note: str):
    note = _note(note)
    if decision not in {"approve", "reject"}:
        raise RateCatalogError("取消决定无效")
    candidate = db.query(BillingRateCancelRequest.catalog_id).filter_by(id=request_id).first()
    if candidate is None:
        raise RateCatalogError("取消申请不存在")
    catalog = _catalog(db, candidate.catalog_id)
    row = (db.query(BillingRateCancelRequest).filter_by(id=request_id)
           .populate_existing().with_for_update().one())
    status = "approved" if decision == "approve" else "rejected"
    if row.status != "pending":
        if row.status == status and row.decided_by == actor_id and row.decision_note == note:
            return row
        raise RateCatalogError("取消申请已处理，请刷新状态")
    if row.proposed_by == actor_id:
        raise RateCatalogError("取消提案人不能复核本人申请")
    at = _now()
    if catalog.status != "approved" or catalog.effective_at <= at:
        row.status, row.active_catalog_id = "expired", None
        row.decided_by, row.decided_at, row.decision_note = actor_id, at, "目录已生效或状态变化，未取消"
        db.flush()
        return row
    if decision == "approve":
        changed = (db.query(BillingRateCatalog).filter(
            BillingRateCatalog.id == catalog.id, BillingRateCatalog.status == "approved",
            BillingRateCatalog.effective_at > at,
        ).update({BillingRateCatalog.status: "canceled", BillingRateCatalog.canceled_by: actor_id,
                  BillingRateCatalog.canceled_at: at, BillingRateCatalog.approved_effective_at: None,
                  BillingRateCatalog.transition_note: note}, synchronize_session=False))
        if changed != 1:
            raise RateCatalogError("目录已生效或被其他操作处理")
        db.add(BillingRateCatalogEvent(catalog_id=catalog.id, actor_id=actor_id,
                                       action="canceled", note=note, created_at=at))
    row.status, row.active_catalog_id = status, None
    row.decided_by, row.decided_at, row.decision_note = actor_id, at, note
    db.flush()
    return row
