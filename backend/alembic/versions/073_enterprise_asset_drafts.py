"""Isolated enterprise Agent/Skill drafts; not published or executable.

Revision ID: 073_enterprise_asset_drafts
Revises: 072_enterprise_knowledge
"""
import sqlalchemy as sa

from alembic import op

revision = "073_enterprise_asset_drafts"
down_revision = "072_enterprise_knowledge"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def _timestamps():
    return (sa.Column("create_time", sa.DateTime(), nullable=False),
            sa.Column("update_time", sa.DateTime(), nullable=False))


def upgrade():
    op.create_table(
        "enterprise_asset_draft", _id(),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), *_timestamps(),
        sa.UniqueConstraint("enterprise_id", "creator_id", "request_key", name="uq_ent_asset_create"),
    )
    op.create_index("ix_ent_asset_owner", "enterprise_asset_draft", ["enterprise_id", "creator_id", "status", "id"])
    op.create_table(
        "enterprise_asset_draft_version", _id(),
        sa.Column("draft_id", sa.BigInteger(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False), *_timestamps(),
        sa.UniqueConstraint("draft_id", "version_number", name="uq_ent_asset_version"),
        sa.UniqueConstraint("draft_id", "request_key", name="uq_ent_asset_version_req"),
    )
    op.create_index("ix_enterprise_asset_draft_version_draft_id", "enterprise_asset_draft_version", ["draft_id"])


def downgrade():
    # 草稿可能包含企业资料，不能静默删除。
    raise RuntimeError("企业 Agent 与 Skill 草稿是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
