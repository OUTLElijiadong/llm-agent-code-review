"""Isolated enterprise remote-test approval, without any execution integration.

Revision ID: 074_enterprise_remote_approval
Revises: 073_enterprise_asset_drafts
"""
import sqlalchemy as sa

from alembic import op

revision = "074_enterprise_remote_approval"
down_revision = "073_enterprise_asset_drafts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "enterprise_remote_approval",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("target_url", sa.String(512), nullable=False),
        sa.Column("target_host", sa.String(255), nullable=False),
        sa.Column("authorization_basis", sa.Text(), nullable=False),
        sa.Column("scope_notes", sa.Text(), nullable=False),
        sa.Column("requested_window_hours", sa.Integer(), nullable=False),
        sa.Column("scope_version", sa.String(32), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_enterprise_remote_approval_status", "enterprise_remote_approval",
                    ["enterprise_id", "status", "id"])
    op.create_index("ix_enterprise_remote_approval_group", "enterprise_remote_approval",
                    ["enterprise_id", "group_id", "status", "id"])
    op.create_index("ix_enterprise_remote_approval_owner", "enterprise_remote_approval",
                    ["enterprise_id", "owner_id", "id"])


def downgrade():
    raise RuntimeError("企业远测审批记录不可通过降级删除")
