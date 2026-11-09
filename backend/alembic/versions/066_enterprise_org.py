"""新增企业成员、小组和邀请；不修改平台角色及历史项目。

Revision ID: 066_enterprise_org
Revises: 065_review_rule_lang_severity
"""

import sqlalchemy as sa

from alembic import op

revision = "066_enterprise_org"
down_revision = "065_review_rule_lang_severity"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def _timestamps():
    return (
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
    )


def upgrade():
    op.create_table(
        "enterprise",
        _id(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("seat_limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("paid_order_id", sa.String(64), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("paid_order_id", name="uk_enterprise_paid_order"),
    )
    op.create_index("ix_enterprise_owner_id", "enterprise", ["owner_id"])
    op.create_table(
        "enterprise_member",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("default_group_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "user_id", name="uk_enterprise_member"),
    )
    op.create_index("ix_enterprise_member_enterprise_id", "enterprise_member", ["enterprise_id"])
    op.create_index("ix_enterprise_member_user_status", "enterprise_member", ["user_id", "status"])
    op.create_table(
        "enterprise_group",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "name", name="uk_enterprise_group_name"),
        sa.UniqueConstraint("enterprise_id", "id", name="uq_ent_group_scope"),
    )
    op.create_index("ix_enterprise_group_enterprise_id", "enterprise_group", ["enterprise_id"])
    op.create_table(
        "enterprise_group_member",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "group_id", "user_id", name="uk_enterprise_group_member"),
    )
    op.create_index("ix_enterprise_group_member_group_id", "enterprise_group_member", ["group_id"])
    op.create_index(
        "ix_enterprise_group_member_user", "enterprise_group_member", ["enterprise_id", "user_id", "status"]
    )
    op.create_table(
        "enterprise_invitation",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_enterprise_invitation_enterprise_id", "enterprise_invitation", ["enterprise_id"])
    op.create_table(
        "enterprise_audit_event",
        _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("subject_user_id", sa.BigInteger(), nullable=True),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_enterprise_audit_created", "enterprise_audit_event", ["enterprise_id", "create_time"])


def downgrade():
    raise RuntimeError("企业组织含业务数据，不支持破坏性回退；请按备份恢复流程处理")
