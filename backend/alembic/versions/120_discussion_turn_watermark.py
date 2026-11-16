"""Persist the human-message boundary closed by each published answer.

Revision ID: 120_discussion_turn_watermark
Revises: 119_chat_message_authority
"""

import sqlalchemy as sa

from alembic import op

revision = "120_discussion_turn_watermark"
down_revision = "119_chat_message_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "enterprise_chat_message",
        sa.Column("discussion_closed_through_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_ent_chat_discussion_cutoff",
        "enterprise_chat_message",
        ["enterprise_id", "conversation_id", "discussion_closed_through_message_id"],
    )


def downgrade():
    raise RuntimeError("已发布的小菱讨论边界不能自动降级，请按备份恢复流程处理")
