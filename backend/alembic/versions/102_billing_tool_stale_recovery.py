"""Audit the safe transition from abandoned Search/Fetch holds to unknown.

Revision ID: 102_billing_tool_recovery
Revises: 101_billing_tool_exec_result
"""

from alembic import op

revision = "102_billing_tool_recovery"
down_revision = "101_billing_tool_exec_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("billing_reservation_reconciliation_event") as batch:
        batch.drop_constraint("ck_billing_res_recon_decision", type_="check")
        batch.drop_constraint("ck_billing_res_recon_resolution", type_="check")
        batch.drop_constraint("ck_billing_res_recon_related_event", type_="check")
        batch.drop_constraint("ck_billing_res_recon_evidence_hash_length", type_="check")
        batch.create_check_constraint(
            "ck_billing_res_recon_decision",
            "decision IN ('proposed', 'approved', 'rejected', 'blocked', 'recovered')",
        )
        batch.create_check_constraint(
            "ck_billing_res_recon_resolution",
            "(decision = 'blocked' AND (resolution IS NULL OR resolution IN ('settle', 'release_not_sent'))) OR "
            "(decision = 'recovered' AND resolution IS NULL) OR "
            "(decision NOT IN ('blocked', 'recovered') AND resolution IS NOT NULL "
            "AND resolution IN ('settle', 'release_not_sent'))",
        )
        batch.create_check_constraint(
            "ck_billing_res_recon_related_event",
            "(decision = 'proposed' AND related_event_id IS NULL) OR "
            "(decision IN ('approved', 'rejected') AND related_event_id IS NOT NULL) OR "
            "decision = 'blocked' OR (decision = 'recovered' AND related_event_id IS NULL)",
        )
        batch.create_check_constraint(
            "ck_billing_res_recon_evidence_hash_length",
            "(decision IN ('blocked', 'recovered') AND provider_request_id IS NULL AND input_tokens IS NULL "
            "AND output_tokens IS NULL AND actual_milli IS NULL AND evidence_reference IS NULL "
            "AND evidence_sha256 IS NULL) OR "
            "(decision NOT IN ('blocked', 'recovered') AND evidence_reference IS NOT NULL "
            "AND evidence_sha256 IS NOT NULL AND length(evidence_sha256) = 64)",
        )


def downgrade() -> None:
    raise RuntimeError("不得删除 Search/Fetch 陈旧预占的恢复审计证据")
