"""Billing ledger. Amounts are integer milli-credits, never floating point."""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.core.database import Base
from app.models.base import IdMixin


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BillingWallet(Base, IdMixin):
    __tablename__ = "billing_wallet"
    __table_args__ = (UniqueConstraint("owner_type", "owner_id", name="uq_billing_wallet_owner"),)
    owner_type = Column(String(20), nullable=False)  # user / enterprise
    owner_id = Column(BigInteger, nullable=False)
    lock_version = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, nullable=False, default=utcnow)


class BillingCreditBatch(Base, IdMixin):
    __tablename__ = "billing_credit_batch"
    __table_args__ = (
        UniqueConstraint("grant_key", name="uq_billing_credit_grant"),
        Index("ix_billing_batch_wallet_expiry", "wallet_id", "expires_at"),
    )
    wallet_id = Column(BigInteger, nullable=False)
    grant_key = Column(String(160), nullable=False)
    kind = Column(String(24), nullable=False)  # subscription / purchase / trial / permanent
    issued_milli = Column(BigInteger, nullable=False)
    remaining_milli = Column(BigInteger, nullable=False)
    reserved_milli = Column(BigInteger, nullable=False, default=0, server_default="0")
    refund_reserved_milli = Column(BigInteger, nullable=False, default=0, server_default="0")
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class BillingLimit(Base, IdMixin):
    __tablename__ = "billing_limit"
    __table_args__ = (
        UniqueConstraint("limit_key", name="uq_billing_limit_key"),
        Index("ix_billing_limit_scope", "enterprise_id", "period_start", "period_end"),
    )
    limit_key = Column(String(160), nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    category = Column(String(20), nullable=False, default="normal", server_default="normal")
    group_id = Column(BigInteger, nullable=True)
    user_id = Column(BigInteger, nullable=True)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    limit_milli = Column(BigInteger, nullable=False)
    used_milli = Column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_milli = Column(BigInteger, nullable=False, default=0, server_default="0")
    accounting_version = Column(Integer, nullable=False, default=0, server_default="0")


class BillingReservation(Base, IdMixin):
    __tablename__ = "billing_reservation"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_billing_reservation_request"),
        Index("ix_billing_reservation_kind_created", "billable_kind", "created_at"),
        Index("ix_billing_reservation_ent_usage_created", "enterprise_id", "usage_kind", "created_at", "id"),
        CheckConstraint(
            "(server_tool_search_calls IS NULL AND server_tool_fetch_calls IS NULL) OR "
            "(server_tool_search_calls IS NOT NULL AND server_tool_fetch_calls IS NOT NULL)",
            name="ck_billing_reservation_server_tool_usage_pair",
        ),
        CheckConstraint(
            "(billable_kind = 'model' AND ((pricing_version IS NULL AND input_rate_numerator IS NULL "
            "AND input_rate_denominator IS NULL AND output_rate_numerator IS NULL "
            "AND output_rate_denominator IS NULL) OR (pricing_version IS NOT NULL AND pricing_version <> '' "
            "AND input_rate_numerator IS NOT NULL AND input_rate_denominator IS NOT NULL "
            "AND output_rate_numerator IS NOT NULL AND output_rate_denominator IS NOT NULL "
            "AND input_rate_numerator > 0 AND input_rate_denominator > 0 "
            "AND output_rate_numerator > 0 AND output_rate_denominator > 0))) OR "
            "(billable_kind IN ('search', 'fetch') AND pricing_version IS NOT NULL AND pricing_version <> '' "
            "AND input_rate_numerator IS NULL AND input_rate_denominator IS NULL "
            "AND output_rate_numerator IS NULL AND output_rate_denominator IS NULL)",
            name="ck_billing_reservation_rate_snapshot_pair",
        ),
        CheckConstraint(
            "(provider_identity_version IS NULL AND provider_name IS NULL "
            "AND provider_endpoint_host IS NULL AND credential_source IS NULL) OR "
            "(provider_identity_version IS NOT NULL AND provider_name IS NOT NULL "
            "AND provider_endpoint_host IS NOT NULL AND credential_source IS NOT NULL "
            "AND provider_identity_version = 'deepseek-official-v1' AND provider_name = 'deepseek' "
            "AND provider_endpoint_host = 'api.deepseek.com' AND credential_source IN ('system', 'global'))",
            name="ck_billing_reservation_provider_identity_pair",
        ),
        CheckConstraint(
            "billable_kind IN ('model', 'search', 'fetch') AND ("
            "(billable_kind = 'model' AND tool_name IS NULL "
            "AND tool_cost_cny_micros_per_success IS NULL AND tool_price_milli_per_success IS NULL "
            "AND tool_successful_calls IS NULL) OR "
            "(billable_kind IN ('search', 'fetch') AND tool_name IS NOT NULL "
            "AND tool_name = billable_kind AND tool_cost_cny_micros_per_success IS NOT NULL "
            "AND tool_price_milli_per_success IS NOT NULL "
            "AND tool_cost_cny_micros_per_success > 0 AND tool_price_milli_per_success > 0 "
            "AND pricing_version IS NOT NULL AND provider_identity_version IS NOT NULL "
            "AND input_rate_numerator IS NULL AND input_rate_denominator IS NULL "
            "AND output_rate_numerator IS NULL AND output_rate_denominator IS NULL))",
            name="ck_billing_reservation_billable_kind_snapshot",
        ),
        CheckConstraint(
            "tool_successful_calls IS NULL OR tool_successful_calls IN (0, 1)",
            name="ck_billing_reservation_tool_success_count",
        ),
    )
    request_key = Column(String(160), nullable=False)
    model_name = Column(String(80), nullable=False)
    billable_kind = Column(String(16), nullable=False, default="model", server_default="model")
    tool_name = Column(String(16), nullable=True)
    tool_cost_cny_micros_per_success = Column(BigInteger, nullable=True)
    tool_price_milli_per_success = Column(BigInteger, nullable=True)
    tool_successful_calls = Column(BigInteger, nullable=True)
    provider_attempt_started_at = Column(DateTime, nullable=True)
    provider_identity_version = Column(String(32), nullable=True)
    provider_name = Column(String(32), nullable=True)
    provider_endpoint_host = Column(String(253), nullable=True)
    credential_source = Column(String(16), nullable=True)
    pricing_version = Column(String(64), nullable=True)
    input_rate_numerator = Column(BigInteger, nullable=True)
    input_rate_denominator = Column(BigInteger, nullable=True)
    output_rate_numerator = Column(BigInteger, nullable=True)
    output_rate_denominator = Column(BigInteger, nullable=True)
    enterprise_id = Column(BigInteger, nullable=True)
    group_id = Column(BigInteger, nullable=True)
    user_id = Column(BigInteger, nullable=True)
    usage_kind = Column(String(20), nullable=False, default="member", server_default="member")
    enterprise_wallet_id = Column(BigInteger, nullable=True)
    personal_wallet_id = Column(BigInteger, nullable=True)
    allow_personal = Column(Integer, nullable=False, default=0)
    requested_milli = Column(BigInteger, nullable=False)
    actual_milli = Column(BigInteger, nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    # DeepSeek server-side Search/Fetch usage is retained for reconciliation;
    # positive counts remain unpriced and cannot be auto-settled.
    server_tool_search_calls = Column(BigInteger, nullable=True)
    server_tool_fetch_calls = Column(BigInteger, nullable=True)
    usage_state = Column(String(16), nullable=False, default="pending", server_default="pending")
    status = Column(String(20), nullable=False, default="reserved")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    settled_at = Column(DateTime, nullable=True)


class BillingReservationPart(Base, IdMixin):
    __tablename__ = "billing_reservation_part"
    __table_args__ = (Index("ix_billing_part_reservation", "reservation_id"),)
    reservation_id = Column(BigInteger, nullable=False)
    wallet_id = Column(BigInteger, nullable=False)
    batch_id = Column(BigInteger, nullable=False)
    reserved_milli = Column(BigInteger, nullable=False)
    charged_milli = Column(BigInteger, nullable=True)


class BillingReservationLimit(Base, IdMixin):
    __tablename__ = "billing_reservation_limit"
    __table_args__ = (UniqueConstraint("reservation_id", "limit_id", name="uq_billing_reservation_limit"),)
    reservation_id = Column(BigInteger, nullable=False)
    limit_id = Column(BigInteger, nullable=False)
    reserved_milli = Column(BigInteger, nullable=False)


class BillingReconciliationReviewerGrant(Base, IdMixin):
    """Explicit, revocable authority for one independent C4 reviewer."""

    __tablename__ = "billing_reconciliation_reviewer_grant"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_billing_c4_reviewer_user"),
        CheckConstraint("active IN (0, 1)", name="ck_billing_c4_reviewer_active"),
    )
    user_id = Column(BigInteger, nullable=False)
    active = Column(Integer, nullable=False, default=0, server_default="0")
    granted_by = Column(BigInteger, nullable=True)
    granted_at = Column(DateTime, nullable=True)
    revoked_by = Column(BigInteger, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class BillingRefundReviewerGrant(Base, IdMixin):
    """Independent authority limited to manual refund review."""

    __tablename__ = "billing_refund_reviewer_grant"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_refund_reviewer_user"),
        CheckConstraint("active IN (0, 1)", name="ck_refund_reviewer_active"),
    )
    user_id = Column(BigInteger, nullable=False)
    active = Column(Integer, nullable=False, default=0, server_default="0")
    granted_by = Column(BigInteger, nullable=True)
    granted_at = Column(DateTime, nullable=True)
    revoked_by = Column(BigInteger, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class BillingRateReviewerGrant(Base, IdMixin):
    """Independent reviewer authority for rate catalogs, separate from C4."""

    __tablename__ = "billing_rate_reviewer_grant"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_rate_reviewer_user"),
        CheckConstraint("active IN (0, 1)", name="ck_rate_reviewer_active"),
    )
    user_id = Column(BigInteger, nullable=False)
    active = Column(Integer, nullable=False, default=0, server_default="0")
    granted_by = Column(BigInteger, nullable=True)
    granted_at = Column(DateTime, nullable=True)
    revoked_by = Column(BigInteger, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class BillingRateCancelRequest(Base, IdMixin):
    """Two-person cancellation request for a future approved catalog."""

    __tablename__ = "billing_rate_cancel_request"
    __table_args__ = (
        UniqueConstraint("active_catalog_id", name="uq_rate_cancel_active"),
        Index("ix_rate_cancel_catalog", "catalog_id", "id"),
        CheckConstraint(
            "(status = 'pending' AND active_catalog_id IS NOT NULL AND active_catalog_id = catalog_id "
            "AND decided_by IS NULL AND decided_at IS NULL AND decision_note IS NULL) OR "
            "(status IN ('approved', 'rejected', 'expired') AND active_catalog_id IS NULL "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND decision_note IS NOT NULL)",
            name="ck_rate_cancel_state",
        ),
    )
    catalog_id = Column(BigInteger, ForeignKey("billing_rate_catalog.id", ondelete="RESTRICT"), nullable=False)
    active_catalog_id = Column(BigInteger, nullable=True)
    status = Column(String(16), nullable=False)
    proposed_by = Column(BigInteger, nullable=False)
    proposal_note = Column(String(500), nullable=False)
    proposed_at = Column(DateTime, nullable=False)
    decided_by = Column(BigInteger, nullable=True)
    decision_note = Column(String(500), nullable=True)
    decided_at = Column(DateTime, nullable=True)


class BillingReservationReconciliationEvent(Base, IdMixin):
    """Append-only two-person review history for unresolved model reservations."""

    __tablename__ = "billing_reservation_reconciliation_event"
    __table_args__ = (
        Index("ix_billing_res_recon_reservation", "reservation_id", "id"),
        CheckConstraint("decision IN ('proposed', 'approved', 'rejected', 'blocked', 'recovered')",
                        name="ck_billing_res_recon_decision"),
        CheckConstraint(
            "(decision = 'blocked' AND (resolution IS NULL OR resolution IN ('settle', 'release_not_sent'))) OR "
            "(decision = 'recovered' AND resolution IS NULL) OR "
            "(decision NOT IN ('blocked', 'recovered') AND resolution IS NOT NULL "
            "AND resolution IN ('settle', 'release_not_sent'))",
                        name="ck_billing_res_recon_resolution"),
        CheckConstraint(
            "(decision = 'proposed' AND related_event_id IS NULL) OR "
            "(decision IN ('approved', 'rejected') AND related_event_id IS NOT NULL) OR "
            "decision = 'blocked' OR (decision = 'recovered' AND related_event_id IS NULL)",
            name="ck_billing_res_recon_related_event",
        ),
        CheckConstraint(
            "(decision IN ('blocked', 'recovered') AND provider_request_id IS NULL AND input_tokens IS NULL "
            "AND output_tokens IS NULL AND actual_milli IS NULL AND evidence_reference IS NULL "
            "AND evidence_sha256 IS NULL) OR "
            "(decision NOT IN ('blocked', 'recovered') AND evidence_reference IS NOT NULL "
            "AND evidence_sha256 IS NOT NULL AND length(evidence_sha256) = 64)",
                        name="ck_billing_res_recon_evidence_hash_length"),
        CheckConstraint(
            "tool_successful_calls IS NULL OR tool_successful_calls IN (0, 1)",
            name="ck_billing_res_recon_tool_success_count",
        ),
    )
    reservation_id = Column(BigInteger, nullable=False)
    related_event_id = Column(BigInteger, nullable=True)
    actor_id = Column(BigInteger, nullable=False)
    decision = Column(String(16), nullable=False)
    resolution = Column(String(24), nullable=True)
    provider_request_id = Column(String(128), nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    actual_milli = Column(BigInteger, nullable=True)
    tool_successful_calls = Column(BigInteger, nullable=True)
    evidence_reference = Column(String(240), nullable=True)
    evidence_sha256 = Column(String(64), nullable=True)
    note = Column(String(500), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class BillingToolExecutionResult(Base, IdMixin):
    """Durable sanitized result paired atomically with a Search/Fetch settlement."""

    __tablename__ = "billing_tool_execution_result"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_billing_tool_result_request"),
        Index("ix_billing_tool_result_created", "created_at"),
        CheckConstraint("length(input_sha256) = 64", name="ck_billing_tool_result_input_sha256"),
    )
    request_key = Column(String(160), nullable=False)
    tool_name = Column(String(16), nullable=False)
    input_sha256 = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class BillingOrder(Base, IdMixin):
    __tablename__ = "billing_order"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_billing_order_no"),
        UniqueConstraint("user_id", "client_request_id", name="uq_billing_order_user_request"),
    )
    order_no = Column(String(64), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    enterprise_id = Column(BigInteger, nullable=True)
    product = Column(String(32), nullable=False)
    amount_fen = Column(BigInteger, nullable=False)
    quantity = Column(Integer, nullable=False)
    channel = Column(String(16), nullable=False)
    status = Column(String(20), nullable=False, default="created")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    enterprise_name = Column(String(120), nullable=True)
    paid_at = Column(DateTime, nullable=True)
    client_request_id = Column(String(80), nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    payment_status = Column(String(20), nullable=False, default="not_started", server_default="not_started")
    payment_code_url = Column(String(2048), nullable=True)
    provider_payment_id = Column(String(128), nullable=True)
    payment_expires_at = Column(DateTime, nullable=True)


class BillingPaymentEvent(Base, IdMixin):
    __tablename__ = "billing_payment_event"
    __table_args__ = (
        UniqueConstraint("channel", "trade_id", name="uq_billing_payment_trade"),
        UniqueConstraint("order_no", name="uq_billing_payment_order"),
    )
    channel = Column(String(16), nullable=False)
    trade_id = Column(String(128), nullable=False)
    order_no = Column(String(64), nullable=False)
    amount_fen = Column(BigInteger, nullable=False)
    verified_at = Column(DateTime, nullable=False, default=utcnow)


class BillingRefund(Base, IdMixin):
    __tablename__ = "billing_refund"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_billing_refund_order"),
        UniqueConstraint("refund_no", name="uq_billing_refund_no"),
        Index("ix_billing_refund_status", "status", "created_at"),
    )
    order_no = Column(String(64), nullable=False)
    refund_no = Column(String(64), nullable=False)
    amount_fen = Column(BigInteger, nullable=False)
    reason = Column(String(500), nullable=False)
    status = Column(String(24), nullable=False, default="requested")
    requested_by = Column(BigInteger, nullable=False)
    reviewed_by = Column(BigInteger, nullable=True)
    review_note = Column(String(500), nullable=True)
    provider_refund_id = Column(String(128), nullable=True)
    provider_status = Column(String(32), nullable=True)
    reconciliation_claim_token = Column(String(64), nullable=True)
    reconciliation_claimed_until = Column(DateTime, nullable=True)
    # NULL means the pre-upgrade ordering is unknown; zero means no query has
    # been applied to the current attempt since it was explicitly prepared.
    reconciliation_query_attempt = Column(Integer, nullable=True)
    refund_creation_attempt = Column(Integer, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    completed_at = Column(DateTime, nullable=True)


class BillingStatementBatch(Base, IdMixin):
    """Immutable metadata for one imported provider statement; source bytes are never retained."""

    __tablename__ = "billing_statement_batch"
    __table_args__ = (
        UniqueConstraint("channel", "bill_date", "bill_type", "source_origin", "file_sha256",
                         name="uq_billing_statement_source"),
        Index("ix_billing_statement_date", "channel", "bill_date", "id"),
    )
    channel = Column(String(16), nullable=False)
    source_origin = Column(String(24), nullable=False, default="manual_upload", server_default="manual_upload")
    bill_date = Column(Date, nullable=False)
    bill_type = Column(String(16), nullable=False)
    file_sha256 = Column(String(64), nullable=False)
    provider_hash_type = Column(String(16), nullable=True)
    provider_hash_value = Column(String(64), nullable=True)
    file_name = Column(String(200), nullable=False)
    file_size = Column(Integer, nullable=False)
    parser_version = Column(String(24), nullable=False)
    row_count = Column(Integer, nullable=False)
    payment_total_fen = Column(BigInteger, nullable=False, default=0, server_default="0")
    refund_total_fen = Column(BigInteger, nullable=False, default=0, server_default="0")
    received_total_fen = Column(BigInteger, nullable=False, default=0, server_default="0")
    received_missing_count = Column(Integer, nullable=False, default=0, server_default="0")
    fee_total_micros = Column(BigInteger, nullable=False, default=0, server_default="0")
    fee_missing_count = Column(Integer, nullable=False, default=0, server_default="0")
    imported_by = Column(BigInteger, nullable=False)
    imported_at = Column(DateTime, nullable=False, default=utcnow)


class BillingStatementRow(Base, IdMixin):
    """Minimized normalized record; provider customer identifiers and raw lines are discarded."""

    __tablename__ = "billing_statement_row"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_line_number", "event_index", name="uq_billing_statement_line_event"),
        Index("ix_billing_statement_row_batch_issue", "batch_id", "discrepancy_code", "id"),
        Index("ix_billing_statement_row_order", "order_no", "record_type"),
    )
    batch_id = Column(BigInteger, nullable=False)
    source_line_number = Column(Integer, nullable=False)
    event_index = Column(Integer, nullable=False, default=0, server_default="0")
    record_type = Column(String(16), nullable=False)  # payment / refund
    order_no = Column(String(64), nullable=True)
    provider_trade_id = Column(String(128), nullable=True)
    refund_no = Column(String(64), nullable=True)
    provider_refund_id = Column(String(128), nullable=True)
    provider_status = Column(String(32), nullable=True)
    currency = Column(String(8), nullable=False, default="CNY", server_default="CNY")
    amount_fen = Column(BigInteger, nullable=False)
    received_fen = Column(BigInteger, nullable=True)
    fee_micros = Column(BigInteger, nullable=True)
    local_order_status = Column(String(20), nullable=True)
    local_amount_fen = Column(BigInteger, nullable=True)
    local_trade_id = Column(String(128), nullable=True)
    local_refund_no = Column(String(64), nullable=True)
    local_provider_refund_id = Column(String(128), nullable=True)
    local_refund_status = Column(String(24), nullable=True)
    local_refund_amount_fen = Column(BigInteger, nullable=True)
    discrepancy_code = Column(String(64), nullable=False)
    review_status = Column(String(24), nullable=False, default="unreviewed", server_default="unreviewed")
    last_reviewed_by = Column(BigInteger, nullable=True)
    last_reviewed_at = Column(DateTime, nullable=True)
    last_review_note = Column(String(500), nullable=True)


class BillingStatementReviewEvent(Base, IdMixin):
    """Append-only human review history; decisions never modify payment or entitlement records."""

    __tablename__ = "billing_statement_review_event"
    __table_args__ = (Index("ix_billing_statement_review_row", "statement_row_id", "id"),)
    statement_row_id = Column(BigInteger, nullable=False)
    actor_id = Column(BigInteger, nullable=False)
    decision = Column(String(24), nullable=False)
    note = Column(String(500), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class BillingSubscription(Base, IdMixin):
    __tablename__ = "billing_subscription"
    __table_args__ = (UniqueConstraint("owner_type", "owner_id", "product", name="uq_billing_subscription_owner"),)
    owner_type = Column(String(20), nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    product = Column(String(24), nullable=False)
    anchor_at = Column(DateTime, nullable=False)
    last_end_at = Column(DateTime, nullable=False)
    next_offset = Column(Integer, nullable=False, default=1)


class BillingSubscriptionPeriod(Base, IdMixin):
    __tablename__ = "billing_subscription_period"
    __table_args__ = (UniqueConstraint("order_no", name="uq_billing_period_order"),)
    subscription_id = Column(BigInteger, nullable=False)
    order_no = Column(String(64), nullable=False)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    credit_milli = Column(BigInteger, nullable=False)
    seats = Column(Integer, nullable=False, default=1)
    granted_at = Column(DateTime, nullable=True)


class BillingPermanentReviewer(Base, IdMixin):
    __tablename__ = "billing_permanent_reviewer"
    __table_args__ = (UniqueConstraint("user_id", name="uq_billing_permanent_reviewer_user"),)
    user_id = Column(BigInteger, nullable=False)
    status = Column(String(16), nullable=False, default="active")
    granted_by = Column(BigInteger, nullable=False)
    granted_at = Column(DateTime, nullable=False, default=utcnow)
    revoked_by = Column(BigInteger, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class BillingPermanentReviewerEvent(Base, IdMixin):
    __tablename__ = "billing_permanent_reviewer_event"
    user_id = Column(BigInteger, nullable=False)
    actor_id = Column(BigInteger, nullable=False)
    action = Column(String(16), nullable=False)
    reason = Column(String(500), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class BillingRateCatalog(Base, IdMixin):
    """Versioned pricing revision with append-only lifecycle events."""

    __tablename__ = "billing_rate_catalog"
    __table_args__ = (
        UniqueConstraint("version_key", name="uq_billing_rate_catalog_version"),
        UniqueConstraint("approved_effective_at", name="uq_billing_rate_catalog_approved_effective_at"),
        Index("ix_billing_rate_catalog_status_effective", "status", "effective_at"),
        CheckConstraint("status IN ('draft', 'approved', 'rejected', 'canceled')",
                        name="ck_billing_rate_catalog_status"),
        CheckConstraint(
            "(status = 'approved' AND approved_effective_at = effective_at) OR "
            "(status <> 'approved' AND approved_effective_at IS NULL)",
            name="ck_billing_rate_catalog_approved_time_pair",
        ),
        CheckConstraint("length(version_key) = 64", name="ck_billing_rate_catalog_version_length"),
        CheckConstraint("length(source_sha256) = 64", name="ck_billing_rate_catalog_source_digest_length"),
        CheckConstraint(
            "(model_cny_source_url IS NULL AND model_cny_source_checked_at IS NULL) OR "
            "(model_cny_source_url IS NOT NULL AND model_cny_source_checked_at IS NOT NULL)",
            name="ck_bill_model_cny_source_pair",
        ),
        CheckConstraint(
            "fx_cny_per_usd_scaled > 0 AND fx_scale = 10000 AND credit_pack_price_fen > 0 "
            "AND credit_pack_credits > 0",
            name="ck_billing_rate_catalog_financial_basis",
        ),
        CheckConstraint(
            "(tool_search_cost_cny_micros_per_success = 0 AND tool_search_price_milli_per_success = 0) OR "
            "(tool_search_cost_cny_micros_per_success > 0 AND tool_search_price_milli_per_success > 0)",
            name="ck_billing_catalog_search_cost_price_pair",
        ),
        CheckConstraint(
            "(tool_fetch_cost_cny_micros_per_success = 0 AND tool_fetch_price_milli_per_success = 0) OR "
            "(tool_fetch_cost_cny_micros_per_success > 0 AND tool_fetch_price_milli_per_success > 0)",
            name="ck_billing_catalog_fetch_cost_price_pair",
        ),
        CheckConstraint(
            "(tool_cny_source_url IS NULL AND tool_cny_source_checked_at IS NULL "
            "AND tool_usd_source_url IS NULL AND tool_usd_source_checked_at IS NULL "
            "AND tool_search_cost_cny_micros_per_success = 0 "
            "AND tool_search_price_milli_per_success = 0 "
            "AND tool_fetch_cost_cny_micros_per_success = 0 "
            "AND tool_fetch_price_milli_per_success = 0) OR "
            "(tool_cny_source_url IS NOT NULL AND tool_cny_source_checked_at IS NOT NULL "
            "AND tool_usd_source_url IS NOT NULL AND tool_usd_source_checked_at IS NOT NULL)",
            name="ck_billing_catalog_tool_source_pair",
        ),
    )
    version_key = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="draft", server_default="draft")
    effective_at = Column(DateTime, nullable=False)
    approved_effective_at = Column(DateTime, nullable=True)
    source_url = Column(String(512), nullable=False)
    source_checked_at = Column(DateTime, nullable=False)
    model_cny_source_url = Column(String(512), nullable=True)
    model_cny_source_checked_at = Column(DateTime, nullable=True)
    fx_source_url = Column(String(512), nullable=False)
    fx_source_checked_at = Column(DateTime, nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    fx_cny_per_usd_scaled = Column(BigInteger, nullable=False)
    fx_scale = Column(Integer, nullable=False, default=10000, server_default="10000")
    credit_pack_price_fen = Column(BigInteger, nullable=False)
    credit_pack_credits = Column(BigInteger, nullable=False)
    tool_cny_source_url = Column(String(512), nullable=True)
    tool_cny_source_checked_at = Column(DateTime, nullable=True)
    tool_usd_source_url = Column(String(512), nullable=True)
    tool_usd_source_checked_at = Column(DateTime, nullable=True)
    tool_search_cost_cny_micros_per_success = Column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    tool_search_price_milli_per_success = Column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    tool_fetch_cost_cny_micros_per_success = Column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    tool_fetch_price_milli_per_success = Column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    change_note = Column(String(500), nullable=False)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    approved_by = Column(BigInteger, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_by = Column(BigInteger, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    canceled_by = Column(BigInteger, nullable=True)
    canceled_at = Column(DateTime, nullable=True)
    transition_note = Column(String(500), nullable=True)


class BillingRateCatalogItem(Base, IdMixin):
    """Consumer debit and supplier cost for one model in a catalog revision."""

    __tablename__ = "billing_rate_catalog_item"
    __table_args__ = (
        UniqueConstraint("catalog_id", "model_name", name="uq_billing_rate_catalog_item_model"),
        Index("ix_billing_rate_catalog_item_catalog", "catalog_id", "id"),
        CheckConstraint(
            "(provider_input_cny_micros_per_million IS NULL AND provider_output_cny_micros_per_million IS NULL) OR "
            "(provider_input_cny_micros_per_million IS NOT NULL AND provider_output_cny_micros_per_million IS NOT NULL "
            "AND provider_input_cny_micros_per_million > 0 AND provider_output_cny_micros_per_million > 0)",
            name="ck_bill_model_cny_cost_pair",
        ),
        CheckConstraint(
            "input_rate_numerator > 0 AND input_rate_denominator > 0 "
            "AND output_rate_numerator > 0 AND output_rate_denominator > 0 "
            "AND provider_input_usd_micros_per_million >= 0 "
            "AND provider_output_usd_micros_per_million >= 0",
            name="ck_billing_rate_catalog_item_rates_positive",
        ),
    )
    catalog_id = Column(BigInteger, ForeignKey(
        "billing_rate_catalog.id", ondelete="RESTRICT", name="fk_billing_rate_catalog_item_catalog",
    ), nullable=False)
    model_name = Column(String(80), nullable=False)
    input_rate_numerator = Column(BigInteger, nullable=False)
    input_rate_denominator = Column(BigInteger, nullable=False)
    output_rate_numerator = Column(BigInteger, nullable=False)
    output_rate_denominator = Column(BigInteger, nullable=False)
    provider_input_usd_micros_per_million = Column(BigInteger, nullable=False)
    provider_output_usd_micros_per_million = Column(BigInteger, nullable=False)
    provider_input_cny_micros_per_million = Column(BigInteger, nullable=True)
    provider_output_cny_micros_per_million = Column(BigInteger, nullable=True)


class BillingRateCatalogEvent(Base, IdMixin):
    """Append-only audit trail for catalog lifecycle transitions."""

    __tablename__ = "billing_rate_catalog_event"
    __table_args__ = (
        Index("ix_billing_rate_catalog_event_catalog", "catalog_id", "id"),
        CheckConstraint("action IN ('created', 'approved', 'rejected', 'canceled')",
                        name="ck_billing_rate_catalog_event_action"),
    )
    catalog_id = Column(BigInteger, ForeignKey(
        "billing_rate_catalog.id", ondelete="RESTRICT", name="fk_billing_rate_catalog_event_catalog",
    ), nullable=False)
    actor_id = Column(BigInteger, nullable=True)
    action = Column(String(16), nullable=False)
    note = Column(String(500), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
