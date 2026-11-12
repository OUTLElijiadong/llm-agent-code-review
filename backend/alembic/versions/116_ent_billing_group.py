"""Snapshot billing attribution separately from conversation authorization."""

import sqlalchemy as sa

from alembic import op

revision = "116_ent_billing_group"
down_revision = "115_ent_assistant_clarification"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "enterprise_member",
        sa.Column("billing_group_version", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("enterprise_assistant_run", sa.Column("billing_group_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "enterprise_assistant_run",
        sa.Column("billing_attribution_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "enterprise_assistant_run",
        sa.Column("billing_membership_version", sa.BigInteger(), nullable=True),
    )
    op.add_column("enterprise_assistant_run", sa.Column("billing_user_token_version", sa.Integer(), nullable=True))


def downgrade():
    raise RuntimeError("历史账单归属不可自动降级，请按备份恢复流程处理")
