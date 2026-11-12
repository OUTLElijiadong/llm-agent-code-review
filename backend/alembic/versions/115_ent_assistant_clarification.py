"""Persist scoped, versioned clarification evidence without granting authority."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "115_ent_assistant_clarification"
down_revision = "114_pentest_parent_exit_proof"
branch_labels = None
depends_on = None


def upgrade():
    ident = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    json_text = mysql.LONGTEXT().with_variant(sa.Text(), "sqlite")
    op.create_table(
        "enterprise_assistant_clarification",
        sa.Column("id", ident, primary_key=True, autoincrement=True),
        sa.Column("assistant_run_id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("call_id", sa.String(120), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_json", json_text, nullable=False),
        sa.Column("arguments_json", json_text, nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("authority_sha256", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("assistant_run_id", "phase", "call_id", "version", name="uq_ent_clarify_call_version"),
        sa.UniqueConstraint("id", "enterprise_id", "conversation_id", name="uq_ent_clarify_scope"),
        sa.ForeignKeyConstraint(
            ["assistant_run_id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id"],
            ["enterprise_assistant_run.id", "enterprise_assistant_run.enterprise_id",
             "enterprise_assistant_run.conversation_id", "enterprise_assistant_run.initiator_user_id",
             "enterprise_assistant_run.source_message_id"],
            name="fk_ent_clarify_run_scope", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("phase IN ('model_send', 'tool')", name="ck_ent_clarify_phase"),
        sa.CheckConstraint("status IN ('pending', 'cancelled', 'superseded')", name="ck_ent_clarify_status"),
    )
    op.create_index("ix_ent_clarify_scope_status", "enterprise_assistant_clarification",
                    ["enterprise_id", "conversation_id", "status", "id"])
    op.create_table(
        "enterprise_assistant_clarification_decision",
        sa.Column("id", ident, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("author_rank", sa.Integer(), nullable=True),
        sa.Column("decider_rank", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("request_id", "source_message_id", name="uq_ent_clarify_source_decision"),
        sa.ForeignKeyConstraint(
            ["request_id", "enterprise_id", "conversation_id"],
            ["enterprise_assistant_clarification.id", "enterprise_assistant_clarification.enterprise_id",
             "enterprise_assistant_clarification.conversation_id"],
            name="fk_ent_clarify_decision_scope", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id", "enterprise_id", "conversation_id", "author_user_id"],
            ["enterprise_chat_message.id", "enterprise_chat_message.enterprise_id",
             "enterprise_chat_message.conversation_id", "enterprise_chat_message.sender_id"],
            name="fk_ent_clarify_decision_message", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("decision IN ('pending', 'allow', 'deny', 'unrelated')", name="ck_ent_clarify_decision"),
        sa.CheckConstraint(
            "(decision = 'pending' AND decided_by IS NULL AND decided_at IS NULL AND author_rank IS NULL "
            "AND decider_rank IS NULL) OR (decision <> 'pending' AND decided_by IS NOT NULL "
            "AND decided_at IS NOT NULL AND author_rank IS NOT NULL AND decider_rank IS NOT NULL)",
            name="ck_ent_clarify_decision_evidence",
        ),
    )
    op.create_index("ix_ent_clarify_decision_author", "enterprise_assistant_clarification_decision",
                    ["author_user_id", "request_id"])


def downgrade():
    raise RuntimeError("澄清证据不能自动降级，请按备份恢复流程处理")
