"""企业知识附件、抽取正文和企业隔离检索切片。

Revision ID: 088_enterprise_kb_attach
Revises: 087_enterprise_remote_project
"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMBLOB, MEDIUMTEXT

from alembic import op

revision = "088_enterprise_kb_attach"
down_revision = "087_enterprise_remote_project"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "enterprise_knowledge_attachment",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=False),
        sa.Column("client_request_id", sa.String(80), nullable=False),
        sa.Column("create_hash", sa.String(64), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=False),
        sa.Column("file_ext", sa.String(10), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("original_data", MEDIUMBLOB().with_variant(sa.LargeBinary(), "sqlite"), nullable=True),
        sa.Column("extracted_text", MEDIUMTEXT().with_variant(sa.Text(), "sqlite"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "enterprise_id", "author_id", "client_request_id",
            name="uq_enterprise_knowledge_attachment_create",
        ),
    )
    op.create_index(
        "ix_enterprise_knowledge_attachment_scope",
        "enterprise_knowledge_attachment",
        ["enterprise_id", "status", "id"],
    )
    op.create_table(
        "enterprise_knowledge_attachment_chunk",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=False),
        sa.Column("embed_model", sa.String(64), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("attachment_id", "seq", name="uq_enterprise_knowledge_attachment_chunk_seq"),
    )
    op.create_index(
        "ix_enterprise_knowledge_attachment_chunk_scope",
        "enterprise_knowledge_attachment_chunk",
        ["enterprise_id", "attachment_id", "id"],
    )


def downgrade():
    # 附件原文、企业材料和审计数据禁止随迁移降级自动删除。
    raise RuntimeError("企业知识附件包含企业资料；请通过备份恢复，禁止将 Alembic 版本号单独回退")
