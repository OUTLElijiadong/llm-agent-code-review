"""Durable opt-in and source-message triggers for enterprise proactive chat."""

import sqlalchemy as sa
from alembic import op

revision = "113_ent_proactive_chat"
down_revision = "112_quota_history_index"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("enterprise", sa.Column("proactive_enabled", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("enterprise", sa.Column("proactive_monthly_credits", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("enterprise", sa.Column("proactive_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("enterprise", sa.Column("proactive_day", sa.Date(), nullable=True))
    op.add_column("enterprise", sa.Column("proactive_daily_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("enterprise_conversation", sa.Column("proactive_last_event_at", sa.DateTime(), nullable=True))
    op.add_column("enterprise_assistant_run", sa.Column("trigger_kind", sa.String(16), nullable=False,
                                                       server_default="manual"))
    op.add_column("enterprise_assistant_run", sa.Column("proactive_version", sa.Integer(), nullable=True))
    op.add_column("enterprise_assistant_runtime_state", sa.Column("proactive_attempt_started_at", sa.DateTime(), nullable=True))
    op.create_table(
        "enterprise_assistant_proactive_event",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT")),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source_message_id", name="uq_ent_proactive_source"),
        sa.ForeignKeyConstraint(
            ["source_message_id", "enterprise_id", "conversation_id", "actor_user_id"],
            ["enterprise_chat_message.id", "enterprise_chat_message.enterprise_id",
             "enterprise_chat_message.conversation_id", "enterprise_chat_message.sender_id"],
            ondelete="RESTRICT", name="fk_ent_proactive_source_scope",
        ),
        sa.CheckConstraint("status IN ('pending', 'queued', 'skipped')", name="ck_ent_proactive_status"),
    )
    op.create_index("ix_ent_proactive_pending", "enterprise_assistant_proactive_event", ["status", "id"])
    op.create_index("ix_ent_proactive_scope_time", "enterprise_assistant_proactive_event",
                    ["enterprise_id", "conversation_id", "create_time"])


def downgrade():
    raise RuntimeError("主动讨论事件和账本不可自动降级，请按备份恢复流程处理")
