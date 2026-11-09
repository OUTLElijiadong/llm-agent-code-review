"""只读计费视图与经过签名验证的支付通知入口；收款下单默认关闭。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from pydantic import Field
from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_super_admin
from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import (
    BillingOrder,
    BillingRateCancelRequest,
    BillingRateCatalog,
    BillingRateReviewerGrant,
    BillingReconciliationReviewerGrant,
    BillingRefund,
    BillingRefundReviewerGrant,
    BillingReservation,
    BillingReservationPart,
    BillingReservationReconciliationEvent,
    BillingStatementBatch,
    BillingStatementReviewEvent,
    BillingStatementRow,
    BillingWallet,
)
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import (
    billing_c4_reviewer_service,
    billing_checkout_service,
    billing_entitlement_service,
    billing_ledger_service,
    billing_quota_service,
    billing_rate_cancel_service,
    billing_rate_catalog_service,
    billing_rate_reviewer_service,
    billing_refund_reviewer_service,
    billing_reservation_reconciliation_service,
    enterprise_org_service,
)
from app.services.billing_payment_verification import PaymentVerificationError, extract_wechat_order_no
from app.services.billing_statement_service import (
    MAX_STATEMENT_FILE_BYTES,
    MAX_STATEMENT_OFFSET,
    StatementImportError,
    import_statement,
    review_statement_row,
)

router = APIRouter(prefix="/billing", tags=["积分与计费"])

# 代码级开售锁：所有旧模型调用计量、权益执行、退款/对账和商户沙箱验收完成后，
# 才能在经过审查的代码变更中打开。部署环境变量不能单独越过此锁。
BILLING_SALES_CODE_GATE_OPEN = False


class OrderRequest(StrictInputModel):
    product: str
    quantity: int = 1
    channel: str
    client_request_id: str | None = Field(default=None, min_length=1, max_length=80)
    enterprise_id: int | None = Field(default=None, gt=0)
    enterprise_name: str | None = Field(default=None, min_length=1, max_length=120)


class RefundRequest(StrictInputModel):
    reason: str = Field(min_length=1, max_length=500)


class RefundDecision(StrictInputModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=500)


class PermanentReviewerChange(StrictInputModel):
    active: bool
    reason: str


class StatementReview(StrictInputModel):
    decision: Literal["acknowledge", "investigating", "resolved_external"]
    note: str = Field(min_length=1, max_length=500)


class StatementFetch(StrictInputModel):
    channel: Literal["alipay", "wechat"]
    bill_date: date
    bill_type: Literal["trade", "ALL", "SUCCESS", "REFUND"]


class ReservationReconciliationProposal(StrictInputModel):
    resolution: Literal["settle", "release_not_sent"]
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    input_tokens: int | None = Field(default=None, strict=True, ge=0, le=2**63 - 1)
    output_tokens: int | None = Field(default=None, strict=True, ge=0, le=2**63 - 1)
    tool_successful_calls: int | None = Field(default=None, strict=True, ge=0, le=1)
    evidence_reference: str = Field(min_length=1, max_length=240)
    evidence_sha256: str = Field(min_length=64, max_length=64)
    note: str = Field(min_length=1, max_length=500)


class ReservationReconciliationDecision(StrictInputModel):
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=1, max_length=500)


class ReservationStaleRecovery(StrictInputModel):
    note: str = Field(min_length=1, max_length=500)


class C4ReviewerChange(StrictInputModel):
    active: bool = Field(strict=True)
    reason: str = Field(min_length=1, max_length=500)


class MonthlyLimitSet(StrictInputModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    scope: Literal["enterprise", "group", "member"]
    limit_credits: int = Field(strict=True, ge=0, le=billing_quota_service.MAX_LIMIT_CREDITS)
    group_id: int | None = Field(default=None, gt=0)
    member_id: int | None = Field(default=None, gt=0)


class RateCatalogItemInput(StrictInputModel):
    model_name: str = Field(min_length=1, max_length=80)
    input_rate_numerator: int = Field(strict=True, ge=1, le=1_000_000_000)
    input_rate_denominator: int = Field(strict=True, ge=1, le=1_000_000)
    output_rate_numerator: int = Field(strict=True, ge=1, le=1_000_000_000)
    output_rate_denominator: int = Field(strict=True, ge=1, le=1_000_000)
    provider_input_usd_micros_per_million: int = Field(strict=True, ge=0, le=2**63 - 1)
    provider_output_usd_micros_per_million: int = Field(strict=True, ge=0, le=2**63 - 1)
    provider_input_cny_micros_per_million: int = Field(strict=True, ge=1, le=2**63 - 1)
    provider_output_cny_micros_per_million: int = Field(strict=True, ge=1, le=2**63 - 1)


class RateCatalogCreate(StrictInputModel):
    effective_at: datetime
    source_url: str = Field(min_length=1, max_length=512)
    source_checked_at: datetime
    model_cny_source_url: str = Field(min_length=1, max_length=512)
    model_cny_source_checked_at: datetime
    fx_source_url: str = Field(min_length=1, max_length=512)
    fx_source_checked_at: datetime
    fx_cny_per_usd_scaled: int = Field(strict=True, ge=1, le=1_000_000_000)
    change_note: str = Field(min_length=1, max_length=500)
    tool_cny_source_url: str | None = Field(default=None, max_length=512)
    tool_cny_source_checked_at: datetime | None = None
    tool_usd_source_url: str | None = Field(default=None, max_length=512)
    tool_usd_source_checked_at: datetime | None = None
    tool_search_cost_cny_micros_per_success: int = Field(strict=True, ge=0, le=2**63 - 1, default=0)
    tool_search_price_milli_per_success: int = Field(strict=True, ge=0, le=2**63 - 1, default=0)
    tool_fetch_cost_cny_micros_per_success: int = Field(strict=True, ge=0, le=2**63 - 1, default=0)
    tool_fetch_price_milli_per_success: int = Field(strict=True, ge=0, le=2**63 - 1, default=0)
    items: list[RateCatalogItemInput] = Field(min_length=1, max_length=20)


class RateCatalogDecision(StrictInputModel):
    note: str = Field(min_length=1, max_length=500)


class RateCancelDecision(StrictInputModel):
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=1, max_length=500)


@router.get("/pricing")
def pricing(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        current = billing_ledger_service.active_pricing_metadata(db)
    except billing_ledger_service.BillingError as exc:
        raise HTTPException(status_code=503, detail="当前计费目录不可用，已停止报价与计费") from exc
    return Resp(data={
        "currency": "CNY",
        "billing_unit": {
            "unit": "milli_credit", "milli_per_credit": 1000,
            "rate_unit": "milli_credit_per_token",
            "rounding": "ceil_per_model_request",
        },
        "reviewer_month": {"price_fen": 4900, "credits": 5000},
        "enterprise_seat_month": {"price_fen": 6900, "credits": 5000},
        "pricing_version": current["pricing_version"],
        "pricing_effective_at": current["effective_at"],
        "next_pricing_effective_at": current["next_effective_at"],
        "credit_pack": current["credit_pack"],
        "model_milli_per_token": current["model_milli_per_token"],
        "model_pricing_ready": current["model_pricing_ready"],
        "tool_milli_credits_per_success": current["tool_milli_credits_per_success"],
        "billable_models": current["billable_models"],
        "channels": {"alipay": _sales_ready("alipay"), "wechat": _sales_ready("wechat")},
        "sales_enabled": _sales_ready("alipay") or _sales_ready("wechat"),
    })


@router.get("/admin/rate-catalogs")
def list_rate_catalogs(limit: int = Query(default=100, ge=1, le=100), db: Session = Depends(get_db),
                       _admin: User = Depends(require_super_admin)):
    try:
        rows = billing_rate_catalog_service.list_catalogs(db, limit=limit)
        return Resp(data={"items": [billing_rate_catalog_service.catalog_view(db, row) for row in rows]})
    except billing_rate_catalog_service.RateCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/rate-catalogs")
def create_rate_catalog(payload: RateCatalogCreate, db: Session = Depends(get_db),
                         admin: User = Depends(require_super_admin)):
    try:
        row = billing_rate_catalog_service.create_draft(
            db, actor_id=int(admin.id), effective_at=payload.effective_at,
            source_url=payload.source_url, source_checked_at=payload.source_checked_at,
            model_cny_source_url=payload.model_cny_source_url,
            model_cny_source_checked_at=payload.model_cny_source_checked_at,
            fx_source_url=payload.fx_source_url, fx_source_checked_at=payload.fx_source_checked_at,
            fx_cny_per_usd_scaled=payload.fx_cny_per_usd_scaled,
            change_note=payload.change_note,
            tool_cny_source_url=payload.tool_cny_source_url,
            tool_cny_source_checked_at=payload.tool_cny_source_checked_at,
            tool_usd_source_url=payload.tool_usd_source_url,
            tool_usd_source_checked_at=payload.tool_usd_source_checked_at,
            tool_search_cost_cny_micros_per_success=payload.tool_search_cost_cny_micros_per_success,
            tool_search_price_milli_per_success=payload.tool_search_price_milli_per_success,
            tool_fetch_cost_cny_micros_per_success=payload.tool_fetch_cost_cny_micros_per_success,
            tool_fetch_price_milli_per_success=payload.tool_fetch_price_milli_per_success,
            items=[item.model_dump() for item in payload.items],
        )
        result = billing_rate_catalog_service.catalog_view(db, row)
        db.commit()
        return Resp(data=result)
    except billing_rate_catalog_service.RateCatalogError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="费率版本冲突，请刷新目录后重试") from exc


@router.post("/admin/rate-catalogs/{catalog_id}/approve")
def approve_rate_catalog(catalog_id: int, db: Session = Depends(get_db),
                         admin: User = Depends(require_super_admin)):
    try:
        row = billing_rate_catalog_service.approve_draft(
            db, catalog_id=catalog_id, actor_id=int(admin.id),
        )
        result = billing_rate_catalog_service.catalog_view(db, row)
        db.commit()
        return Resp(data=result)
    except billing_rate_catalog_service.RateCatalogError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该生效时间已被并发审批占用，请刷新目录后重试") from exc


@router.post("/admin/rate-catalogs/{catalog_id}/reject")
def reject_rate_catalog(catalog_id: int, payload: RateCatalogDecision,
                        db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    try:
        row = billing_rate_catalog_service.reject_draft(
            db, catalog_id=catalog_id, actor_id=int(admin.id), note=payload.note,
        )
        result = billing_rate_catalog_service.catalog_view(db, row)
        db.commit()
        return Resp(data=result)
    except billing_rate_catalog_service.RateCatalogError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/rate-catalogs/{catalog_id}/cancel")
def cancel_rate_catalog(catalog_id: int, payload: RateCatalogDecision,
                        db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    raise HTTPException(status_code=409, detail="费率取消需要发起申请并由另一人复核")


@router.put("/admin/rate-reviewers/{user_id}")
def set_rate_reviewer(user_id: int, payload: C4ReviewerChange, db: Session = Depends(get_db),
                      admin: User = Depends(require_super_admin)):
    billing_rate_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
    row = billing_rate_reviewer_service.set_reviewer(
        db, actor_id=int(admin.id), user_id=user_id, active=payload.active, reason=payload.reason,
    )
    db.commit()
    return Resp(data={"user_id": row.user_id, "active": bool(row.active)})


@router.get("/admin/rate-reviewers")
def list_rate_reviewers(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000),
                        db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    billing_rate_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
    query = db.query(BillingRateReviewerGrant)
    total = query.count()
    rows = query.order_by(BillingRateReviewerGrant.id.desc()).offset(offset).limit(limit).all()
    eligible = billing_c4_reviewer_service.reviewer_eligibility(db, [int(row.user_id) for row in rows])
    users = {int(user.id): user for user in db.query(User.id, User.username, User.nickname).filter(
        User.id.in_([row.user_id for row in rows]),
    ).all()} if rows else {}
    return Resp(data={"items": [{"user_id": row.user_id, "active": bool(row.active),
                                  "eligible": eligible.get(int(row.user_id), False),
                                  "username": users[int(row.user_id)].username if int(row.user_id) in users else None,
                                  "nickname": users[int(row.user_id)].nickname if int(row.user_id) in users else None,
                                  "granted_at": row.granted_at, "revoked_at": row.revoked_at}
                                 for row in rows], "total": total, "limit": limit, "offset": offset})


@router.get("/rate-reviews")
def list_rate_reviews(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_rate_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    rows = (db.query(BillingRateCatalog).filter_by(status="draft")
            .order_by(BillingRateCatalog.id.desc()).limit(100).all())
    return Resp(data={"items": [billing_rate_catalog_service.catalog_view(db, row) for row in rows]})


@router.get("/rate-reviews/{catalog_id}")
def get_rate_review(catalog_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_rate_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    row = db.query(BillingRateCatalog).filter_by(id=catalog_id, status="draft").first()
    if row is None:
        raise HTTPException(status_code=404, detail="待审费率目录不存在")
    return Resp(data=billing_rate_catalog_service.catalog_view(db, row))


@router.post("/rate-reviews/{catalog_id}/approve")
def approve_rate_review(catalog_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_rate_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    try:
        row = billing_rate_catalog_service.approve_draft(db, catalog_id=catalog_id, actor_id=int(user.id))
        result = billing_rate_catalog_service.catalog_view(db, row)
        db.commit()
        return Resp(data=result)
    except (billing_rate_catalog_service.RateCatalogError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="费率审批失败，请核对来源、权限和目录状态") from exc


@router.post("/rate-reviews/{catalog_id}/reject")
def reject_rate_review(catalog_id: int, payload: RateCatalogDecision,
                       db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_rate_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    try:
        row = billing_rate_catalog_service.reject_draft(db, catalog_id=catalog_id, actor_id=int(user.id),
                                                        note=payload.note)
        result = billing_rate_catalog_service.catalog_view(db, row)
        db.commit()
        return Resp(data=result)
    except billing_rate_catalog_service.RateCatalogError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _rate_cancel_view(row: BillingRateCancelRequest):
    return {"id": row.id, "catalog_id": row.catalog_id, "status": row.status,
            "proposed_by": row.proposed_by, "proposal_note": row.proposal_note, "proposed_at": row.proposed_at,
            "decided_by": row.decided_by, "decision_note": row.decision_note, "decided_at": row.decided_at}


@router.post("/rate-catalogs/{catalog_id}/cancel-requests")
def propose_rate_cancellation(catalog_id: int, payload: RateCatalogDecision,
                              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version = int(user.id), int(user.token_version or 0)
    billing_rate_reviewer_service.require_reviewer(db, user_id, token_version)
    try:
        row = billing_rate_cancel_service.propose(db, catalog_id=catalog_id, actor_id=user_id, note=payload.note)
        result = _rate_cancel_view(row)
        db.commit()
        return Resp(data=result)
    except IntegrityError:
        db.rollback()
        billing_rate_reviewer_service.require_reviewer(db, user_id, token_version)
        try:
            row = billing_rate_cancel_service.propose(db, catalog_id=catalog_id, actor_id=user_id, note=payload.note)
            result = _rate_cancel_view(row)
            db.commit()
            return Resp(data=result)
        except (billing_rate_catalog_service.RateCatalogError, IntegrityError) as retry_exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="取消申请冲突，请刷新目录和申请状态") from retry_exc
    except billing_rate_catalog_service.RateCatalogError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/rate-cancel-requests")
def list_rate_cancellations(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_rate_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    rows = (db.query(BillingRateCancelRequest).filter_by(status="pending")
            .order_by(BillingRateCancelRequest.id.desc()).limit(100).all())
    catalog_ids = [row.catalog_id for row in rows]
    catalogs = {row.id: row for row in db.query(BillingRateCatalog).filter(
        BillingRateCatalog.id.in_(catalog_ids),
    ).all()} if catalog_ids else {}
    fields = (
        "id", "version_key", "status", "effective_at", "source_url", "source_checked_at",
        "model_cny_source_url", "model_cny_source_checked_at", "fx_source_url", "fx_source_checked_at",
        "source_sha256", "fx_cny_per_usd", "credit_pack_price_fen", "credit_pack_credits", "items",
        "tool_cny_source_url", "tool_cny_source_checked_at", "tool_usd_source_url", "tool_usd_source_checked_at",
        "tool_search_cost_cny_micros_per_success", "tool_search_price_milli_per_success",
        "tool_fetch_cost_cny_micros_per_success", "tool_fetch_price_milli_per_success", "change_note",
    )
    items = []
    for row in rows:
        catalog = catalogs.get(row.catalog_id)
        if catalog is None:
            raise HTTPException(status_code=409, detail="取消申请对应的费率目录不存在")
        view = billing_rate_catalog_service.catalog_view(db, catalog)
        items.append({**_rate_cancel_view(row), "catalog": {field: view[field] for field in fields}})
    return Resp(data={"items": items})


@router.post("/rate-cancel-requests/{request_id}/decision")
def decide_rate_cancellation(request_id: int, payload: RateCancelDecision,
                             db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_rate_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    try:
        row = billing_rate_cancel_service.decide(db, request_id=request_id, actor_id=int(user.id),
                                                 decision=payload.decision, note=payload.note)
        result = _rate_cancel_view(row)
        db.commit()
        return Resp(data=result)
    except billing_rate_catalog_service.RateCatalogError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _wallet_balance(db, kind, owner_id):
    wallet = db.query(BillingWallet).filter_by(owner_type=kind, owner_id=owner_id).first()
    return {"available_milli": billing_ledger_service.available_milli(db, wallet.id) if wallet else 0}


@router.get("/wallet")
def personal_wallet(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_wallet_balance(db, "user", user.id))


@router.get("/entitlement")
def personal_entitlement(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data={"reviewer": billing_entitlement_service.has_reviewer_entitlement(db, user.id),
                      "enforcement_enabled": settings.billing_entitlement_enforcement_ready})


@router.post("/permanent-reviewers/{user_id}")
def change_permanent_reviewer(user_id: int, payload: PermanentReviewerChange, db: Session = Depends(get_db),
                              admin: User = Depends(require_super_admin)):
    try:
        row = billing_entitlement_service.set_permanent(db, actor_id=admin.id, user_id=user_id,
                                                       active=payload.active, reason=payload.reason)
        if payload.active:
            billing_entitlement_service.issue_permanent_month(db, user_id)
        billing_entitlement_service.sync_reviewer_role(db, user_id)
        db.commit()
        return Resp(data={"user_id": row.user_id, "status": row.status})
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/enterprises/{enterprise_id}/wallet")
def enterprise_wallet(enterprise_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enterprise_org_service.require_enterprise_admin(db, enterprise_id, user.id)
    return Resp(data=_wallet_balance(db, "enterprise", enterprise_id))


def _payment_totals(db, filters, start, end):
    """按支付明细归集；独立查询以避免一个请求的多钱包明细重复计算 Token。"""
    day_expr = func.date(BillingReservation.created_at)
    rows = (db.query(day_expr.label("day"), BillingWallet.owner_type,
                     func.sum(BillingReservationPart.charged_milli).label("amount"))
            .select_from(BillingReservation)
            .join(BillingReservationPart, BillingReservationPart.reservation_id == BillingReservation.id)
            .join(BillingWallet, BillingWallet.id == BillingReservationPart.wallet_id)
            .filter(*filters, BillingReservation.status == "settled",
                    BillingReservation.created_at >= start, BillingReservation.created_at < end)
            .group_by(day_expr, BillingWallet.owner_type).all())
    return {(str(row.day), row.owner_type): int(row.amount or 0) for row in rows}


def _usage_trend(db, filters, days, *, fallback_user_id=None):
    """UTC daily amounts; unknown usage is counted rather than fabricated as zero."""
    today = datetime.now(timezone.utc).date()
    start_day = today - timedelta(days=days - 1)
    start = datetime.combine(start_day, datetime.min.time())
    end = datetime.combine(today + timedelta(days=1), datetime.min.time())
    day_expr = func.date(BillingReservation.created_at)
    model_kind = BillingReservation.billable_kind == "model"
    search_kind = BillingReservation.billable_kind == "search"
    fetch_kind = BillingReservation.billable_kind == "fetch"
    reported_model = and_(model_kind, BillingReservation.usage_state == "reported")
    search_unknown = and_(search_kind, BillingReservation.status == "unknown")
    fetch_unknown = and_(fetch_kind, BillingReservation.status == "unknown")
    rows = (db.query(
        day_expr.label("day"),
        func.sum(case((reported_model, BillingReservation.input_tokens), else_=0)).label("input_tokens"),
        func.sum(case((reported_model, BillingReservation.output_tokens), else_=0)).label("output_tokens"),
        func.sum(case((reported_model, 1), else_=0)).label("reported_calls"),
        func.sum(case((and_(model_kind, BillingReservation.usage_state == "unknown"), 1), else_=0))
        .label("unknown_calls"),
        func.sum(case((and_(model_kind, BillingReservation.usage_state == "pending"), 1), else_=0))
        .label("pending_calls"),
        func.sum(case((and_(model_kind, BillingReservation.status == "settled"),
                       BillingReservation.actual_milli), else_=0))
        .label("charged_milli"),
        func.sum(case((and_(model_kind, BillingReservation.server_tool_search_calls.is_not(None)),
                       BillingReservation.server_tool_search_calls), else_=0)).label("server_tool_search_calls"),
        func.sum(case((and_(model_kind, BillingReservation.server_tool_fetch_calls.is_not(None)),
                       BillingReservation.server_tool_fetch_calls), else_=0)).label("server_tool_fetch_calls"),
        func.sum(case((and_(model_kind, BillingReservation.server_tool_search_calls.is_not(None)), 1), else_=0))
        .label("server_tool_usage_reported_calls"),
        func.sum(case((and_(search_kind, BillingReservation.tool_successful_calls == 1), 1), else_=0))
        .label("search_success_calls"),
        func.sum(case((and_(fetch_kind, BillingReservation.tool_successful_calls == 1), 1), else_=0))
        .label("fetch_success_calls"),
        func.sum(case((and_(search_kind, BillingReservation.status == "settled"),
                       BillingReservation.actual_milli), else_=0)).label("search_charged_milli"),
        func.sum(case((and_(fetch_kind, BillingReservation.status == "settled"),
                       BillingReservation.actual_milli), else_=0)).label("fetch_charged_milli"),
        func.sum(case((search_unknown, 1), else_=0)).label("search_unknown_calls"),
        func.sum(case((fetch_unknown, 1), else_=0)).label("fetch_unknown_calls"),
        func.sum(case((search_unknown, BillingReservation.requested_milli), else_=0))
        .label("search_unknown_milli"),
        func.sum(case((fetch_unknown, BillingReservation.requested_milli), else_=0))
        .label("fetch_unknown_milli"),
        func.sum(case((and_(search_kind, BillingReservation.status == "reserved"), 1), else_=0))
        .label("search_pending_calls"),
        func.sum(case((and_(search_kind, BillingReservation.status == "reserved"),
                       BillingReservation.requested_milli), else_=0)).label("search_pending_milli"),
        func.sum(case((and_(fetch_kind, BillingReservation.status == "reserved"), 1), else_=0))
        .label("fetch_pending_calls"),
        func.sum(case((and_(fetch_kind, BillingReservation.status == "reserved"),
                       BillingReservation.requested_milli), else_=0)).label("fetch_pending_milli"),
        func.sum(case((and_(search_kind, or_(
            BillingReservation.usage_state == "not_billable",
            and_(BillingReservation.status == "settled", BillingReservation.actual_milli == 0,
                 BillingReservation.tool_successful_calls == 0),
        )), 1), else_=0))
        .label("search_not_billable_calls"),
        func.sum(case((and_(fetch_kind, or_(
            BillingReservation.usage_state == "not_billable",
            and_(BillingReservation.status == "settled", BillingReservation.actual_milli == 0,
                 BillingReservation.tool_successful_calls == 0),
        )), 1), else_=0))
        .label("fetch_not_billable_calls"),
    ).filter(*filters, BillingReservation.created_at >= start, BillingReservation.created_at < end)
     .group_by(day_expr).all())
    payments = _payment_totals(db, filters, start, end)
    fallback = _payment_totals(db, [BillingReservation.user_id == fallback_user_id,
                                    BillingReservation.enterprise_id.is_not(None),
                                    BillingWallet.owner_type == "user", BillingWallet.owner_id == fallback_user_id],
                               start, end) if fallback_user_id is not None else {}
    records = {str(row.day): row for row in rows}
    result = []
    for index in range(days):
        day = (start_day + timedelta(days=index)).isoformat()
        row = records.get(day)
        company_paid = payments.get((day, "enterprise"), 0)
        personal_paid = payments.get((day, "user"), 0)
        item = {"date": day, "input_tokens": int(row.input_tokens or 0) if row else 0,
                       "output_tokens": int(row.output_tokens or 0) if row else 0,
                       "reported_calls": int(row.reported_calls or 0) if row else 0,
                       "unknown_calls": int(row.unknown_calls or 0) if row else 0,
                       "pending_calls": int(row.pending_calls or 0) if row else 0,
                       "charged_milli": int(row.charged_milli or 0) if row else 0,
                       "server_tool_search_calls": int(row.server_tool_search_calls or 0) if row else 0,
                       "server_tool_fetch_calls": int(row.server_tool_fetch_calls or 0) if row else 0,
                       "server_tool_usage_reported_calls": (
                           int(row.server_tool_usage_reported_calls or 0) if row else 0
                       ),
                       "search_success_calls": int(row.search_success_calls or 0) if row else 0,
                       "fetch_success_calls": int(row.fetch_success_calls or 0) if row else 0,
                       "search_charged_milli": int(row.search_charged_milli or 0) if row else 0,
                       "fetch_charged_milli": int(row.fetch_charged_milli or 0) if row else 0,
                       "search_unknown_calls": int(row.search_unknown_calls or 0) if row else 0,
                       "fetch_unknown_calls": int(row.fetch_unknown_calls or 0) if row else 0,
                       "search_unknown_milli": int(row.search_unknown_milli or 0) if row else 0,
                       "fetch_unknown_milli": int(row.fetch_unknown_milli or 0) if row else 0,
                       "search_pending_calls": int(row.search_pending_calls or 0) if row else 0,
                       "search_pending_milli": int(row.search_pending_milli or 0) if row else 0,
                       "fetch_pending_calls": int(row.fetch_pending_calls or 0) if row else 0,
                       "fetch_pending_milli": int(row.fetch_pending_milli or 0) if row else 0,
                       "search_not_billable_calls": int(row.search_not_billable_calls or 0) if row else 0,
                       "fetch_not_billable_calls": int(row.fetch_not_billable_calls or 0) if row else 0,
                       "enterprise_wallet_charged_milli": company_paid,
                       "personal_wallet_charged_milli": personal_paid}
        if fallback_user_id is not None:
            item["enterprise_personal_fallback_milli"] = fallback.get((day, "user"), 0)
            # `personal_paid` already includes the employee wallet's share of enterprise calls.
            item["personal_wallet_total_milli"] = personal_paid
        result.append(item)
    return {"timezone": "UTC", "items": result,
            "scope_note": ("Token 按调用空间归属，钱包扣费按支付明细归属；"
                           "仅统计已接入商业计量的调用，未知用量不计入 Token 合计；"
                           "Search/Fetch 仅汇总账单记录的供应商成功次数，不作为积分扣费，也不代表缺失计数为零")}


@router.get("/usage")
def personal_usage(days: int = Query(default=30, ge=1, le=366), db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    # 个人视图按发起账号归集；企业钱包付款不会让员工看不到自己发起的调用。
    return Resp(data=_usage_trend(db, [BillingReservation.user_id == user.id], days,
                                  fallback_user_id=user.id))


@router.get("/enterprises/{enterprise_id}/usage")
def enterprise_usage(enterprise_id: int, days: int = Query(default=30, ge=1, le=366),
                     group_id: int | None = None, member_id: int | None = None,
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if group_id is None:
        enterprise_org_service.require_enterprise_admin(db, enterprise_id, user.id)
    else:
        enterprise_org_service.require_group_manager(db, enterprise_id, group_id, user.id)
    filters = [BillingReservation.enterprise_id == enterprise_id]
    if group_id is not None:
        filters.append(BillingReservation.group_id == group_id)
    if member_id is not None:
        filters.append(BillingReservation.user_id == member_id)
    return Resp(data=_usage_trend(db, filters, days))


@router.get("/enterprises/{enterprise_id}/limits")
def enterprise_limits(enterprise_id: int, period: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                      group_id: int | None = Query(None, gt=0), db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    period = period or billing_quota_service.current_period()
    rows = billing_quota_service.list_limits(db, enterprise_id, user.id, period, group_id=group_id)
    return Resp(data={"period": period, "timezone": "Asia/Shanghai", "items": rows})


@router.put("/enterprises/{enterprise_id}/limits")
def set_enterprise_limit(enterprise_id: int, payload: MonthlyLimitSet, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    try:
        row = billing_quota_service.set_limit(
            db, enterprise_id, user.id, period=payload.period, scope=payload.scope,
            limit_credits=payload.limit_credits, group_id=payload.group_id, member_id=payload.member_id,
        )
        db.commit()
        return Resp(data=row)
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orders")
def orders(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (db.query(BillingOrder).filter_by(user_id=user.id)
            .order_by(BillingOrder.id.desc()).limit(100).all())
    return Resp(data=[{"order_no": r.order_no, "product": r.product, "amount_fen": r.amount_fen,
                       "quantity": r.quantity, "channel": r.channel, "status": r.status,
                       "payment_status": r.payment_status, "created_at": r.created_at} for r in rows])


@router.get("/enterprises/{enterprise_id}/orders")
def enterprise_orders(enterprise_id: int, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Company-wide purchase history is visible only to the owner and admins."""
    enterprise_org_service.require_enterprise_admin(db, enterprise_id, user.id)
    rows = (db.query(BillingOrder).filter_by(enterprise_id=enterprise_id)
            .order_by(BillingOrder.id.desc()).limit(100).all())
    return Resp(data=[{"order_no": r.order_no, "enterprise_id": r.enterprise_id,
                       "product": r.product, "amount_fen": r.amount_fen,
                       "quantity": r.quantity, "channel": r.channel, "status": r.status,
                       "payment_status": r.payment_status, "created_at": r.created_at} for r in rows])


def _sales_ready(channel: str) -> bool:
    gates_open = (
        BILLING_SALES_CODE_GATE_OPEN
        and settings.billing_sales_enabled
        and settings.billing_commercial_metering_ready
        and settings.billing_entitlement_enforcement_ready
    )
    if not gates_open:
        return False
    return gates_open and _gateway_configured(channel)


def _gateway_configured(channel: str) -> bool:
    """Channel configuration check for reconciliation; this never opens sales."""
    if channel == "alipay":
        return all((settings.alipay_app_id, settings.alipay_seller_id,
                    settings.alipay_notify_url.startswith("https://"),
                    Path(settings.alipay_private_key_path).is_file(), Path(settings.alipay_public_key_path).is_file()))
    if channel == "wechat":
        return all((settings.wechat_pay_app_id, settings.wechat_pay_mchid, settings.wechat_pay_merchant_serial,
                    settings.wechat_pay_public_key_serial, len(settings.wechat_pay_api_v3_key) == 32,
                    settings.wechat_pay_notify_url.startswith("https://"),
                    Path(settings.wechat_pay_merchant_private_key_path).is_file(),
                    Path(settings.wechat_pay_public_key_path).is_file()))
    return False


def _order_view(order: BillingOrder) -> dict:
    return {"order_no": order.order_no, "product": order.product, "amount_fen": order.amount_fen,
            "quantity": order.quantity, "channel": order.channel, "status": order.status,
            "enterprise_id": order.enterprise_id,
            "payment_status": order.payment_status, "payment_code_url": order.payment_code_url,
            "payment_expires_at": order.payment_expires_at, "created_at": order.created_at}


def _refund_view(refund: BillingRefund) -> dict:
    return {"order_no": refund.order_no, "refund_no": refund.refund_no, "amount_fen": refund.amount_fen,
            "reason": refund.reason, "status": refund.status, "review_note": refund.review_note,
            "reviewed_by": refund.reviewed_by, "provider_status": refund.provider_status,
            "attempt_count": refund.attempt_count,
            "requires_reconciliation": billing_checkout_service.refund_requires_reconciliation(refund),
            "created_at": refund.created_at, "updated_at": refund.updated_at,
            "completed_at": refund.completed_at}


def _provider_trade_query(order: BillingOrder) -> dict:
    from app.services import billing_payment_gateway as gateway

    if order.channel == "alipay":
        return gateway.query_alipay_trade(
            app_id=settings.alipay_app_id,
            private_key_pem=_read_key(settings.alipay_private_key_path, "支付宝商户私钥"),
            public_key_pem=_read_key(settings.alipay_public_key_path, "支付宝公钥"),
            order_no=order.order_no, amount_fen=order.amount_fen,
        )
    result = gateway.query_wechat_trade(
        mchid=settings.wechat_pay_mchid, merchant_serial=settings.wechat_pay_merchant_serial,
        merchant_private_key_pem=_read_key(settings.wechat_pay_merchant_private_key_path, "微信商户私钥"),
        platform_public_key_pem=_read_key(settings.wechat_pay_public_key_path, "微信支付公钥"),
        platform_serial=settings.wechat_pay_public_key_serial, order_no=order.order_no,
    )
    if result.get("state") == "SUCCESS" and (result.get("appid") != settings.wechat_pay_app_id
                                               or result.get("mchid") != settings.wechat_pay_mchid):
        raise gateway.PaymentGatewayError("微信查单响应商户身份不匹配")
    return result


def _provider_close_trade(order: BillingOrder) -> dict:
    from app.services import billing_payment_gateway as gateway

    if order.channel == "alipay":
        return gateway.close_alipay_trade(
            app_id=settings.alipay_app_id,
            private_key_pem=_read_key(settings.alipay_private_key_path, "支付宝商户私钥"),
            public_key_pem=_read_key(settings.alipay_public_key_path, "支付宝公钥"), order_no=order.order_no,
        )
    return gateway.close_wechat_trade(
        mchid=settings.wechat_pay_mchid, merchant_serial=settings.wechat_pay_merchant_serial,
        merchant_private_key_pem=_read_key(settings.wechat_pay_merchant_private_key_path, "微信商户私钥"),
        platform_public_key_pem=_read_key(settings.wechat_pay_public_key_path, "微信支付公钥"),
        platform_serial=settings.wechat_pay_public_key_serial, order_no=order.order_no,
    )


def _provider_refund(order: BillingOrder, refund: BillingRefund) -> dict:
    from app.services import billing_payment_gateway as gateway

    # The adapters identify the original payment by its merchant order number.
    if order.channel == "alipay":
        return gateway.create_alipay_refund(
            app_id=settings.alipay_app_id,
            private_key_pem=_read_key(settings.alipay_private_key_path, "支付宝商户私钥"),
            public_key_pem=_read_key(settings.alipay_public_key_path, "支付宝公钥"),
            order_no=order.order_no, refund_no=refund.refund_no, amount_fen=refund.amount_fen,
            reason=refund.reason,
        )
    return gateway.create_wechat_refund(
        mchid=settings.wechat_pay_mchid, merchant_serial=settings.wechat_pay_merchant_serial,
        merchant_private_key_pem=_read_key(settings.wechat_pay_merchant_private_key_path, "微信商户私钥"),
        platform_public_key_pem=_read_key(settings.wechat_pay_public_key_path, "微信支付公钥"),
        platform_serial=settings.wechat_pay_public_key_serial, order_no=order.order_no,
        refund_no=refund.refund_no, amount_fen=refund.amount_fen, total_fen=order.amount_fen,
        reason=refund.reason,
    )


def _provider_refund_query(order: BillingOrder, refund: BillingRefund) -> dict:
    from app.services import billing_payment_gateway as gateway

    if order.channel == "alipay":
        return gateway.query_alipay_refund(
            app_id=settings.alipay_app_id,
            private_key_pem=_read_key(settings.alipay_private_key_path, "支付宝商户私钥"),
            public_key_pem=_read_key(settings.alipay_public_key_path, "支付宝公钥"),
            order_no=order.order_no, refund_no=refund.refund_no,
            amount_fen=refund.amount_fen, total_fen=order.amount_fen,
        )
    return gateway.query_wechat_refund(
        mchid=settings.wechat_pay_mchid, merchant_serial=settings.wechat_pay_merchant_serial,
        merchant_private_key_pem=_read_key(settings.wechat_pay_merchant_private_key_path, "微信商户私钥"),
        platform_public_key_pem=_read_key(settings.wechat_pay_public_key_path, "微信支付公钥"),
        platform_serial=settings.wechat_pay_public_key_serial, order_no=order.order_no,
        refund_no=refund.refund_no, amount_fen=refund.amount_fen, total_fen=order.amount_fen,
    )


def _provider_order(order: BillingOrder) -> dict[str, str]:
    from app.services import billing_payment_gateway

    subject = {
        "reviewer_month": "个人审查员月度会员",
        "personal_credits": "个人积分充值",
        "enterprise_credits": "企业积分充值",
        "enterprise_create": "企业席位月度服务",
        "enterprise_renew": "企业席位月度续费",
    }.get(order.product)
    if not subject:
        raise billing_payment_gateway.PaymentGatewayError("订单商品无效")
    if order.channel == "alipay":
        return billing_payment_gateway.create_alipay_preorder(
            app_id=settings.alipay_app_id, seller_id=settings.alipay_seller_id,
            private_key_pem=_read_key(settings.alipay_private_key_path, "支付宝商户私钥"),
            public_key_pem=_read_key(settings.alipay_public_key_path, "支付宝公钥"),
            notify_url=settings.alipay_notify_url, order_no=order.order_no,
            amount_fen=order.amount_fen, subject=subject,
        )
    if order.channel == "wechat":
        return billing_payment_gateway.create_wechat_native_preorder(
            app_id=settings.wechat_pay_app_id, mchid=settings.wechat_pay_mchid,
            merchant_serial=settings.wechat_pay_merchant_serial,
            merchant_private_key_pem=_read_key(settings.wechat_pay_merchant_private_key_path, "微信商户私钥"),
            platform_public_key_pem=_read_key(settings.wechat_pay_public_key_path, "微信支付公钥"),
            platform_serial=settings.wechat_pay_public_key_serial,
            notify_url=settings.wechat_pay_notify_url, order_no=order.order_no,
            amount_fen=order.amount_fen, description=subject,
        )
    raise billing_payment_gateway.PaymentGatewayError("支付渠道无效")


@router.post("/orders")
def create_payment_order(payload: OrderRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not _sales_ready(payload.channel):
        raise HTTPException(status_code=503, detail="支付销售尚未通过计量、权益和商户配置门禁")
    if not payload.client_request_id:
        raise HTTPException(status_code=422, detail="缺少幂等请求编号")
    try:
        order = billing_checkout_service.create_order(
            db, user_id=user.id, product=payload.product, quantity=payload.quantity, channel=payload.channel,
            client_request_id=payload.client_request_id, enterprise_id=payload.enterprise_id,
            enterprise_name=payload.enterprise_name,
        )
        db.commit()
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if order.status == "paid" or order.payment_status == "ready":
        return Resp(data=_order_view(order))
    if order.payment_status != "not_started":
        raise HTTPException(status_code=409, detail="支付请求结果待查询；请勿重复创建新订单")

    order.payment_status = "creating"
    db.commit()
    try:
        provider = _provider_order(order)
    except Exception as exc:
        logger.warning("payment pre-order failed for order {}: {}", order.order_no, type(exc).__name__)
        db.query(BillingOrder).filter_by(id=order.id).update({"payment_status": "unknown"})
        db.commit()
        raise HTTPException(status_code=502, detail="支付渠道暂未确认订单结果，请稍后查询订单") from exc

    order = db.query(BillingOrder).filter_by(id=order.id).populate_existing().with_for_update().one()
    order.payment_code_url = provider["payment_code_url"]
    order.provider_payment_id = provider["provider_order_id"]
    order.payment_expires_at = order.created_at + timedelta(minutes=15)
    order.payment_status = "paid" if order.status == "paid" else "ready"
    db.commit()
    return Resp(data=_order_view(order))


@router.get("/orders/{order_no}")
def order_detail(order_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    order = _accessible_order(db, order_no, user)
    return Resp(data=_order_view(order))


def _accessible_order(db: Session, order_no: str, user: User) -> BillingOrder:
    order = db.query(BillingOrder).filter_by(order_no=order_no).first()
    if order is None:
        raise HTTPException(status_code=404, detail="支付订单不存在")
    if order.enterprise_id is not None:
        try:
            enterprise_org_service.require_enterprise_admin(db, order.enterprise_id, user.id)
        except ForbiddenError as exc:
            raise HTTPException(status_code=404, detail="支付订单不存在") from exc
    elif order.user_id != user.id:
            raise HTTPException(status_code=404, detail="支付订单不存在")
    return order


@router.post("/orders/{order_no}/refresh")
def refresh_order_status(order_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Query the provider; only a signed paid result can grant product benefits."""
    order = _accessible_order(db, order_no, user)
    if order.status in {"paid", "refunded", "cancelled"}:
        return Resp(data=_order_view(order))
    if not _gateway_configured(order.channel):
        raise HTTPException(status_code=503, detail="支付渠道查单配置尚未就绪")
    try:
        result = _provider_trade_query(order)
        if order.channel == "alipay":
            billing_checkout_service.apply_alipay_query(db, order_no=order_no, result=result)
        else:
            billing_checkout_service.apply_wechat_query(db, order_no=order_no, result=result)
        db.commit()
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="支付状态暂不能确认；订单权益未变更") from exc
    except Exception as exc:
        db.rollback()
        logger.warning("payment status query unknown for order {}: {}", order_no, type(exc).__name__)
        raise HTTPException(status_code=502, detail="支付状态暂不能确认；请稍后重试") from exc
    db.refresh(order)
    return Resp(data=_order_view(order))


@router.post("/orders/{order_no}/cancel")
def cancel_payment_order(order_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Close an unpaid provider transaction; query first to protect paid orders."""
    order = _accessible_order(db, order_no, user)
    if order.status in {"paid", "refunded", "cancelled"}:
        return Resp(data=_order_view(order))
    if not _gateway_configured(order.channel):
        raise HTTPException(status_code=503, detail="支付渠道关单配置尚未就绪")
    try:
        state = _provider_trade_query(order)
        if order.channel == "alipay":
            billing_checkout_service.apply_alipay_query(db, order_no=order_no, result=state)
        else:
            billing_checkout_service.apply_wechat_query(db, order_no=order_no, result=state)
        db.flush()
        if order.status == "paid":
            db.commit()
            return Resp(data=_order_view(order))
        if state.get("state") in {"NOT_FOUND", "TRADE_CLOSED", "CLOSED"}:
            billing_checkout_service.mark_order_closed(db, order_no=order_no)
        elif ((order.channel == "alipay" and state.get("state") == "WAIT_BUYER_PAY")
              or (order.channel == "wechat" and state.get("state") == "NOTPAY")):
            _provider_close_trade(order)
            billing_checkout_service.mark_order_closed(db, order_no=order_no)
        else:
            db.commit()
            raise HTTPException(status_code=409, detail="支付处理中或结果未知，暂不能关单")
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.warning("payment close unknown for order {}: {}", order_no, type(exc).__name__)
        # A close response may be lost after provider success. Refresh once and
        # keep the local order open unless a signed terminal state is confirmed.
        try:
            state = _provider_trade_query(order)
            if order.channel == "alipay":
                billing_checkout_service.apply_alipay_query(db, order_no=order_no, result=state)
            else:
                billing_checkout_service.apply_wechat_query(db, order_no=order_no, result=state)
            if order.status == "paid":
                db.commit()
                return Resp(data=_order_view(order))
            if state.get("state") in {"NOT_FOUND", "TRADE_CLOSED", "CLOSED"}:
                billing_checkout_service.mark_order_closed(db, order_no=order_no)
                db.commit()
                return Resp(data=_order_view(order))
            order.payment_status = "unknown"
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(status_code=502, detail="渠道关单结果待确认；请刷新订单状态") from exc
    db.refresh(order)
    return Resp(data=_order_view(order))


@router.post("/orders/{order_no}/refunds")
def request_order_refund(order_no: str, payload: RefundRequest, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    try:
        order = _accessible_order(db, order_no, user)
        refund = billing_checkout_service.request_refund(
            db, order_no=order_no, requester_id=user.id, reason=payload.reason,
            enterprise_admin_authorized=order.enterprise_id is not None,
        )
        db.commit()
        return Resp(data=_refund_view(refund))
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/orders/{order_no}/refunds")
def get_order_refund(order_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    order = _accessible_order(db, order_no, user)
    refund = db.query(BillingRefund).filter_by(order_no=order.order_no).first()
    return Resp(data=_refund_view(refund) if refund else None)


def _statement_batch_view(batch: BillingStatementBatch) -> dict:
    return {
        "id": batch.id,
        "channel": batch.channel,
        "source_origin": batch.source_origin,
        "bill_date": batch.bill_date.isoformat(),
        "bill_type": batch.bill_type,
        "file_sha256": batch.file_sha256,
        "provider_hash_type": batch.provider_hash_type,
        "provider_hash_value": batch.provider_hash_value,
        "file_name": batch.file_name,
        "file_size": batch.file_size,
        "parser_version": batch.parser_version,
        "row_count": batch.row_count,
        "payment_total_fen": batch.payment_total_fen,
        "refund_total_fen": batch.refund_total_fen,
        "received_total_fen": batch.received_total_fen,
        "received_missing_count": batch.received_missing_count,
        "fee_total_micros": batch.fee_total_micros,
        "fee_missing_count": batch.fee_missing_count,
        "imported_by": batch.imported_by,
        "imported_at": batch.imported_at,
    }


def _statement_row_view(row: BillingStatementRow) -> dict:
    return {
        "id": row.id,
        "source_line_number": row.source_line_number,
        "record_type": row.record_type,
        "order_no": row.order_no,
        "provider_trade_id": row.provider_trade_id,
        "refund_no": row.refund_no,
        "provider_refund_id": row.provider_refund_id,
        "provider_status": row.provider_status,
        "currency": row.currency,
        "amount_fen": row.amount_fen,
        "received_fen": row.received_fen,
        "fee_micros": row.fee_micros,
        "local_order_status": row.local_order_status,
        "local_amount_fen": row.local_amount_fen,
        "local_trade_id": row.local_trade_id,
        "local_refund_no": row.local_refund_no,
        "local_provider_refund_id": row.local_provider_refund_id,
        "local_refund_status": row.local_refund_status,
        "local_refund_amount_fen": row.local_refund_amount_fen,
        "discrepancy_code": row.discrepancy_code,
        "review_status": row.review_status,
        "last_reviewed_by": row.last_reviewed_by,
        "last_reviewed_at": row.last_reviewed_at,
        "last_review_note": row.last_review_note,
    }


def _reservation_reconciliation_event_view(event: BillingReservationReconciliationEvent) -> dict:
    return {
        "id": event.id,
        "reservation_id": event.reservation_id,
        "related_event_id": event.related_event_id,
        "actor_id": event.actor_id,
        "decision": event.decision,
        "resolution": event.resolution,
        "provider_request_id": event.provider_request_id,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "tool_successful_calls": event.tool_successful_calls,
        "actual_milli": event.actual_milli,
        "evidence_reference": event.evidence_reference,
        "evidence_sha256": event.evidence_sha256,
        "note": event.note,
        "created_at": event.created_at,
    }


def _reservation_reconciliation_view(db: Session, reservation: BillingReservation, *, held_milli: int,
                                     pending_event: BillingReservationReconciliationEvent | None = None) -> dict:
    if reservation.status not in {"reserved", "unknown"}:
        held_milli = 0
    provider_snapshot = all((reservation.provider_identity_version, reservation.provider_name,
                             reservation.provider_endpoint_host, reservation.credential_source))
    is_tool = reservation.billable_kind in {"search", "fetch"}
    model_rate_snapshot = all((reservation.pricing_version, reservation.input_rate_numerator,
                               reservation.input_rate_denominator, reservation.output_rate_numerator,
                               reservation.output_rate_denominator))
    tool_rate_snapshot = all((
        reservation.pricing_version, reservation.tool_name == reservation.billable_kind,
        reservation.tool_cost_cny_micros_per_success,
        reservation.tool_price_milli_per_success,
    ))
    rate_snapshot = tool_rate_snapshot if is_tool else model_rate_snapshot
    tool_usage_complete = reservation.server_tool_search_calls is not None \
        and reservation.server_tool_fetch_calls is not None
    tool_usage = (reservation.server_tool_search_calls or 0, reservation.server_tool_fetch_calls or 0)
    settlement_reasons = []
    release_reasons = []
    if not provider_snapshot:
        settlement_reasons.append("缺少供应商身份快照")
    if is_tool:
        if not tool_rate_snapshot:
            settlement_reasons.append("缺少工具单位成本、客户费率或目录版本快照")
        if reservation.provider_attempt_started_at is None:
            settlement_reasons.append("缺少已提交的 provider attempt 标记")
        if reservation.input_tokens is not None or reservation.output_tokens is not None:
            settlement_reasons.append("工具 reservation 混入模型 Token 用量")
        if reservation.server_tool_search_calls is not None or reservation.server_tool_fetch_calls is not None:
            settlement_reasons.append("工具 reservation 混入供应商内置 Search/Fetch 计数")
    else:
        if not model_rate_snapshot:
            settlement_reasons.append("缺少模型费率快照")
        if not tool_usage_complete:
            settlement_reasons.append("缺少供应商 Search/Fetch 用量计数，不能推断为零")
        elif any(tool_usage):
            settlement_reasons.append("存在未计价供应商内置 Search/Fetch 用量")
    if reservation.provider_attempt_started_at is not None:
        release_reasons.append("provider attempt 已开始，不能按从未发送释放")
    if reservation.input_tokens is not None or reservation.output_tokens is not None:
        release_reasons.append("已记录 Token 用量，与未发送释放提案冲突")
    if reservation.server_tool_search_calls is not None or reservation.server_tool_fetch_calls is not None:
        release_reasons.append("已记录供应商内置工具用量，与未发送释放提案冲突")
    if held_milli != reservation.requested_milli:
        settlement_reasons.append("当前预占明细与请求金额不一致")
        release_reasons.append("当前预占明细与请求金额不一致")
    if reservation.status == "reserved":
        settlement_reasons.append("reserved 状态可能仍在上游处理中")
        release_reasons.append("reserved 状态可能仍在上游处理中")
    elif reservation.status != "unknown":
        settlement_reasons.append("预占已结束，只能查看审计历史")
        release_reasons.append("预占已结束，只能查看审计历史")
    if pending_event:
        settlement_reasons.append("已有提案等待另一位最高管理员复核")
        release_reasons.append("已有提案等待另一位最高管理员复核")
    can_submit = reservation.status == "unknown" and held_milli == reservation.requested_milli and pending_event is None
    return {
        "id": reservation.id,
        "request_key_suffix": reservation.request_key[-12:],
        "model_name": reservation.model_name,
        "billable_kind": reservation.billable_kind,
        "tool_name": reservation.tool_name,
        "status": reservation.status,
        "usage_state": reservation.usage_state,
        "user_id": reservation.user_id,
        "enterprise_id": reservation.enterprise_id,
        "group_id": reservation.group_id,
        "usage_kind": reservation.usage_kind,
        "created_at": reservation.created_at,
        "requested_milli": reservation.requested_milli,
        "held_milli": held_milli,
        "actual_milli": reservation.actual_milli,
        "input_tokens": reservation.input_tokens,
        "output_tokens": reservation.output_tokens,
        "server_tool_search_calls": reservation.server_tool_search_calls,
        "server_tool_fetch_calls": reservation.server_tool_fetch_calls,
        "tool_cost_cny_micros_per_success": reservation.tool_cost_cny_micros_per_success,
        "tool_price_milli_per_success": reservation.tool_price_milli_per_success,
        "tool_successful_calls": reservation.tool_successful_calls,
        "provider_attempt_started_at": reservation.provider_attempt_started_at,
        "pricing_version": reservation.pricing_version,
        "provider_identity_version": reservation.provider_identity_version,
        "has_rate_snapshot": rate_snapshot,
        "has_tool_rate_snapshot": tool_rate_snapshot if is_tool else False,
        "has_provider_snapshot": provider_snapshot,
        "can_settle": can_submit and not settlement_reasons,
        "can_release_not_sent": can_submit and not release_reasons,
        "can_recover_stale_attempt": billing_reservation_reconciliation_service.can_recover_stale_tool_attempt(
            db, reservation,
        ),
        "settlement_block_reasons": settlement_reasons,
        "release_block_reasons": release_reasons,
        "pending_proposal": _reservation_reconciliation_event_view(pending_event) if pending_event else None,
    }


@router.get("/admin/reservations/reconciliation")
def list_reservation_reconciliation(
    status: Literal["reserved", "unknown", "reviewed"] | None = Query(None),
    billable_kind: Literal["model", "search", "fetch"] | None = Query(None),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    try:
        rows, total = billing_reservation_reconciliation_service.list_unresolved_reservations(
            db, status=status, billable_kind=billable_kind, offset=offset, limit=limit,
        )
        ids = [row.id for row in rows]
        parts = (db.query(BillingReservationPart).filter(BillingReservationPart.reservation_id.in_(ids)).all()
                 if ids else [])
        held = {}
        for part in parts:
            held[part.reservation_id] = held.get(part.reservation_id, 0) + int(part.reserved_milli)
        events = (db.query(BillingReservationReconciliationEvent)
                  .filter(BillingReservationReconciliationEvent.reservation_id.in_(ids))
                  .order_by(BillingReservationReconciliationEvent.id).all() if ids else [])
        latest = {}
        for event in events:
            if event.decision != "blocked":
                latest[event.reservation_id] = event
        pending = {key: event for key, event in latest.items() if event.decision == "proposed"}
        return Resp(data={
            "items": [_reservation_reconciliation_view(
                db, row, held_milli=held.get(row.id, 0), pending_event=pending.get(row.id),
            ) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except billing_reservation_reconciliation_service.ReconciliationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/reservations/{reservation_id}/reconciliation/events")
def list_reservation_reconciliation_events(
    reservation_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    try:
        rows = billing_reservation_reconciliation_service.list_reconciliation_events(
            db, reservation_id=reservation_id,
        )
        return Resp(data={"items": [_reservation_reconciliation_event_view(row) for row in rows]})
    except billing_reservation_reconciliation_service.ReconciliationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/admin/reservations/{reservation_id}/reconciliation/recover-stale")
def recover_stale_reservation_attempt(
    reservation_id: int,
    payload: ReservationStaleRecovery,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    try:
        reservation = billing_reservation_reconciliation_service.recover_stale_tool_attempt(
            db,
            reservation_id=reservation_id,
            actor_id=int(admin.id),
            actor_name=str(admin.username or f"admin-{admin.id}"),
            note=payload.note,
        )
        db.commit()
        parts = db.query(BillingReservationPart).filter_by(reservation_id=reservation.id).all()
        event = (
            db.query(BillingReservationReconciliationEvent)
            .filter_by(reservation_id=reservation.id)
            .order_by(BillingReservationReconciliationEvent.id.desc())
            .first()
        )
        return Resp(data=_reservation_reconciliation_view(
            db, reservation,
            held_milli=sum(int(part.reserved_milli) for part in parts),
            pending_event=event if event and event.decision == "proposed" else None,
        ))
    except billing_reservation_reconciliation_service.ReconciliationBlocked as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except billing_reservation_reconciliation_service.ReconciliationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/reservations/{reservation_id}/reconciliation/proposals")
def propose_reservation_reconciliation(
    reservation_id: int,
    payload: ReservationReconciliationProposal,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    try:
        event = billing_reservation_reconciliation_service.propose_reconciliation(
            db, reservation_id=reservation_id, actor_id=admin.id,
            resolution=payload.resolution, provider_request_id=payload.provider_request_id,
            input_tokens=payload.input_tokens, output_tokens=payload.output_tokens,
            tool_successful_calls=payload.tool_successful_calls,
            evidence_reference=payload.evidence_reference,
            evidence_sha256=payload.evidence_sha256, note=payload.note,
        )
        db.commit()
        return Resp(data=_reservation_reconciliation_event_view(event))
    except billing_reservation_reconciliation_service.ReconciliationBlocked as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except billing_reservation_reconciliation_service.ReconciliationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/reservations/{reservation_id}/reconciliation/decision")
def decide_reservation_reconciliation(
    reservation_id: int,
    payload: ReservationReconciliationDecision,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_user),
):
    try:
        billing_c4_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
        reservation, event = billing_reservation_reconciliation_service.decide_reconciliation(
            db, reservation_id=reservation_id, actor_id=admin.id,
            decision=payload.decision, note=payload.note,
        )
        if admin.role != "super_admin":
            result = {"id": reservation.id, "status": reservation.status, "actual_milli": reservation.actual_milli,
                      "tool_successful_calls": reservation.tool_successful_calls}
            result_event = _reservation_reconciliation_event_view(event)
            db.commit()
            return Resp(data={"reservation": result, "event": result_event})
        parts = db.query(BillingReservationPart).filter_by(reservation_id=reservation.id).all()
        db.commit()
        row = _reservation_reconciliation_view(
            db, reservation, held_milli=sum(int(part.reserved_milli) for part in parts),
        )
        return Resp(data={"reservation": row, "event": _reservation_reconciliation_event_view(event)})
    except billing_reservation_reconciliation_service.ReconciliationBlocked as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except billing_reservation_reconciliation_service.ReconciliationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/admin/reconciliation/reviewers/{user_id}")
def set_c4_reviewer(user_id: int, payload: C4ReviewerChange, db: Session = Depends(get_db),
                    admin: User = Depends(require_super_admin)):
    billing_c4_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
    row = billing_c4_reviewer_service.set_reviewer(
        db, actor_id=int(admin.id), user_id=user_id, active=payload.active, reason=payload.reason,
    )
    db.commit()
    return Resp(data={"user_id": row.user_id, "active": bool(row.active)})


@router.get("/admin/reconciliation/reviewers")
def list_c4_reviewers(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000),
                      db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    billing_c4_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
    query = db.query(BillingReconciliationReviewerGrant)
    total = query.count()
    rows = query.order_by(BillingReconciliationReviewerGrant.id.desc()).offset(offset).limit(limit).all()
    eligible = billing_c4_reviewer_service.reviewer_eligibility(db, [int(row.user_id) for row in rows])
    users = {int(user.id): user for user in db.query(User.id, User.username, User.nickname).filter(
        User.id.in_([row.user_id for row in rows]),
    ).all()} if rows else {}
    return Resp(data={"items": [{"user_id": row.user_id, "active": bool(row.active),
                                  "eligible": eligible.get(int(row.user_id), False),
                                  "username": users[int(row.user_id)].username if int(row.user_id) in users else None,
                                  "nickname": users[int(row.user_id)].nickname if int(row.user_id) in users else None,
                                  "granted_at": row.granted_at, "revoked_at": row.revoked_at}
                                 for row in rows], "total": total, "limit": limit, "offset": offset})


def _pending_c4_reviews(db: Session, *, reservation_id: int | None = None, limit: int = 50, offset: int = 0):
    event = BillingReservationReconciliationEvent
    latest = (db.query(event.reservation_id.label("reservation_id"), func.max(event.id).label("event_id"))
              .filter(event.decision != "blocked").group_by(event.reservation_id).subquery())
    query = (db.query(BillingReservation, event).join(latest, latest.c.reservation_id == BillingReservation.id)
             .join(event, event.id == latest.c.event_id)
             .filter(BillingReservation.status == "unknown", event.decision == "proposed"))
    if reservation_id is not None:
        query = query.filter(BillingReservation.id == reservation_id)
    rows = query.order_by(event.id.desc()).offset(offset).limit(limit).all()
    return [{
        "reservation": {"id": reservation.id, "status": reservation.status,
                        "billable_kind": reservation.billable_kind, "model_name": reservation.model_name,
                        "requested_milli": reservation.requested_milli,
                        "pricing_version": reservation.pricing_version},
        "proposal": _reservation_reconciliation_event_view(proposal),
    } for reservation, proposal in rows]


@router.get("/reconciliation/reviews")
def list_c4_reviews(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000),
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_c4_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    return Resp(data={"items": _pending_c4_reviews(db, limit=limit, offset=offset)})


@router.get("/reconciliation/reviews/{reservation_id}")
def get_c4_review(reservation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    billing_c4_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    rows = _pending_c4_reviews(db, reservation_id=reservation_id, limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="待复核提案不存在")
    return Resp(data=rows[0])


@router.post("/admin/statements/import")
async def import_payment_statement(
    channel: Literal["alipay", "wechat"] = Form(...),
    bill_date: str = Form(..., min_length=10, max_length=10),
    bill_type: str = Form(..., min_length=1, max_length=16),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """Import a provider bill for review; source bytes are discarded after normalization."""
    raw = await file.read(MAX_STATEMENT_FILE_BYTES + 1)
    if len(raw) > MAX_STATEMENT_FILE_BYTES:
        raise HTTPException(status_code=413, detail="账单文件超过 12 MiB 限制")
    try:
        batch = import_statement(
            db, actor_id=admin.id, channel=channel, bill_date=bill_date, bill_type=bill_type,
            filename=file.filename, raw=raw,
        )
        db.commit()
        return Resp(data=_statement_batch_view(batch))
    except StatementImportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        await file.close()


@router.post("/admin/statements/download")
def download_payment_statement(
    payload: StatementFetch,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """Fetch a signed provider transaction statement and import it for read-only review."""
    from app.services import billing_statement_gateway
    bill_date = payload.bill_date.isoformat()

    try:
        if payload.channel == "alipay":
            if payload.bill_type != "trade":
                raise StatementImportError("支付宝仅支持 trade 账单")
            if not settings.alipay_app_id:
                raise PaymentVerificationError("支付宝商户应用尚未配置")
            downloaded = billing_statement_gateway.download_alipay_trade_statement(
                app_id=settings.alipay_app_id,
                private_key_pem=_read_key(settings.alipay_private_key_path, "支付宝商户私钥"),
                public_key_pem=_read_key(settings.alipay_public_key_path, "支付宝公钥"),
                bill_date=bill_date,
            )
        else:
            if payload.bill_type not in {"ALL", "SUCCESS", "REFUND"}:
                raise StatementImportError("微信支付账单类型无效")
            if not all((settings.wechat_pay_mchid, settings.wechat_pay_merchant_serial,
                        settings.wechat_pay_public_key_serial)):
                raise PaymentVerificationError("微信支付商户身份尚未配置")
            downloaded = billing_statement_gateway.download_wechat_trade_statement(
                mchid=settings.wechat_pay_mchid,
                merchant_serial=settings.wechat_pay_merchant_serial,
                merchant_private_key_pem=_read_key(settings.wechat_pay_merchant_private_key_path, "微信商户私钥"),
                platform_public_key_pem=_read_key(settings.wechat_pay_public_key_path, "微信支付公钥"),
                platform_serial=settings.wechat_pay_public_key_serial,
                bill_date=bill_date,
                bill_type=payload.bill_type,
            )
        batch = import_statement(
            db, actor_id=admin.id, channel=payload.channel, bill_date=bill_date,
            bill_type=payload.bill_type, filename=downloaded.file_name, raw=downloaded.raw,
            source_origin=downloaded.source_origin, provider_hash_type=downloaded.provider_hash_type,
            provider_hash_value=downloaded.provider_hash_value,
        )
        db.commit()
        return Resp(data=_statement_batch_view(batch))
    except PaymentVerificationError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except billing_statement_gateway.StatementGatewayInputError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except billing_statement_gateway.StatementGatewayError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except StatementImportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("支付渠道账单下载或导入失败")
        db.rollback()
        raise


@router.get("/admin/statements")
def list_payment_statements(
    channel: Literal["alipay", "wechat"] | None = Query(None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=MAX_STATEMENT_OFFSET),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    query = db.query(BillingStatementBatch)
    if channel:
        query = query.filter(BillingStatementBatch.channel == channel)
    total = query.count()
    rows = query.order_by(BillingStatementBatch.bill_date.desc(), BillingStatementBatch.id.desc()) \
        .offset(offset).limit(limit).all()
    return Resp(data={"items": [_statement_batch_view(row) for row in rows], "total": total,
                      "limit": limit, "offset": offset})


@router.get("/admin/statements/rows/{row_id}/reviews")
def list_payment_statement_row_reviews(
    row_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    if db.get(BillingStatementRow, row_id) is None:
        raise HTTPException(status_code=404, detail="渠道账单明细不存在")
    rows = db.query(BillingStatementReviewEvent).filter_by(statement_row_id=row_id) \
        .order_by(BillingStatementReviewEvent.id).all()
    return Resp(data={"items": [{"id": row.id, "actor_id": row.actor_id, "decision": row.decision,
                                  "note": row.note, "created_at": row.created_at} for row in rows]})


@router.get("/admin/statements/{batch_id}")
def get_payment_statement(
    batch_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    batch = db.get(BillingStatementBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="渠道账单批次不存在")
    return Resp(data=_statement_batch_view(batch))


@router.get("/admin/statements/{batch_id}/rows")
def list_payment_statement_rows(
    batch_id: int,
    discrepancy_code: str | None = Query(None, max_length=64),
    review_status: str | None = Query(None, max_length=24),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=MAX_STATEMENT_OFFSET),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_super_admin),
):
    if db.get(BillingStatementBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="渠道账单批次不存在")
    query = db.query(BillingStatementRow).filter(BillingStatementRow.batch_id == batch_id)
    if discrepancy_code:
        query = query.filter(BillingStatementRow.discrepancy_code == discrepancy_code)
    if review_status:
        query = query.filter(BillingStatementRow.review_status == review_status)
    total = query.count()
    rows = query.order_by(BillingStatementRow.source_line_number, BillingStatementRow.event_index,
                          BillingStatementRow.id).offset(offset).limit(limit).all()
    return Resp(data={"items": [_statement_row_view(row) for row in rows], "total": total,
                      "limit": limit, "offset": offset})


@router.post("/admin/statements/{batch_id}/rows/{row_id}/review")
def review_payment_statement_row(
    batch_id: int,
    row_id: int,
    payload: StatementReview,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = db.query(BillingStatementRow).filter_by(id=row_id, batch_id=batch_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="渠道账单明细不存在")
    try:
        row = review_statement_row(db, actor_id=admin.id, row_id=row.id,
                                   decision=payload.decision, note=payload.note)
        db.commit()
        return Resp(data=_statement_row_view(row))
    except StatementImportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/refunds")
def list_refunds(status: str | None = Query(None, max_length=24), limit: int = Query(default=100, ge=1, le=500),
                 before_id: int | None = Query(None, gt=0, le=2**63 - 1),
                 db: Session = Depends(get_db), _admin: User = Depends(require_super_admin)):
    query = db.query(BillingRefund)
    if status:
        query = query.filter_by(status=status)
    if before_id is not None:
        query = query.filter(BillingRefund.id < before_id)
    rows = query.order_by(BillingRefund.id.desc()).limit(limit).all()
    return Resp(data=[{**_refund_view(row), "id": row.id} for row in rows])


@router.put("/admin/refund-reviewers/{user_id}")
def set_refund_reviewer(user_id: int, payload: C4ReviewerChange, db: Session = Depends(get_db),
                        admin: User = Depends(require_super_admin)):
    billing_c4_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
    row = billing_refund_reviewer_service.set_reviewer(
        db, actor_id=int(admin.id), user_id=user_id, active=payload.active, reason=payload.reason,
    )
    db.commit()
    return Resp(data={"user_id": row.user_id, "active": bool(row.active)})


@router.get("/admin/refund-reviewers")
def list_refund_reviewers(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000),
                          db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    billing_c4_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
    query = db.query(BillingRefundReviewerGrant)
    total = query.count()
    rows = query.order_by(BillingRefundReviewerGrant.id.desc()).offset(offset).limit(limit).all()
    eligible = billing_c4_reviewer_service.reviewer_eligibility(db, [int(row.user_id) for row in rows])
    users = {int(user.id): user for user in db.query(User.id, User.username, User.nickname).filter(
        User.id.in_([row.user_id for row in rows]),
    ).all()} if rows else {}
    return Resp(data={"items": [{"user_id": row.user_id, "active": bool(row.active),
                                  "eligible": eligible.get(int(row.user_id), False),
                                  "username": users[int(row.user_id)].username if int(row.user_id) in users else None,
                                  "nickname": users[int(row.user_id)].nickname if int(row.user_id) in users else None,
                                  "granted_at": row.granted_at, "revoked_at": row.revoked_at}
                                 for row in rows], "total": total, "limit": limit, "offset": offset})


@router.get("/refund-reviews")
def list_refund_reviews(limit: int = Query(50, ge=1, le=100), before_id: int | None = Query(None, gt=0),
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.services.billing_refund_policy import requires_reconciliation_expression

    billing_refund_reviewer_service.require_reviewer(db, int(user.id), int(user.token_version or 0))
    old_owner = db.query(User.id).filter_by(username="admin", role="super_admin")
    query = (db.query(BillingRefund, BillingOrder.product)
             .join(BillingOrder, BillingOrder.order_no == BillingRefund.order_no)
             .filter(or_(BillingRefund.status == "manual_review",
                         and_(BillingRefund.status == "approved", BillingRefund.reviewed_by.in_(old_owner),
                              BillingRefund.provider_status.is_(None), BillingRefund.attempt_count == 0)),
                     or_(BillingRefund.provider_status.is_(None),
                         ~BillingRefund.provider_status.in_(("SUCCESS", "PROCESSING", "UNKNOWN", "NOT_FOUND"))),
                     ~requires_reconciliation_expression()))
    if before_id is not None:
        query = query.filter(BillingRefund.id < before_id)
    rows = query.order_by(BillingRefund.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    return Resp(data={"items": [{**_refund_view(refund), "product": product} for refund, product in page],
                      "next_before_id": page[-1][0].id if has_more else None})


@router.post("/admin/refunds/{refund_no}/decision")
def admin_decide_refund(refund_no: str, payload: RefundDecision, db: Session = Depends(get_db),
                        admin: User = Depends(get_current_user)):
    billing_refund_reviewer_service.require_reviewer(db, int(admin.id), int(admin.token_version or 0))
    try:
        refund = billing_checkout_service.review_manual_refund(
            db, refund_no=refund_no, admin_id=admin.id, decision=payload.decision, reason=payload.reason,
        )
        db.commit()
        return Resp(data=_refund_view(refund))
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/refunds/{refund_no}/execute")
def execute_refund(refund_no: str, db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    try:
        refund, order = billing_checkout_service.prepare_refund(db, refund_no=refund_no, admin_id=admin.id)
        attempt_no = refund.attempt_count
        db.commit()  # Persist the quota lock before calling a remote payment provider.
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not _gateway_configured(order.channel):
        try:
            billing_checkout_service.update_refund_result(db, refund_no=refund_no, result={"state": "UNKNOWN"},
                                                           attempt_no=attempt_no)
            db.commit()
        except billing_ledger_service.RetryableWalletSnapshot:
            # The prepared state was already committed; preserve its hold and
            # surface the configuration failure without claiming a response.
            db.rollback()
        raise HTTPException(status_code=503, detail="支付渠道退款配置尚未就绪；退款额度保持冻结")
    try:
        result = _provider_refund(order, refund)
        create_response_verified = True
    except Exception as exc:
        logger.warning("payment refund outcome unknown for {}: {}", refund_no, type(exc).__name__)
        result = {"state": "UNKNOWN"}
        create_response_verified = False
    try:
        refund = billing_checkout_service.update_refund_result(db, refund_no=refund_no, result=result,
                                                                attempt_no=attempt_no,
                                                                create_response_verified=create_response_verified)
        db.commit()
    except billing_ledger_service.RetryableWalletSnapshot as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="退款已提交渠道，结果请通过查单确认；积分保持冻结") from exc
    return Resp(data=_refund_view(refund))


@router.post("/admin/refunds/{refund_no}/reconcile")
def reconcile_refund(refund_no: str, db: Session = Depends(get_db), _admin: User = Depends(require_super_admin)):
    refund = db.query(BillingRefund).filter_by(refund_no=refund_no).first()
    if refund is None:
        raise HTTPException(status_code=404, detail="退款单不存在")
    retry_local_finalization = refund.status == "manual_review" and refund.provider_status == "SUCCESS"
    retry_manual_provider_query = (
        refund.status == "manual_review" and refund.provider_status in {"PROCESSING", "UNKNOWN", "NOT_FOUND"}
    )
    retry_legacy_not_found = billing_checkout_service.refund_requires_reconciliation(refund)
    if (refund.status not in {"processing", "unknown"}
            and not retry_local_finalization and not retry_manual_provider_query and not retry_legacy_not_found):
        raise HTTPException(
            status_code=409,
            detail="只有处理中、结果未知或渠道状态在途/已成功且本地账本待复核的退款可以查单",
        )
    order = db.query(BillingOrder).filter_by(order_no=refund.order_no).one()
    if not _gateway_configured(order.channel):
        raise HTTPException(status_code=503, detail="支付渠道查单配置尚未就绪")
    claim_token = billing_checkout_service.claim_refund_reconciliation(
        db, refund_no=refund_no,
    )
    if claim_token is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="该退款刚提交渠道请求或已有管理员正在查单，请稍后重试")
    # Publish the atomic claim before external I/O. A second worker's conditional
    # UPDATE then sees the fresh cooldown timestamp instead of issuing another query.
    db.commit()
    try:
        result = _provider_refund_query(order, refund)
    except Exception as exc:
        logger.warning("refund reconciliation unknown for {}: {}", refund_no, type(exc).__name__)
        billing_checkout_service.release_refund_reconciliation(db, refund_no=refund_no, claim_token=claim_token)
        db.commit()
        raise HTTPException(status_code=502, detail="退款查单未获得有效渠道响应，状态保持待核对") from exc
    if retry_local_finalization and (not isinstance(result, dict) or result.get("state") != "SUCCESS"):
        billing_checkout_service.release_refund_reconciliation(
            db, refund_no=refund_no, claim_token=claim_token,
        )
        db.commit()
        raise HTTPException(status_code=409, detail="渠道未再次确认退款成功；本地退款状态保持冻结并待人工复核")
    try:
        refund = billing_checkout_service.update_refund_result(
            db, refund_no=refund_no, result=result, verified_provider_query=True,
            reconciliation_token=claim_token,
        )
    except billing_ledger_service.BillingError as exc:
        db.rollback()
        billing_checkout_service.release_refund_reconciliation(
            db, refund_no=refund_no, claim_token=claim_token,
        )
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return Resp(data=_refund_view(refund))


MAX_PAYMENT_CALLBACK_BYTES = 1024 * 1024


async def _bounded_body(request: Request) -> bytes:
    """Read callback bodies with a hard cap before passing raw bytes to signature checks."""
    header = request.headers.get("content-length")
    if header is not None:
        try:
            if int(header) < 0 or int(header) > MAX_PAYMENT_CALLBACK_BYTES:
                raise ValueError("支付通知正文过大")
        except ValueError as exc:
            raise ValueError("支付通知长度无效") from exc
    chunks = bytearray()
    async for part in request.stream():
        if len(chunks) + len(part) > MAX_PAYMENT_CALLBACK_BYTES:
            raise ValueError("支付通知正文过大")
        chunks.extend(part)
    return bytes(chunks)


def _read_key(path: str, label: str) -> bytes:
    if not isinstance(path, str) or not path.strip():
        raise PaymentVerificationError(f"{label}尚未配置")
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise PaymentVerificationError(f"{label}不可读取") from exc
    if not data:
        raise PaymentVerificationError(f"{label}为空")
    return data


@router.post("/payments/alipay/notify", include_in_schema=False)
async def alipay_payment_notification(request: Request, db: Session = Depends(get_db)):
    """支付宝 RSA2 通知；仅已匹配本地订单并完成幂等入账后应答 success。"""
    try:
        body = await _bounded_body(request)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise ValueError("支付宝通知格式无效")
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=50)
        params = {}
        for key, value in pairs:
            if key in params:
                raise ValueError("支付宝通知包含重复字段")
            params[key] = value
        order_no = params.get("out_trade_no", "")
        if not order_no or not settings.alipay_app_id or not settings.alipay_seller_id:
            raise PaymentVerificationError("支付宝通知配置或订单号缺失")
        billing_checkout_service.apply_alipay(
            db, order_no=order_no, params=params,
            public_key_pem=_read_key(settings.alipay_public_key_path, "支付宝公钥"),
            expected_appid=settings.alipay_app_id, expected_seller=settings.alipay_seller_id,
        )
        db.commit()
        return PlainTextResponse("success")
    except (ValueError, UnicodeError, PaymentVerificationError, billing_ledger_service.BillingError):
        db.rollback()
        return PlainTextResponse("failure", status_code=400)
    except Exception:
        logger.exception("支付宝支付通知处理失败")
        db.rollback()
        return PlainTextResponse("failure", status_code=500)


@router.post("/payments/wechat/notify", include_in_schema=False)
async def wechat_payment_notification(request: Request, db: Session = Depends(get_db)):
    """微信支付 API v3 通知；失败返回 5xx 允许渠道重试。"""
    try:
        body = await _bounded_body(request)
        if not settings.wechat_pay_app_id or not settings.wechat_pay_mchid:
            raise PaymentVerificationError("微信支付通知配置缺失")
        public_key = _read_key(settings.wechat_pay_public_key_path, "微信支付公钥")
        api_v3_key = settings.wechat_pay_api_v3_key.encode("utf-8")
        order_no = extract_wechat_order_no(
            headers=request.headers, body=body, public_key_pem=public_key, api_v3_key=api_v3_key,
            expected_serial=settings.wechat_pay_public_key_serial,
        )
        billing_checkout_service.apply_wechat(
            db, order_no=order_no, headers=request.headers, body=body,
            public_key_pem=public_key, api_v3_key=api_v3_key,
            expected_serial=settings.wechat_pay_public_key_serial,
            expected_appid=settings.wechat_pay_app_id, expected_mchid=settings.wechat_pay_mchid,
        )
        db.commit()
        return JSONResponse({"code": "SUCCESS", "message": "成功"})
    except (ValueError, UnicodeError, PaymentVerificationError, billing_ledger_service.BillingError):
        db.rollback()
        return JSONResponse({"code": "FAIL", "message": "通知验证失败"}, status_code=400)
    except Exception:
        logger.exception("微信支付通知处理失败")
        db.rollback()
        return JSONResponse({"code": "FAIL", "message": "暂时无法处理"}, status_code=500)
