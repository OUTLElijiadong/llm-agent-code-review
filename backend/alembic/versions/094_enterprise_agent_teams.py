"""Create tenant-isolated durable teams for enterprise registered Agents.

Revision ID: 094_enterprise_agent_teams
Revises: 093_billing_provider_identity
"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

from alembic import op

revision = "094_enterprise_agent_teams"
down_revision = "093_billing_provider_identity"
branch_labels = None
depends_on = None


def _id_type():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _json_type():
    return LONGTEXT().with_variant(sa.Text(), "sqlite")


def upgrade():
    op.create_table(
        "enterprise_agent_team",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("assistant_run_id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_tool_call_id", sa.String(120), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("payer_owner_type", sa.String(24), nullable=False, server_default="enterprise"),
        sa.Column("payer_owner_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(80), nullable=False),
        sa.Column("request_key_prefix", sa.String(160), nullable=False),
        sa.Column("parent_lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("plan_snapshot_json", _json_type(), nullable=False),
        sa.Column("permission_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("permission_snapshot_json", _json_type(), nullable=False),
        sa.Column("result_json", _json_type(), nullable=False),
        sa.Column("error_json", _json_type(), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancel_reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "payer_owner_type = 'enterprise' AND payer_owner_id = enterprise_id",
            name="ck_ent_agent_team_enterprise_payer",
        ),
        sa.UniqueConstraint("assistant_run_id", "source_tool_call_id", name="uq_ent_agent_team_tool_call"),
        sa.UniqueConstraint("trace_id", name="uq_ent_agent_team_trace"),
        sa.UniqueConstraint("request_key_prefix", name="uq_ent_agent_team_request_prefix"),
        sa.UniqueConstraint("id", "enterprise_id", "conversation_id", name="uq_ent_agent_team_scope_ref"),
        sa.UniqueConstraint("id", "enterprise_id", "conversation_id", "group_id",
                            name="uq_ent_agent_team_scope_group"),
        sa.ForeignKeyConstraint(
            ["assistant_run_id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id"],
            ["enterprise_assistant_run.id", "enterprise_assistant_run.enterprise_id",
             "enterprise_assistant_run.conversation_id", "enterprise_assistant_run.initiator_user_id",
             "enterprise_assistant_run.source_message_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_parent_scope",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_run_id", "enterprise_id", "conversation_id", "group_id", "initiator_user_id",
             "source_message_id"],
            ["enterprise_assistant_run.id", "enterprise_assistant_run.enterprise_id",
             "enterprise_assistant_run.conversation_id", "enterprise_assistant_run.group_id",
             "enterprise_assistant_run.initiator_user_id", "enterprise_assistant_run.source_message_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_parent_group_scope",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id", "enterprise_id", "conversation_id", "initiator_user_id"],
            ["enterprise_chat_message.id", "enterprise_chat_message.enterprise_id",
             "enterprise_chat_message.conversation_id", "enterprise_chat_message.sender_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_message_scope",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id", "conversation_id", "group_id"],
            ["enterprise_conversation.enterprise_id", "enterprise_conversation.id",
             "enterprise_conversation.group_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_conversation_group",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id", "conversation_id"],
            ["enterprise_conversation.enterprise_id", "enterprise_conversation.id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_conversation_scope",
        ),
    )
    op.create_index(
        "ix_ent_agent_team_scope_status", "enterprise_agent_team",
        ["enterprise_id", "conversation_id", "status", "id"],
    )
    op.create_index(
        "ix_ent_agent_team_group_status", "enterprise_agent_team",
        ["enterprise_id", "group_id", "status", "id"],
    )
    op.create_index(
        "ix_ent_agent_team_initiator", "enterprise_agent_team",
        ["enterprise_id", "initiator_user_id", "id"],
    )
    op.create_index("ix_ent_agent_team_lease", "enterprise_agent_team", ["status", "lease_expires_at"])

    op.create_table(
        "enterprise_agent_team_member",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), sa.ForeignKey(
            "enterprise_agent_team.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("member_key", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("address", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default="worker"),
        sa.Column("publication_id", sa.BigInteger(), nullable=True),
        sa.Column("publication_version_id", sa.BigInteger(), nullable=True),
        sa.Column("registered_agent_version", sa.String(80), nullable=True),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("capabilities_json", _json_type(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="created"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('registered', 'published_asset')", name="ck_ent_agent_team_member_kind"),
        sa.CheckConstraint(
            "role IN ('worker', 'verifier', 'summarizer')", name="ck_ent_agent_team_member_role",
        ),
        sa.UniqueConstraint("team_id", "member_key", name="uq_ent_agent_team_member_key"),
        sa.UniqueConstraint("id", "team_id", name="uq_ent_agent_team_member_scope"),
    )
    op.create_index(
        "ix_ent_agent_team_member_status", "enterprise_agent_team_member", ["team_id", "status", "id"],
    )

    op.create_table(
        "enterprise_agent_team_task",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), sa.ForeignKey(
            "enterprise_agent_team.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("task_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("dependency_keys_json", _json_type(), nullable=False),
        sa.Column("input_json", _json_type(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("approval_sha256", sa.String(64), nullable=True),
        sa.Column("approved_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("result_json", _json_type(), nullable=False),
        sa.Column("artifacts_json", _json_type(), nullable=False),
        sa.Column("errors_json", _json_type(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND lease_generation >= 0",
            name="ck_ent_agent_team_task_attempts",
        ),
        sa.UniqueConstraint("team_id", "task_key", name="uq_ent_agent_team_task_key"),
        sa.UniqueConstraint("id", "team_id", name="uq_ent_agent_team_task_scope"),
        sa.ForeignKeyConstraint(
            ["member_id", "team_id"],
            ["enterprise_agent_team_member.id", "enterprise_agent_team_member.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_task_member_scope",
        ),
    )
    op.create_index(
        "ix_ent_agent_team_task_queue", "enterprise_agent_team_task", ["team_id", "status", "priority", "id"],
    )
    op.create_index(
        "ix_ent_agent_team_task_lease", "enterprise_agent_team_task", ["status", "lease_expires_at"],
    )

    op.create_table(
        "enterprise_agent_team_approval",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("approval_key", sa.String(120), nullable=False),
        sa.Column("plan_sha256", sa.String(64), nullable=False),
        sa.Column("required_permission_code", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("plan_json", _json_type(), nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("decided_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name="ck_ent_agent_team_approval_status",
        ),
        sa.UniqueConstraint("team_id", "approval_key", name="uq_ent_agent_team_approval_key"),
        sa.ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id"],
            ondelete="RESTRICT", name="fk_ent_agent_approval_team_scope",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id", "group_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id", "enterprise_agent_team.group_id"],
            ondelete="RESTRICT", name="fk_ent_agent_approval_team_group",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "team_id"],
            ["enterprise_agent_team_task.id", "enterprise_agent_team_task.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_approval_task_scope",
        ),
    )
    op.create_index(
        "ix_ent_agent_team_approval_queue", "enterprise_agent_team_approval", ["enterprise_id", "status", "id"],
    )
    op.create_index(
        "ix_ent_agent_team_approval_task", "enterprise_agent_team_approval", ["team_id", "task_id", "status"],
    )

    op.create_table(
        "enterprise_agent_team_event",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("member_id", sa.BigInteger(), nullable=True),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("correlation_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=True),
        sa.Column("actor_address", sa.String(200), nullable=False),
        sa.Column("detail_json", _json_type(), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id"],
            ondelete="RESTRICT", name="fk_ent_agent_event_team_scope",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "team_id"],
            ["enterprise_agent_team_task.id", "enterprise_agent_team_task.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_event_task_scope",
        ),
        sa.ForeignKeyConstraint(
            ["member_id", "team_id"],
            ["enterprise_agent_team_member.id", "enterprise_agent_team_member.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_event_member_scope",
        ),
    )
    op.create_index("ix_ent_agent_team_event_team", "enterprise_agent_team_event", ["team_id", "id"])
    op.create_index(
        "ix_ent_agent_team_event_scope", "enterprise_agent_team_event", ["enterprise_id", "conversation_id", "id"],
    )
    op.create_index("ix_ent_agent_team_event_task", "enterprise_agent_team_event", ["task_id", "id"])


def downgrade():
    raise RuntimeError("企业 Agent 团队执行与审批记录属于租户审计依据；请通过备份恢复，禁止自动删除")
