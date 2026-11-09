"""Add tenant-scoped checkpoints and approvals for enterprise Xiaoling.

Revision ID: 082_enterprise_assistant_runtime
Revises: 081_remote_live_recheck
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "082_enterprise_assistant_runtime"
down_revision = "081_remote_live_recheck"
branch_labels = None
depends_on = None

_JSON = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def upgrade():
    op.create_table(
        "enterprise_assistant_runtime_state",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("assistant_run_id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("checkpoint_json", _JSON, nullable=False),
        sa.Column("permission_snapshot_json", _JSON, nullable=False),
        sa.Column("permission_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancel_reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("billing_invocation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["assistant_run_id"], ["enterprise_assistant_run.id"],
            name="fk_ent_assistant_runtime_run", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("assistant_run_id", name="uq_ent_assistant_runtime_run"),
    )
    op.create_index(
        "ix_ent_assistant_runtime_scope", "enterprise_assistant_runtime_state",
        ["enterprise_id", "conversation_id", "status", "id"],
    )
    op.create_index(
        "ix_ent_assistant_runtime_lease", "enterprise_assistant_runtime_state", ["status", "lease_expires_at"],
    )

    op.create_table(
        "enterprise_assistant_approval",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("assistant_run_id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("run_public_id", sa.String(length=80), nullable=False),
        sa.Column("call_id", sa.String(length=120), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("required_role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("arguments_json", _JSON, nullable=False),
        sa.Column("arguments_sha256", sa.String(length=64), nullable=False),
        sa.Column("requested_message_id", sa.BigInteger(), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decision_reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("result_object_id", sa.BigInteger(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["assistant_run_id"], ["enterprise_assistant_run.id"],
            name="fk_ent_assistant_approval_run", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("assistant_run_id", "call_id", name="uq_ent_assistant_approval_call"),
    )
    op.create_index(
        "ix_ent_assistant_approval_queue", "enterprise_assistant_approval", ["enterprise_id", "status", "id"],
    )
    op.create_index(
        "ix_ent_assistant_approval_run", "enterprise_assistant_approval",
        ["enterprise_id", "conversation_id", "assistant_run_id"],
    )


def downgrade():
    raise RuntimeError("企业小菱运行检查点与审批审计不可自动删除；请按备份恢复，禁止单独回退迁移版本")
