"""persist account-scoped, single-claim browser action receipts

Revision ID: 063_agent_browser_actions
Revises: 062_audit_log_action_length
"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

from alembic import op

revision = "063_agent_browser_actions"
down_revision = "062_audit_log_action_length"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_browser_action",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("session_key", sa.String(128), nullable=False),
        sa.Column("surface", sa.String(24), nullable=False),
        sa.Column("call_id", sa.String(160), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("arguments_json", LONGTEXT().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column("observation_snapshot_id", sa.String(32), nullable=False),
        sa.Column("nonce_sha256", sa.String(64), nullable=False),
        sa.Column("claim_sha256", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("observation_json", LONGTEXT().with_variant(sa.Text(), "sqlite")),
        sa.Column("error", sa.String(500)),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
    )
    op.create_index("uq_agent_browser_request", "agent_browser_action", ["request_id"], unique=True)
    op.create_index("ix_agent_browser_owner_session", "agent_browser_action", ["user_id", "surface", "session_key", "run_id"])


def downgrade() -> None:
    # 执行回执是防重放依据；禁止有记录时静默抹除。
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM agent_browser_action")).scalar_one():
        raise RuntimeError("拒绝删除非空浏览器动作账本；请先制定保留回执的回退方案")
    op.drop_table("agent_browser_action")
