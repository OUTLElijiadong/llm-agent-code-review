"""Persist tenant-scoped XiaoLing Q&A runs and assistant chat messages.

Revision ID: 079_enterprise_assistant_runs
Revises: 078_enterprise_remote_execution
"""

import sqlalchemy as sa

from alembic import op

revision = "079_enterprise_assistant_runs"
down_revision = "078_enterprise_remote_execution"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True,
                     autoincrement=True)


def upgrade():
    op.create_table(
        "enterprise_assistant_run",
        _id(),
        sa.Column("public_id", sa.String(80), nullable=False),
        sa.Column("request_key", sa.String(120), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("public_id", name="uq_enterprise_assistant_public_id"),
        sa.UniqueConstraint("request_key", name="uq_enterprise_assistant_request"),
        sa.UniqueConstraint("enterprise_id", "conversation_id", "source_message_id",
                            name="uq_enterprise_assistant_source"),
        sa.UniqueConstraint("id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id",
                            name="uq_ent_assistant_run_team_source"),
        sa.UniqueConstraint("id", "enterprise_id", "conversation_id", "group_id", "initiator_user_id",
                            "source_message_id", name="uq_ent_assistant_run_team_group"),
    )
    op.create_index(
        "ix_enterprise_assistant_scope_status", "enterprise_assistant_run",
        ["enterprise_id", "conversation_id", "status", "id"],
    )
    with op.batch_alter_table("enterprise_chat_message") as batch:
        batch.alter_column("sender_id", existing_type=sa.BigInteger(), nullable=True)
        batch.add_column(sa.Column("sender_type", sa.String(16), nullable=False, server_default="human"))
        batch.add_column(sa.Column("assistant_run_id", sa.BigInteger(), nullable=True))
        batch.create_unique_constraint("uq_enterprise_chat_assistant_run", ["assistant_run_id"])
        batch.create_foreign_key(
            "fk_enterprise_chat_assistant_run", "enterprise_assistant_run", ["assistant_run_id"], ["id"],
        )


def downgrade():
    raise RuntimeError("企业小菱运行审计与聊天记录不可自动删除；请按备份恢复，禁止单独回退迁移版本")
