"""Version-bound enterprise/global Agent and Skill publication approvals.

Revision ID: 084_ent_asset_publications
Revises: 083_manual_refund_review
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "084_ent_asset_publications"
down_revision = "083_manual_refund_review"
branch_labels = None
depends_on = None

_JSON = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def upgrade():
    op.create_table(
        "enterprise_asset_publication",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("draft_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("definition_json", _JSON, nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_key", sa.String(length=80), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revocation_reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("withdrawn_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["enterprise_asset_draft.id"],
            name="fk_ent_asset_pub_draft", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], ["enterprise_asset_draft_version.id"],
            name="fk_ent_asset_pub_version", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "enterprise_id", "requested_by", "request_key", name="uq_ent_asset_pub_request",
        ),
    )
    op.create_index(
        "ix_ent_asset_pub_queue", "enterprise_asset_publication", ["enterprise_id", "scope", "status", "id"],
    )
    op.create_index(
        "ix_ent_asset_pub_draft", "enterprise_asset_publication", ["draft_id", "scope", "status", "id"],
    )


def downgrade():
    raise RuntimeError("已审批资产快照与审计记录不可静默删除；请通过备份恢复，禁止单独回退迁移")
