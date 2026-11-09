"""企业员工会话及消息。

Revision ID: 068_enterprise_chat
Revises: 067_billing_ledger
"""

import sqlalchemy as sa

from alembic import op

revision = "068_enterprise_chat"
down_revision = "067_billing_ledger"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def _timestamps():
    return [
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "enterprise_conversation",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("conversation_key", sa.String(100), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("participant_low_id", sa.BigInteger(), nullable=True),
        sa.Column("participant_high_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "conversation_key", name="uq_enterprise_conversation_key"),
        sa.UniqueConstraint("enterprise_id", "id", name="uq_ent_conversation_scope"),
        sa.UniqueConstraint("enterprise_id", "id", "group_id", name="uq_ent_conversation_scope_group"),
        sa.ForeignKeyConstraint(
            ["enterprise_id", "group_id"], ["enterprise_group.enterprise_id", "enterprise_group.id"],
            ondelete="RESTRICT", name="fk_ent_conversation_group_scope",
        ),
    )
    op.create_index("ix_enterprise_conversation_org", "enterprise_conversation", ["enterprise_id", "id"])
    op.create_table(
        "enterprise_chat_message",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=False),
        sa.Column("client_message_id", sa.String(80), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "conversation_id", "sender_id", "client_message_id", name="uq_enterprise_chat_message_client"
        ),
        sa.UniqueConstraint(
            "id", "enterprise_id", "conversation_id", "sender_id", name="uq_ent_chat_message_team_source"
        ),
    )
    op.create_index(
        "ix_enterprise_chat_message_conversation", "enterprise_chat_message", ["enterprise_id", "conversation_id", "id"]
    )
    op.create_table(
        "enterprise_forum_post",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=True),
        sa.Column("request_digest", sa.String(64), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "author_id", "request_key", name="uq_forum_post_request"),
    )
    op.create_index("ix_enterprise_forum_post_org", "enterprise_forum_post", ["enterprise_id", "status", "id"])
    op.create_table(
        "enterprise_forum_reply",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=True),
        sa.Column("request_digest", sa.String(64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "author_id", "request_key", name="uq_forum_reply_request"),
    )
    op.create_index(
        "ix_enterprise_forum_reply_post", "enterprise_forum_reply", ["enterprise_id", "post_id", "status", "id"]
    )
    op.create_table(
        "enterprise_announcement",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=True),
        sa.Column("request_digest", sa.String(64), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "author_id", "request_key", name="uq_announcement_request"),
    )
    op.create_index("ix_enterprise_announcement_org", "enterprise_announcement", ["enterprise_id", "status", "id"])


def downgrade():
    raise RuntimeError("企业沟通记录禁止自动降级删除")
