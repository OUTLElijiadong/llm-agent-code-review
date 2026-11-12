"""Add private personal Agent definitions and version snapshots."""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from alembic import op

revision = "103_personal_agent"
down_revision = "102_billing_tool_recovery"
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "personal_agent",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("create_key", sa.String(80), nullable=False),
        sa.Column("create_hash", sa.String(64), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_id", "create_key", name="uq_personal_agent_create"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_personal_agent_status"),
    )
    op.create_index("ix_personal_agent_owner", "personal_agent", ["owner_id", "id"])
    op.create_table(
        "personal_agent_version",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.BigInteger(), sa.ForeignKey("personal_agent.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("prompt", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("agent_id", "version_number", name="uq_personal_agent_version"),
        sa.UniqueConstraint("agent_id", "request_key", name="uq_personal_agent_version_req"),
    )


def downgrade():
    op.drop_table("personal_agent_version")
    op.drop_index("ix_personal_agent_owner", table_name="personal_agent")
    op.drop_table("personal_agent")
