"""Explicit C4 reviewer delegation, disabled by default."""

import sqlalchemy as sa

from alembic import op

revision = "104_c4_reviewer_grant"
down_revision = "103_personal_agent"
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "billing_reconciliation_reviewer_grant",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("granted_by", sa.BigInteger(), nullable=True),
        sa.Column("granted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_billing_c4_reviewer_user"),
        sa.CheckConstraint("active IN (0, 1)", name="ck_billing_c4_reviewer_active"),
    )


def downgrade():
    raise RuntimeError("C4 委任记录不可自动降级，请先按备份恢复流程处理")
