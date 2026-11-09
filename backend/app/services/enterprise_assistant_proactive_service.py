"""Opt-in enterprise proactive chat: durable human-message triggers and budget."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, ForbiddenError, ValidationError
from app.models.billing_ledger import BillingLimit, BillingReservation
from app.models.enterprise_chat import EnterpriseAssistantProactiveEvent, EnterpriseConversation
from app.models.enterprise_org import Enterprise
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_quota_service as quota
from app.services import enterprise_org_service as org

MIN_INTERVAL_SECONDS = 180
MAX_DAILY_EVENTS = 20
MAX_MONTHLY_CREDITS = 100_000_000
MAX_EVENT_AGE = timedelta(minutes=10)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _company_and_admin(db: Session, enterprise_id: int, actor_id: int, *, lock: bool):
    company = org._company(db, enterprise_id, lock=lock)
    member = org.require_enterprise_member(db, enterprise_id, actor_id, lock=lock)
    if member.role not in {"owner", "admin"}:
        raise ForbiddenError("只有企业管理员可以管理主动讨论")
    return company


def _view(company):
    return {"enabled": bool(company.proactive_enabled),
            "monthly_credits": int(company.proactive_monthly_credits or 0),
            "version": int(company.proactive_version or 0),
            "metering_ready": bool(settings.billing_commercial_metering_ready),
            "worker_ready": bool(settings.enterprise_assistant_worker_enabled)}


def get_policy(db: Session, enterprise_id: int, actor_id: int):
    return _view(_company_and_admin(db, enterprise_id, actor_id, lock=False))


def ensure_auto_budget(db: Session, company, *, now: datetime | None = None):
    """Create this month's enterprise-only cap under the company and wallet locks."""
    observed = now or _now()
    local = observed.replace(tzinfo=timezone.utc).astimezone(quota.SHANGHAI)
    period = f"{local.year:04d}-{local.month:02d}"
    start, end = quota._period_bounds(period)
    wallet, snapshot, created = quota._lock_enterprise_wallet(db, int(company.id))
    if not created:
        ledger._require_wallet_snapshot(snapshot, wallet)
    active_auto_rows = (db.query(BillingLimit).filter(
        BillingLimit.enterprise_id == company.id, BillingLimit.category == "auto",
        BillingLimit.period_start <= observed, BillingLimit.period_end > observed,
    ).order_by(BillingLimit.id).populate_existing().with_for_update().all())
    for limit in active_auto_rows:
        if (limit.accounting_version != 1 or min(limit.limit_milli, limit.used_milli, limit.reserved_milli) < 0
                or limit.used_milli + limit.reserved_milli > limit.limit_milli):
            raise ConflictError("企业主动预算账本无法核验")
    rows = (db.query(BillingLimit).filter_by(enterprise_id=company.id, category="auto",
                                             group_id=None, user_id=None, period_start=start, period_end=end)
            .order_by(BillingLimit.id).populate_existing().with_for_update().all())
    if len(rows) > 1:
        raise ConflictError("企业主动讨论预算存在多个记录，需先核对")
    cap = int(company.proactive_monthly_credits or 0) * 1000
    if not rows:
        history = (db.query(BillingReservation.id).filter(
            BillingReservation.enterprise_id == company.id,
            BillingReservation.usage_kind == "enterprise_auto",
            BillingReservation.created_at >= start, BillingReservation.created_at < end,
        ).first())
        if history is not None:
            raise ConflictError("企业主动讨论历史账单缺少预算记录，需先核对")
        row = BillingLimit(limit_key=f"auto:{company.id}:{period}", enterprise_id=company.id,
                           category="auto", group_id=None, user_id=None, period_start=start, period_end=end,
                           limit_milli=cap, used_milli=0, reserved_milli=0, accounting_version=1)
        db.add(row)
        db.flush()
    else:
        row = rows[0]
        if row.limit_key != f"auto:{company.id}:{period}" or row.accounting_version != 1:
            raise ConflictError("企业主动讨论预算记录无法核验")
        if cap < int(row.used_milli) + int(row.reserved_milli):
            raise ConflictError("预算低于本月已用和预占，不能覆盖")
        row.limit_milli = cap
    return row


def set_policy(db: Session, enterprise_id: int, actor_id: int, *, enabled: bool, monthly_credits: int,
               expected_version: int, expected_token_version: int):
    if (type(enabled) is not bool or type(monthly_credits) is not int or not 0 <= monthly_credits <= MAX_MONTHLY_CREDITS
            or type(expected_version) is not int or expected_version < 0
            or type(expected_token_version) is not int or expected_token_version < 0):
        raise ValidationError("主动讨论预算无效")
    if enabled and monthly_credits <= 0:
        raise ValidationError("启用主动讨论必须设置大于零的月预算")
    if enabled and not settings.billing_commercial_metering_ready:
        raise ForbiddenError("平台计量尚未就绪，不能开启主动讨论")
    if enabled and not settings.enterprise_assistant_worker_enabled:
        raise ForbiddenError("主动讨论服务未开启")
    company = _company_and_admin(db, enterprise_id, actor_id, lock=True)
    actor = (db.query(User).filter_by(id=actor_id).populate_existing().with_for_update().one_or_none())
    if actor is None or actor.status != 1 or int(actor.token_version or 0) != expected_token_version:
        raise ForbiddenError("登录状态已变化，请重新登录")
    if enabled and (company.status != "active" or company.billing_suspended):
        raise ForbiddenError("企业已暂停，不能开启主动讨论")
    if int(company.proactive_version or 0) != expected_version:
        raise ConflictError("主动讨论设置已变化，请刷新后重新确认")
    if (bool(company.proactive_enabled), int(company.proactive_monthly_credits or 0)) != (enabled, monthly_credits):
        company.proactive_enabled = enabled
        company.proactive_monthly_credits = monthly_credits
        company.proactive_version = int(company.proactive_version or 0) + 1
        if enabled:
            ensure_auto_budget(db, company)
        org._event(db, enterprise_id, actor_id, "assistant_proactive_policy_set",
                   detail={"enabled": enabled, "monthly_credits": monthly_credits})
        db.flush()
    return _view(company)


def require_live_policy(db: Session, run) -> None:
    """Caller locks company before this current read; changed opt-in invalidates queued work."""
    if getattr(run, "trigger_kind", "manual") != "proactive":
        return
    company = (db.query(Enterprise).filter_by(id=run.enterprise_id)
               .populate_existing().with_for_update().one_or_none())
    if (company is None or not company.proactive_enabled or not settings.billing_commercial_metering_ready
            or not settings.enterprise_assistant_worker_enabled
            or int(company.proactive_monthly_credits or 0) <= 0
            or int(company.proactive_version or 0) != int(run.proactive_version or -1)):
        raise ForbiddenError("主动讨论设置已关闭或变化")


def require_fresh_event(db: Session, run) -> None:
    """Recheck the durable source before claiming and before each provider send."""
    if getattr(run, "trigger_kind", "manual") != "proactive":
        return
    event = (db.query(EnterpriseAssistantProactiveEvent).filter_by(
        run_id=run.id, enterprise_id=run.enterprise_id, conversation_id=run.conversation_id,
        source_message_id=run.source_message_id, actor_user_id=run.initiator_user_id,
        policy_version=run.proactive_version, status="queued",
    ).populate_existing().with_for_update().one_or_none())
    now = _now()
    # MySQL DATETIME without fractional precision can round the inserted
    # timestamp up by one second; reject larger future skew only.
    if event is None or event.create_time < now - MAX_EVENT_AGE or event.create_time > now + timedelta(seconds=1):
        raise ForbiddenError("主动讨论来源已过期或无效")


def record_message_event(db: Session, company, room, message) -> None:
    """Called in the message transaction while the company row is locked."""
    if (not settings.billing_commercial_metering_ready or not settings.enterprise_assistant_worker_enabled
            or not company.proactive_enabled
            or int(company.proactive_monthly_credits or 0) <= 0
            or room.kind not in {"company", "group"} or message.sender_type != "human"
            or message.sender_id is None or "@小菱" in message.content):
        return
    text = message.content.strip()
    if len(text) < 6 and "?" not in text and "？" not in text:
        return
    if db.query(EnterpriseAssistantProactiveEvent.id).filter_by(source_message_id=message.id).first():
        return
    observed = _now()
    room = (db.query(EnterpriseConversation).filter_by(id=room.id, enterprise_id=company.id)
            .populate_existing().with_for_update().one())
    latest = room.proactive_last_event_at
    if latest is not None and observed - latest < timedelta(seconds=MIN_INTERVAL_SECONDS):
        return
    local = observed.replace(tzinfo=timezone.utc).astimezone(quota.SHANGHAI)
    if company.proactive_day != local.date():
        company.proactive_day = local.date()
        company.proactive_daily_count = 0
    if int(company.proactive_daily_count or 0) >= MAX_DAILY_EVENTS:
        return
    company.proactive_daily_count = int(company.proactive_daily_count or 0) + 1
    room.proactive_last_event_at = observed
    db.add(EnterpriseAssistantProactiveEvent(
        enterprise_id=company.id, conversation_id=room.id, source_message_id=message.id,
        actor_user_id=message.sender_id, policy_version=int(company.proactive_version or 0),
        status="pending", create_time=observed, update_time=observed,
    ))
    db.flush()
