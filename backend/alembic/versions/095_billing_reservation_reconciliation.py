"""Add append-only review events for unresolved model billing reservations.

Revision ID: 095_billing_res_reconcile
Revises: 094_enterprise_agent_teams
"""

import sqlalchemy as sa

from alembic import op

revision = "095_billing_res_reconcile"
down_revision = "094_enterprise_agent_teams"
branch_labels = None
depends_on = None


def _id_type():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "billing_reservation_reconciliation_event",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("related_event_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("resolution", sa.String(24), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_milli", sa.BigInteger(), nullable=True),
        sa.Column("evidence_reference", sa.String(240), nullable=True),
        sa.Column("evidence_sha256", sa.String(64), nullable=True),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("decision IN ('proposed', 'approved', 'rejected', 'blocked')",
                           name="ck_billing_res_recon_decision"),
        sa.CheckConstraint(
            "(decision = 'blocked' AND (resolution IS NULL OR resolution IN ('settle', 'release_not_sent'))) OR "
            "(decision <> 'blocked' AND resolution IS NOT NULL "
            "AND resolution IN ('settle', 'release_not_sent'))",
                           name="ck_billing_res_recon_resolution"),
        sa.CheckConstraint(
            "(decision = 'proposed' AND related_event_id IS NULL) OR "
            "(decision IN ('approved', 'rejected') AND related_event_id IS NOT NULL) OR "
            "decision = 'blocked'",
            name="ck_billing_res_recon_related_event",
        ),
        sa.CheckConstraint(
            "(decision = 'blocked' AND provider_request_id IS NULL AND input_tokens IS NULL "
            "AND output_tokens IS NULL AND actual_milli IS NULL AND evidence_reference IS NULL "
            "AND evidence_sha256 IS NULL) OR "
            "(decision <> 'blocked' AND evidence_reference IS NOT NULL "
            "AND evidence_sha256 IS NOT NULL AND length(evidence_sha256) = 64)",
                           name="ck_billing_res_recon_evidence_hash_length"),
    )
    op.create_index(
        "ix_billing_res_recon_reservation",
        "billing_reservation_reconciliation_event",
        ["reservation_id", "id"],
    )


def downgrade():
    op.drop_index("ix_billing_res_recon_reservation", table_name="billing_reservation_reconciliation_event")
    op.drop_table("billing_reservation_reconciliation_event")
