"""Allow a personal-wallet run to reference an approved enterprise global asset."""

import sqlalchemy as sa

from alembic import op

revision = "110_ent_public_personal_run"
down_revision = "109_refund_reviewer_grant"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("personal_agent_run") as batch:
        batch.alter_column("agent_id", existing_type=sa.BigInteger(), nullable=True)
        batch.alter_column("version_id", existing_type=sa.BigInteger(), nullable=True)
        batch.add_column(sa.Column("enterprise_publication_id", sa.BigInteger(), nullable=True))
        batch.create_foreign_key("fk_pa_run_ent_publication", "enterprise_asset_publication",
                                 ["enterprise_publication_id"], ["id"], ondelete="RESTRICT")
        batch.create_check_constraint(
            "ck_pa_run_source",
            "(enterprise_publication_id IS NULL AND agent_id IS NOT NULL AND version_id IS NOT NULL) OR "
            "(enterprise_publication_id IS NOT NULL AND agent_id IS NULL AND version_id IS NULL "
            "AND publication_id IS NULL)",
        )


def downgrade():
    raise RuntimeError("个人运行账单不能自动降级，请按备份恢复流程处理")
