"""Link approved enterprise remote requests to the existing scoped executor.

Revision ID: 078_enterprise_remote_execution
Revises: 077_payment_refunds
"""
import sqlalchemy as sa

from alembic import op

revision = "078_enterprise_remote_execution"
down_revision = "077_payment_refunds"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("enterprise_remote_approval") as batch:
        batch.add_column(sa.Column("execution_task_public_id", sa.String(32), nullable=True))
        batch.create_unique_constraint("uq_enterprise_remote_approval_execution", ["execution_task_public_id"])


def downgrade():
    raise RuntimeError("远测审批执行关联保留安全审计记录；请按备份恢复，禁止单独回退 Alembic 版本号")
