"""Independent, revocable manual refund reviewer delegation."""

import sqlalchemy as sa

from alembic import op

revision = "109_refund_reviewer_grant"
down_revision = "108_personal_agent_publication"
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.add_column("billing_refund", sa.Column("reconciliation_query_attempt", sa.Integer(), nullable=True))
    op.add_column("billing_refund", sa.Column("refund_creation_attempt", sa.Integer(), nullable=True))
    op.create_table(
        "billing_refund_reviewer_grant",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("granted_by", sa.BigInteger(), nullable=True),
        sa.Column("granted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_refund_reviewer_user"),
        sa.CheckConstraint("active IN (0, 1)", name="ck_refund_reviewer_active"),
    )


def downgrade():
    raise RuntimeError("退款复核委任记录不可自动降级，请按备份恢复流程处理")
