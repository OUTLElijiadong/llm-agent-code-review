"""Pure calendar and pricing rules; no entitlement is granted by these helpers."""

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def cycle_boundary(anchor: datetime, month_offset: int) -> datetime:
    """Preserve original day and local time after short months, returning UTC."""
    if type(month_offset) is not int or month_offset < 0:
        raise ValueError("周期偏移必须为非负整数")
    local = _utc(anchor).astimezone(SHANGHAI)
    total_month = local.year * 12 + (local.month - 1) + month_offset
    year, month0 = divmod(total_month, 12)
    day = min(local.day, monthrange(year, month0 + 1)[1])
    return local.replace(year=year, month=month0 + 1, day=day).astimezone(timezone.utc)


def subscription_status(end: datetime, now: datetime) -> str:
    end, now = _utc(end), _utc(now)
    if now < end:
        return "active"
    if now < end + timedelta(days=7):
        return "grace"
    return "expired"


def renewal_anchor(previous_end: datetime, paid_at: datetime) -> datetime:
    previous_end, paid_at = _utc(previous_end), _utc(paid_at)
    return previous_end if paid_at < previous_end + timedelta(days=7) else paid_at


def prorate_seats(*, cycle_start: datetime, cycle_end: datetime, purchased_at: datetime, seats: int):
    """Charge up to a cent, grant down to one milli-credit, never float."""
    start, end, at = _utc(cycle_start), _utc(cycle_end), _utc(purchased_at)
    if type(seats) is not int or not 1 <= seats <= 10000 or not start <= at < end:
        raise ValueError("席位或订阅周期无效")
    total_us = (end - start) // timedelta(microseconds=1)
    remaining_us = (end - at) // timedelta(microseconds=1)
    numerator = 6900 * seats * remaining_us
    return {"amount_fen": (numerator + total_us - 1) // total_us,
            "credit_milli": 5_000_000 * seats * remaining_us // total_us}
