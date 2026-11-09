"""Add immutable project-scoped enterprise security report artifacts.

Revision ID: 096_ent_sec_artifacts
Revises: 095_billing_res_reconcile
"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

from alembic import op

revision = "096_ent_sec_artifacts"
down_revision = "095_billing_res_reconcile"
branch_labels = None
depends_on = None


def _id_type():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _install_immutability_triggers() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER trg_ent_sec_artifact_no_update "
            "BEFORE UPDATE ON enterprise_security_artifact "
            "BEGIN SELECT RAISE(ABORT, 'enterprise security artifacts are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_sec_artifact_no_delete "
            "BEFORE DELETE ON enterprise_security_artifact "
            "BEGIN SELECT RAISE(ABORT, 'enterprise security artifacts are immutable'); END"
        )
        return
    if bind.dialect.name == "mysql":
        op.execute(
            "CREATE TRIGGER trg_ent_sec_artifact_no_update "
            "BEFORE UPDATE ON enterprise_security_artifact FOR EACH ROW "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'enterprise security artifacts are immutable'"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_sec_artifact_no_delete "
            "BEFORE DELETE ON enterprise_security_artifact FOR EACH ROW "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'enterprise security artifacts are immutable'"
        )
        return
    raise RuntimeError(f"unsupported database for immutable artifact triggers: {bind.dialect.name}")


def upgrade():
    with op.batch_alter_table("enterprise_project") as batch:
        batch.create_unique_constraint(
            "uq_enterprise_project_tenant_scope", ["enterprise_id", "project_id"]
        )

    op.create_table(
        "enterprise_security_artifact",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("audit_run_id", sa.String(length=64), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column("coverage_status", sa.String(length=16), nullable=False),
        sa.Column("termination_reason", sa.String(length=64), server_default="", nullable=False),
        sa.Column("summary_json", LONGTEXT().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column("report_json", LONGTEXT().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("task_id", "attempt_no", name="uq_ent_sec_artifact_attempt"),
        sa.UniqueConstraint("audit_run_id", name="uq_ent_sec_artifact_audit_run"),
        sa.UniqueConstraint("id", "enterprise_id", "project_id", name="uq_ent_sec_artifact_scope"),
        sa.CheckConstraint("attempt_no > 0", name="ck_ent_sec_artifact_attempt"),
        sa.CheckConstraint(
            "coverage_status IN ('complete', 'incomplete', 'failed')",
            name="ck_ent_sec_artifact_coverage",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64 AND length(report_sha256) = 64",
            name="ck_ent_sec_artifact_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id"],
            ondelete="RESTRICT", name="fk_ent_sec_artifact_team_scope",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "team_id"],
            ["enterprise_agent_team_task.id", "enterprise_agent_team_task.team_id"],
            ondelete="RESTRICT", name="fk_ent_sec_artifact_task_scope",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id", "project_id"],
            ["enterprise_project.enterprise_id", "enterprise_project.project_id"],
            ondelete="RESTRICT", name="fk_ent_sec_artifact_project_scope",
        ),
    )
    op.create_index(
        "ix_ent_sec_artifact_project", "enterprise_security_artifact", ["enterprise_id", "project_id", "id"]
    )
    op.create_index("ix_ent_sec_artifact_team", "enterprise_security_artifact", ["team_id", "id"])
    _install_immutability_triggers()


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_ent_sec_artifact_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_ent_sec_artifact_no_update")
    elif bind.dialect.name == "mysql":
        op.execute("DROP TRIGGER IF EXISTS trg_ent_sec_artifact_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_ent_sec_artifact_no_update")
    op.drop_index("ix_ent_sec_artifact_team", table_name="enterprise_security_artifact")
    op.drop_index("ix_ent_sec_artifact_project", table_name="enterprise_security_artifact")
    op.drop_table("enterprise_security_artifact")
    with op.batch_alter_table("enterprise_project") as batch:
        batch.drop_constraint("uq_enterprise_project_tenant_scope", type_="unique")
