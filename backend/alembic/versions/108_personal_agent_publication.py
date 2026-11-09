"""Add independently approved personal Agent publication and its caller runs."""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from alembic import op

revision = "108_personal_agent_publication"
down_revision = "107_rate_review_authority"
branch_labels = None
depends_on = None


def upgrade():
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "personal_agent_publication",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_id", sa.BigInteger(), sa.ForeignKey("personal_agent.id", ondelete="RESTRICT"), nullable=False),
        sa.Column(
            "version_id",
            sa.BigInteger(),
            sa.ForeignKey("personal_agent_version.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("prompt", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active_version_id", sa.BigInteger(),
                  sa.Computed("CASE WHEN status IN ('pending', 'approved') THEN version_id ELSE NULL END",
                              persisted=True)),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column("revocation_reason", sa.String(500), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_id", "request_key", name="uq_pa_publication_request"),
        sa.UniqueConstraint("active_version_id", name="uq_pa_publication_active_version"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn', 'revoked')", name="ck_pa_publication_status"
        ),
    )
    op.create_index("ix_pa_publication_catalog", "personal_agent_publication", ["status", "id"])
    op.create_index("ix_pa_publication_version", "personal_agent_publication", ["agent_id", "version_id", "status"])
    with op.batch_alter_table("personal_agent_run") as batch:
        batch.add_column(sa.Column("publication_id", identifier, nullable=True))
        batch.create_foreign_key(
            "fk_pa_run_publication", "personal_agent_publication", ["publication_id"], ["id"], ondelete="RESTRICT"
        )


def downgrade():
    raise RuntimeError("个人 Agent 发布与账单不能自动降级，请按备份恢复流程处理")
