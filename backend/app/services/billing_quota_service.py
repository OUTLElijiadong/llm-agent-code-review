"""企业、团队和成员月额度；所有额度按毫积分保存并由账本预占。"""

import re
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, ValidationError
from app.models.billing_ledger import (
    BillingCreditBatch,
    BillingLimit,
    BillingReservation,
    BillingReservationLimit,
    BillingReservationPart,
    BillingWallet,
)
from app.services import billing_ledger_service as ledger
from app.services import enterprise_org_service as org

SHANGHAI = ZoneInfo("Asia/Shanghai")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_LIMIT_CREDITS = 100_000_000


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise ValidationError("额度周期必须为 YYYY-MM")
    year, month = (int(value) for value in period.split("-"))
    if year < 1 or (year == 9999 and month == 12):
        raise ValidationError("额度周期超出可用日期范围")
    start_local = datetime(year, month, 1, tzinfo=SHANGHAI)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=SHANGHAI)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=SHANGHAI)
    return (start_local.astimezone(timezone.utc).replace(tzinfo=None),
            end_local.astimezone(timezone.utc).replace(tzinfo=None))


def current_period() -> str:
    now = datetime.now(timezone.utc).astimezone(SHANGHAI)
    return f"{now.year:04d}-{now.month:02d}"


def _limit_key(enterprise_id: int, period: str, group_id: int | None, member_id: int | None) -> str:
    return f"normal:{enterprise_id}:{period}:{group_id or 0}:{member_id or 0}"


def _as_dict(row: BillingLimit) -> dict:
    if row.group_id is None and row.user_id is None:
        scope = "enterprise"
    elif row.user_id is None:
        scope = "group"
    else:
        scope = "member"
    return {"scope": scope, "group_id": row.group_id, "member_id": row.user_id,
            "period_start": row.period_start, "period_end": row.period_end,
            "limit_milli": row.limit_milli, "used_milli": row.used_milli,
            "reserved_milli": row.reserved_milli,
            "remaining_milli": max(0, row.limit_milli - row.used_milli - row.reserved_milli)}


def _lock_enterprise_wallet(db: Session, enterprise_id: int) -> tuple[BillingWallet, int | None, bool]:
    """Serialize limit changes with all enterprise reservation and settlement paths."""
    wallet = db.query(BillingWallet).filter_by(owner_type="enterprise", owner_id=enterprise_id).first()
    created = False
    if wallet is None:
        try:
            with db.begin_nested():
                wallet = BillingWallet(owner_type="enterprise", owner_id=enterprise_id)
                db.add(wallet)
                db.flush()
                created = True
        except IntegrityError:
            # A concurrent creator may have committed after this RR snapshot.
            wallet = (db.query(BillingWallet).filter_by(owner_type="enterprise", owner_id=enterprise_id)
                      .populate_existing().with_for_update().one_or_none())
            if wallet is None:
                raise
    snapshot_version = ledger._wallet_snapshot_version(db, int(wallet.id)) if not created else None
    locked = ledger._lock_wallets(db, [int(wallet.id)])[int(wallet.id)]
    return locked, snapshot_version, created


def _reconcile_limit(
    db: Session, row: BillingLimit, wallet_id: int, *, attach: bool = True,
) -> tuple[int, int]:
    """Count the matching month's enterprise-funded charges and attach live holds.

    The caller holds the enterprise wallet lock first. For writes it must also
    prove the wallet has not changed since its RR snapshot was established;
    otherwise the request fails closed and retries in a new transaction.
    """
    query = db.query(BillingReservation).filter(
        BillingReservation.enterprise_id == row.enterprise_id,
        BillingReservation.usage_kind == "member",
        BillingReservation.created_at >= row.period_start,
        BillingReservation.created_at < row.period_end,
    )
    if row.group_id is not None:
        query = query.filter(BillingReservation.group_id == row.group_id)
    if row.user_id is not None:
        query = query.filter(BillingReservation.user_id == row.user_id)
    reservations = query.order_by(BillingReservation.id).populate_existing().all()
    # Fetch related rows in bounded batches instead of issuing several queries
    # per reservation. The same RR snapshot is used for every batch.
    parts_by_reservation: dict[int, list[BillingReservationPart]] = defaultdict(list)
    links_by_reservation: dict[int, BillingReservationLimit] = {}
    ids = [int(reservation.id) for reservation in reservations]
    for offset in range(0, len(ids), 500):
        batch_ids = ids[offset:offset + 500]
        parts = (db.query(BillingReservationPart).filter(BillingReservationPart.reservation_id.in_(batch_ids))
                 .order_by(BillingReservationPart.id).populate_existing().all())
        for part in parts:
            parts_by_reservation[int(part.reservation_id)].append(part)
        links = (db.query(BillingReservationLimit)
                 .filter(BillingReservationLimit.limit_id == row.id,
                         BillingReservationLimit.reservation_id.in_(batch_ids)).populate_existing().all())
        links_by_reservation.update({int(link.reservation_id): link for link in links})
    batch_ids = sorted({int(part.batch_id) for parts in parts_by_reservation.values() for part in parts})
    batch_wallets: dict[int, int] = {}
    for offset in range(0, len(batch_ids), 500):
        rows = (db.query(BillingCreditBatch.id, BillingCreditBatch.wallet_id)
                .filter(BillingCreditBatch.id.in_(batch_ids[offset:offset + 500])).all())
        batch_wallets.update({int(batch.id): int(batch.wallet_id) for batch in rows})
    used = held = 0
    for reservation in reservations:
        if reservation.status not in {"reserved", "unknown", "settled", "released"}:
            raise ConflictError("已有账单状态无法核验，不能设置额度")
        if reservation.enterprise_wallet_id != wallet_id:
            raise ConflictError("已有账单的企业钱包归属无法核验，不能设置额度")
        parts = parts_by_reservation[int(reservation.id)]
        if (not parts or any(part.reserved_milli <= 0 for part in parts)
                or sum(int(part.reserved_milli) for part in parts) != reservation.requested_milli
                or any(part.wallet_id not in {wallet_id, reservation.personal_wallet_id} for part in parts)):
            raise ConflictError("已有账单预占明细无法核验，不能设置额度")
        if any(batch_wallets.get(int(part.batch_id)) != part.wallet_id for part in parts):
            raise ConflictError("已有账单批次归属无法核验，不能设置额度")
        if reservation.status == "released":
            not_sent = (reservation.usage_state == "not_sent" and reservation.actual_milli is None
                        and reservation.provider_attempt_started_at is None
                        and all(part.charged_milli is None for part in parts))
            nonbillable = (reservation.usage_state == "not_billable" and reservation.actual_milli == 0
                           and all(part.charged_milli == 0 for part in parts))
            if not (not_sent or nonbillable):
                raise ConflictError("已有账单释放证据无法核验，不能设置额度")
            continue
        company_parts = [part for part in parts if part.wallet_id == wallet_id]
        company_reserved = sum(int(part.reserved_milli) for part in company_parts)
        if reservation.status == "settled":
            if (reservation.actual_milli is None or any(part.charged_milli is None for part in parts)
                    or any(not 0 <= part.charged_milli <= part.reserved_milli for part in parts)
                    or sum(int(part.charged_milli) for part in parts) != reservation.actual_milli):
                raise ConflictError("已有账单结算明细无法核验，不能设置额度")
            used += sum(int(part.charged_milli) for part in company_parts)
        else:
            if any(part.charged_milli is not None for part in parts):
                raise ConflictError("已有账单预占明细无法核验，不能设置额度")
            link = links_by_reservation.get(int(reservation.id))
            if link is None:
                if company_reserved and attach:
                    db.add(BillingReservationLimit(reservation_id=reservation.id, limit_id=row.id,
                                                   reserved_milli=company_reserved))
            elif link.reserved_milli != company_reserved:
                raise ConflictError("已有额度预占明细无法核验，不能设置额度")
            held += company_reserved
    if used < row.used_milli or held < row.reserved_milli:
        raise ConflictError("已有额度记录超过可核验的账单明细，不能自动重建")
    return used, held


def list_limits(
    db: Session, enterprise_id: int, actor_id: int, period: str, *, group_id: int | None = None,
) -> list[dict]:
    start, end = _period_bounds(period)
    org._company(db, enterprise_id)
    member = org.require_enterprise_member(db, enterprise_id, actor_id)
    query = db.query(BillingLimit).filter(
        BillingLimit.enterprise_id == enterprise_id, BillingLimit.category == "normal",
        BillingLimit.period_start == start, BillingLimit.period_end == end,
    )
    if member.role in {"owner", "admin"}:
        if group_id is not None:
            org.require_group_member(db, enterprise_id, group_id, actor_id, allow_admin=True)
            query = query.filter(BillingLimit.group_id == group_id)
    else:
        if group_id is None:
            raise ForbiddenError("小组长只能查看自己负责小组的额度")
        org.require_group_manager(db, enterprise_id, group_id, actor_id)
        query = query.filter(BillingLimit.group_id == group_id)
    rows = query.order_by(BillingLimit.group_id, BillingLimit.user_id, BillingLimit.id).all()
    if any(row.accounting_version not in {0, 1} for row in rows):
        raise ConflictError("额度账本版本无法识别，不能展示未核验数值")
    legacy_rows = [row for row in rows if row.accounting_version == 0]
    if not legacy_rows:
        return [_as_dict(row) for row in rows]
    wallet = db.query(BillingWallet).filter_by(owner_type="enterprise", owner_id=enterprise_id).first()
    output = []
    for row in rows:
        view = _as_dict(row)
        if row.accounting_version == 0:
            if wallet is None:
                history = db.query(BillingReservation.id).filter(
                    BillingReservation.enterprise_id == enterprise_id,
                    BillingReservation.usage_kind == "member",
                    BillingReservation.created_at >= row.period_start,
                    BillingReservation.created_at < row.period_end,
                )
                if row.group_id is not None:
                    history = history.filter(BillingReservation.group_id == row.group_id)
                if row.user_id is not None:
                    history = history.filter(BillingReservation.user_id == row.user_id)
                if row.used_milli or row.reserved_milli or history.first() is not None:
                    raise ConflictError("历史额度缺少企业钱包，不能展示未核验数值")
                used = held = 0
            else:
                used, held = _reconcile_limit(db, row, int(wallet.id), attach=False)
            view.update(used_milli=used, reserved_milli=held,
                        remaining_milli=max(0, row.limit_milli - used - held))
        output.append(view)
    return output


def set_limit(db: Session, enterprise_id: int, actor_id: int, *, period: str, scope: str,
              limit_credits: int, group_id: int | None = None, member_id: int | None = None) -> dict:
    if scope not in {"enterprise", "group", "member"}:
        raise ValidationError("额度范围无效")
    if type(limit_credits) is not int or not 0 <= limit_credits <= MAX_LIMIT_CREDITS:
        raise ValidationError("额度必须是 0 至 100000000 的整数积分")
    start, end = _period_bounds(period)
    current_start, _ = _period_bounds(current_period())
    if start < current_start:
        raise ValidationError("不能修改已开始月份之前的额度")
    org._company(db, enterprise_id, lock=True)
    actor = org.require_enterprise_member(db, enterprise_id, actor_id, lock=True)
    if scope == "enterprise":
        if group_id is not None or member_id is not None or actor.role not in {"owner", "admin"}:
            raise ForbiddenError("只有企业管理员可以设置企业额度")
    elif scope == "group":
        if group_id is None or member_id is not None:
            raise ValidationError("小组额度必须指定小组且不能指定成员")
        org.require_group_manager(db, enterprise_id, group_id, actor_id, lock=True)
    else:
        if member_id is None:
            raise ValidationError("成员额度必须指定成员")
        if group_id is None:
            if actor.role not in {"owner", "admin"}:
                raise ForbiddenError("小组长只能管理本组成员额度")
            org.require_enterprise_member(db, enterprise_id, member_id, lock=True)
        else:
            org.require_group_manager(db, enterprise_id, group_id, actor_id, lock=True)
            target = org.require_group_member(db, enterprise_id, group_id, member_id, allow_admin=False, lock=True)
            if actor.role == "member" and target.role == "leader":
                raise ForbiddenError("小组长不能修改其他组长的额度")

    wallet, snapshot_version, created = _lock_enterprise_wallet(db, enterprise_id)
    if not created:
        ledger._require_wallet_snapshot(snapshot_version, wallet)
    key = _limit_key(enterprise_id, period, group_id, member_id)
    row = (db.query(BillingLimit).filter_by(limit_key=key).populate_existing().with_for_update().first())
    cap = limit_credits * 1000
    if row is not None:
        if row.accounting_version not in {0, 1}:
            raise ConflictError("额度账本版本无法识别，不能设置额度")
        if (row.enterprise_id, row.category, row.group_id, row.user_id, row.period_start, row.period_end) != (
                enterprise_id, "normal", group_id, member_id, start, end):
            raise ConflictError("额度标识与已有范围不匹配")
    else:
        row = BillingLimit(limit_key=key, enterprise_id=enterprise_id, category="normal", group_id=group_id,
                           user_id=member_id, period_start=start, period_end=end, limit_milli=cap,
                           used_milli=0, reserved_milli=0)
        db.add(row)
        db.flush()
    used, held = _reconcile_limit(db, row, int(wallet.id))
    if cap < used + held:
        raise ConflictError("新额度低于已用和预占金额，不能覆盖")
    row.limit_milli = cap
    row.used_milli = used
    row.reserved_milli = held
    row.accounting_version = 1
    db.flush()
    org._event(db, enterprise_id, actor_id, "billing_limit_set", subject_user_id=member_id, group_id=group_id,
               detail={"scope": scope, "period": period, "limit_milli": cap})
    return _as_dict(row)
