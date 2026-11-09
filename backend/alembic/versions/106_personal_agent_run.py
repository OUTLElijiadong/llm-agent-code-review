"""Add a private personal Agent single-attempt billing run."""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from alembic import op

revision = "106_personal_agent_run"
down_revision = "105_model_cny_cost_sources"
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "personal_agent_run",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_id", sa.BigInteger(), sa.ForeignKey("personal_agent.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_id", sa.BigInteger(), sa.ForeignKey("personal_agent_version.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("reservation_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_text", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True),
        sa.Column("result_reason", sa.String(32), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_id", "request_key", name="uq_personal_agent_run_key"),
        sa.UniqueConstraint("reservation_key", name="uq_personal_agent_run_res"),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'awaiting_reconciliation', 'result_unavailable')",
                           name="ck_personal_agent_run_status"),
    )
    op.create_index("ix_personal_agent_run_owner", "personal_agent_run", ["owner_id", "id"])


def downgrade():
    raise RuntimeError("个人 Agent 运行账单不能自动降级，请按备份恢复流程处理")
