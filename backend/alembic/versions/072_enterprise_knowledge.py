"""Enterprise-scoped text knowledge entries.

Revision ID: 072_enterprise_knowledge
Revises: 071_permanent_reviewer
"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from alembic import op

revision = "072_enterprise_knowledge"
down_revision = "071_permanent_reviewer"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "enterprise_knowledge_entry",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=False),
        sa.Column("client_request_id", sa.String(80), nullable=False),
        sa.Column("create_hash", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", MEDIUMTEXT().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("enterprise_id", "author_id", "client_request_id", name="uq_enterprise_knowledge_create"),
    )
    op.create_index("ix_enterprise_knowledge_scope", "enterprise_knowledge_entry", ["enterprise_id", "status", "id"])


def downgrade():
    # 知识正文可能包含企业资料，禁止在迁移降级时静默删除。
    raise RuntimeError("企业知识库是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
