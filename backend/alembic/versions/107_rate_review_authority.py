"""Explicit fee-review authority and two-person scheduled cancellation."""

import sqlalchemy as sa

from alembic import op

revision = "107_rate_review_authority"
down_revision = "106_personal_agent_run"
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "billing_rate_reviewer_grant",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("granted_by", sa.BigInteger(), nullable=True),
        sa.Column("granted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_rate_reviewer_user"),
        sa.CheckConstraint("active IN (0, 1)", name="ck_rate_reviewer_active"),
    )
    op.create_table(
        "billing_rate_cancel_request",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("catalog_id", identifier, sa.ForeignKey("billing_rate_catalog.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("active_catalog_id", identifier, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("proposed_by", sa.BigInteger(), nullable=False),
        sa.Column("proposal_note", sa.String(500), nullable=False),
        sa.Column("proposed_at", sa.DateTime(), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decision_note", sa.String(500), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("active_catalog_id", name="uq_rate_cancel_active"),
        sa.CheckConstraint(
            "(status = 'pending' AND active_catalog_id IS NOT NULL AND active_catalog_id = catalog_id "
            "AND decided_by IS NULL AND decided_at IS NULL AND decision_note IS NULL) OR "
            "(status IN ('approved', 'rejected', 'expired') AND active_catalog_id IS NULL "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND decision_note IS NOT NULL)",
            name="ck_rate_cancel_state",
        ),
    )
    op.create_index("ix_rate_cancel_catalog", "billing_rate_cancel_request", ["catalog_id", "id"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER trg_rate_cancel_immutable BEFORE UPDATE ON billing_rate_cancel_request "
            "WHEN OLD.status <> 'pending' OR NEW.catalog_id IS NOT OLD.catalog_id "
            "OR NEW.proposed_by IS NOT OLD.proposed_by OR NEW.proposal_note IS NOT OLD.proposal_note "
            "OR NEW.proposed_at IS NOT OLD.proposed_at "
            "BEGIN SELECT RAISE(ABORT, 'rate cancellation evidence is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_rate_cancel_no_delete BEFORE DELETE ON billing_rate_cancel_request "
            "BEGIN SELECT RAISE(ABORT, 'rate cancellation evidence is immutable'); END"
        )
    elif op.get_bind().dialect.name == "mysql":
        op.execute(
            "CREATE TRIGGER trg_rate_cancel_immutable BEFORE UPDATE ON billing_rate_cancel_request "
            "FOR EACH ROW BEGIN IF OLD.status <> 'pending' OR NOT (NEW.catalog_id <=> OLD.catalog_id "
            "AND NEW.proposed_by <=> OLD.proposed_by AND NEW.proposal_note <=> OLD.proposal_note "
            "AND NEW.proposed_at <=> OLD.proposed_at) THEN SIGNAL SQLSTATE '45000' "
            "SET MESSAGE_TEXT = 'rate cancellation evidence is immutable'; END IF; END"
        )
        op.execute(
            "CREATE TRIGGER trg_rate_cancel_no_delete BEFORE DELETE ON billing_rate_cancel_request "
            "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'rate cancellation evidence is immutable'"
        )


def downgrade():
    raise RuntimeError("费率复核和取消记录不可自动降级，请使用备份恢复流程")
