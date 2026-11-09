"""Decision table for the sole refund-related access predicate.

The generated combinations include impossible/corrupted states to make the
current policy observable; they do not assert that such transitions are valid.
"""

from itertools import product

from sqlalchemy import select

from app.models.billing_ledger import BillingRefund
from app.services.billing_refund_policy import pending_access_expression, refund_requires_reconciliation


def _expected(status, provider, attempts, creation, query):
    needs_reconcile = (
        status in {"approved", "failed", "manual_review"}
        and attempts > 0
        and (provider != "FAILED" or creation != attempts or query != attempts)
    )
    pending = (
        status in {"processing", "unknown"}
        or status == "approved" and provider in {None, "SUCCESS", "PROCESSING", "UNKNOWN", "NOT_FOUND"}
        or status == "manual_review" and provider in {"SUCCESS", "PROCESSING", "UNKNOWN", "NOT_FOUND"}
        or needs_reconcile
    )
    return pending, needs_reconcile


def test_refund_access_expression_and_reconciliation_match_decision_table(db):
    statuses = ("requested", "manual_review", "approved", "processing", "unknown", "failed", "succeeded", "rejected")
    providers = (None, "SUCCESS", "PROCESSING", "UNKNOWN", "NOT_FOUND", "FAILED")
    rows = []
    for index, (status, provider, attempts, creation, query) in enumerate(product(
        statuses, providers, (0, 1, 2), (None, 0, 1, 2), (None, 0, 1, 2),
    )):
        row = BillingRefund(order_no=f"policy-order-{index}", refund_no=f"policy-refund-{index}", amount_fen=1,
                            reason="测试", requested_by=1, status=status, provider_status=provider,
                            attempt_count=attempts, refund_creation_attempt=creation,
                            reconciliation_query_attempt=query)
        db.add(row)
        rows.append(row)
    db.flush()
    actual = dict(db.execute(select(BillingRefund.id, pending_access_expression())).all())
    assert len(rows) == len(actual) == 2304
    for row in rows:
        expected_pending, expected_reconcile = _expected(
            row.status, row.provider_status, row.attempt_count,
            row.refund_creation_attempt, row.reconciliation_query_attempt,
        )
        assert bool(actual[row.id]) is expected_pending, (row.status, row.provider_status, row.attempt_count,
                                                         row.refund_creation_attempt, row.reconciliation_query_attempt)
        assert refund_requires_reconciliation(row) is expected_reconcile
