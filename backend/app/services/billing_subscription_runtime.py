"""Idempotent periodic activation for paid and permanent reviewer allowances."""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing_ledger import BillingPermanentReviewer
from app.models.user import User

_scheduler = None


def run_billing_cycle_once(*, session_factory=None, now: datetime | None = None) -> dict[str, int]:
    """Activate due paid cycles and issue this month's permanent reviewer credits."""
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=None)
    else:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    db: Session = session_factory()
    try:
        from app.services.billing_checkout_service import activate_due_periods
        from app.services.billing_entitlement_service import issue_permanent_month, sync_reviewer_role

        activated = 0
        try:
            activated_ids = activate_due_periods(db, now=observed_at, limit=1000)
            db.commit()
            activated = len(activated_ids)
        except Exception:
            db.rollback()
            logger.exception("[billing-cycle] paid period activation failed")

        processed = 0
        permanent_ids = (db.query(BillingPermanentReviewer.user_id)
                         .join(User, User.id == BillingPermanentReviewer.user_id)
                         .filter(BillingPermanentReviewer.status == "active", User.status == 1)
                         .order_by(BillingPermanentReviewer.user_id).all())
        for (user_id,) in permanent_ids:
            try:
                changed = issue_permanent_month(db, user_id, now=observed_at)
                db.commit()
                processed += int(changed)
            except Exception:
                db.rollback()
                logger.exception("[billing-cycle] permanent allowance failed user_id={}", user_id)

        reconciled = 0
        if settings.billing_entitlement_enforcement_ready:
            reviewer_ids = (db.query(User.id).filter(User.status == 1, User.role == "reviewer")
                            .order_by(User.id).all())
            for (user_id,) in reviewer_ids:
                try:
                    changed = sync_reviewer_role(db, user_id)
                    db.commit()
                    reconciled += int(changed)
                except Exception:
                    db.rollback()
                    logger.exception("[billing-cycle] reviewer role reconciliation failed user_id={}", user_id)

        return {"activated_periods": activated, "permanent_allowances_processed": processed,
                "reviewer_roles_reconciled": reconciled}
    finally:
        db.close()


def start_billing_subscription_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and getattr(_scheduler, "running", False):
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone=timezone.utc)
    scheduler.add_job(
        run_billing_cycle_once,
        "interval",
        seconds=60,
        id="billing-subscription-cycle",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[billing-cycle] subscription scheduler started")


def stop_billing_subscription_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
