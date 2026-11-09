"""Index enterprise billing history used to reconcile monthly quota changes."""

import sqlalchemy as sa

from alembic import op

revision = "112_quota_history_index"
down_revision = "111_pentest_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("billing_limit", sa.Column("accounting_version", sa.Integer(), nullable=False,
                                              server_default="0"))
    op.create_index(
        "ix_billing_reservation_ent_usage_created",
        "billing_reservation",
        ["enterprise_id", "usage_kind", "created_at", "id"],
    )


def downgrade():
    raise RuntimeError("额度历史校验是前向迁移，请通过备份恢复")
