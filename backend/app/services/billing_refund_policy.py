"""Shared refund evidence policy for access, review and reconciliation."""

from sqlalchemy import and_, or_

from app.models.billing_ledger import BillingRefund

IN_FLIGHT_PROVIDER_STATUSES = {"SUCCESS", "PROCESSING", "UNKNOWN", "NOT_FOUND"}


def refund_requires_reconciliation(refund: BillingRefund) -> bool:
    attempts = int(refund.attempt_count or 0)
    return (refund.status in {"approved", "failed", "manual_review"} and attempts > 0
            and (refund.provider_status != "FAILED"
                 or refund.refund_creation_attempt != attempts
                 or refund.reconciliation_query_attempt != attempts))


def requires_reconciliation_expression():
    """SQL equivalent of the instance predicate, including missing evidence."""
    refund = BillingRefund
    return and_(
        refund.status.in_({"approved", "failed", "manual_review"}),
        refund.attempt_count > 0,
        or_(refund.provider_status.is_(None), refund.provider_status != "FAILED",
            refund.refund_creation_attempt.is_(None), refund.reconciliation_query_attempt.is_(None),
            refund.refund_creation_attempt != refund.attempt_count,
            refund.reconciliation_query_attempt != refund.attempt_count),
    )


def pending_access_expression():
    """Exclude orders whose paid entitlement is subject to an unresolved refund."""
    refund = BillingRefund
    return or_(
        refund.status.in_({"processing", "unknown"}),
        and_(refund.status == "approved",
             or_(refund.provider_status.is_(None), refund.provider_status.in_(IN_FLIGHT_PROVIDER_STATUSES))),
        and_(refund.status == "manual_review", refund.provider_status.in_(IN_FLIGHT_PROVIDER_STATUSES)),
        requires_reconciliation_expression(),
    )
