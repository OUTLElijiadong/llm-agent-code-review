"""Bind enterprise registered-Agent teams to an optional tenant project.

Revision ID: 097_ent_team_project
Revises: 096_ent_sec_artifacts
"""

import sqlalchemy as sa

from alembic import op

revision = "097_ent_team_project"
down_revision = "096_ent_sec_artifacts"
branch_labels = None
depends_on = None


def _id_type():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.add_column(
        "enterprise_agent_team",
        sa.Column("project_id", _id_type(), nullable=True),
    )
    op.create_index(
        "ix_ent_agent_team_project",
        "enterprise_agent_team",
        ["enterprise_id", "project_id", "id"],
    )
    with op.batch_alter_table("enterprise_agent_team") as batch:
        batch.create_foreign_key(
            "fk_ent_agent_team_project_scope",
            "enterprise_project",
            ["enterprise_id", "project_id"],
            ["enterprise_id", "project_id"],
            ondelete="RESTRICT",
        )


def downgrade():
    with op.batch_alter_table("enterprise_agent_team") as batch:
        batch.drop_constraint("fk_ent_agent_team_project_scope", type_="foreignkey")
    op.drop_index("ix_ent_agent_team_project", table_name="enterprise_agent_team")
    op.drop_column("enterprise_agent_team", "project_id")
