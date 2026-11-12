"""Idempotency records for enterprise Xiaoling published-asset calls.

Revision ID: 085_ent_asset_invocations
Revises: 084_ent_asset_publications
"""

import sqlalchemy as sa

from alembic import op

revision = "085_ent_asset_invocations"
down_revision = "084_ent_asset_publications"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "enterprise_assistant_asset_invocation",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True,
                  autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("assistant_run_id", sa.BigInteger(), nullable=False),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=120), nullable=False),
        sa.Column("invocation_id", sa.String(length=80), nullable=False),
        sa.Column("publication_id", sa.BigInteger(), nullable=False),
        sa.Column("publication_version_id", sa.BigInteger(), nullable=False),
        sa.Column("publication_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_key_prefix", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("execution_token", sa.String(length=80), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["assistant_run_id"], ["enterprise_assistant_run.id"],
            name="fk_ent_asset_invocation_run", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"], ["enterprise_asset_publication.id"],
            name="fk_ent_asset_invocation_publication", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("assistant_run_id", "tool_call_id", name="uq_ent_asset_call_invocation"),
        sa.UniqueConstraint("invocation_id", name="uq_ent_asset_invocation_public_id"),
        sa.UniqueConstraint("request_key_prefix", name="uq_ent_asset_invocation_request"),
    )
    op.create_index(
        "ix_ent_asset_invocation_scope", "enterprise_assistant_asset_invocation",
        ["enterprise_id", "conversation_id", "status", "id"],
    )


def downgrade():
    raise RuntimeError("企业资产调用账本属于审计与幂等记录，不可静默删除；请通过备份恢复")
