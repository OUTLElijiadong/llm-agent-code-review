"""Tenant-scoped durable teams for registered Agents invoked by enterprise Xiaoling."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin

_JSON = LONGTEXT().with_variant(Text, "sqlite")


class EnterpriseAgentTeam(Base, IdMixin, TimestampMixin):
    """A recoverable Agent team owned by one enterprise conversation and payer."""

    __tablename__ = "enterprise_agent_team"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", "source_tool_call_id", name="uq_ent_agent_team_tool_call"),
        UniqueConstraint("trace_id", name="uq_ent_agent_team_trace"),
        UniqueConstraint("request_key_prefix", name="uq_ent_agent_team_request_prefix"),
        UniqueConstraint("id", "enterprise_id", "conversation_id", name="uq_ent_agent_team_scope_ref"),
        UniqueConstraint("id", "enterprise_id", "conversation_id", "group_id",
                         name="uq_ent_agent_team_scope_group"),
        CheckConstraint(
            "payer_owner_type = 'enterprise' AND payer_owner_id = enterprise_id",
            name="ck_ent_agent_team_enterprise_payer",
        ),
        Index("ix_ent_agent_team_scope_status", "enterprise_id", "conversation_id", "status", "id"),
        Index("ix_ent_agent_team_group_status", "enterprise_id", "group_id", "status", "id"),
        Index("ix_ent_agent_team_initiator", "enterprise_id", "initiator_user_id", "id"),
        Index("ix_ent_agent_team_lease", "status", "lease_expires_at"),
        ForeignKeyConstraint(
            ["assistant_run_id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id"],
            ["enterprise_assistant_run.id", "enterprise_assistant_run.enterprise_id",
             "enterprise_assistant_run.conversation_id", "enterprise_assistant_run.initiator_user_id",
             "enterprise_assistant_run.source_message_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_parent_scope",
        ),
        ForeignKeyConstraint(
            ["assistant_run_id", "enterprise_id", "conversation_id", "group_id", "initiator_user_id",
             "source_message_id"],
            ["enterprise_assistant_run.id", "enterprise_assistant_run.enterprise_id",
             "enterprise_assistant_run.conversation_id", "enterprise_assistant_run.group_id",
             "enterprise_assistant_run.initiator_user_id", "enterprise_assistant_run.source_message_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_parent_group_scope",
        ),
        ForeignKeyConstraint(
            ["source_message_id", "enterprise_id", "conversation_id", "initiator_user_id"],
            ["enterprise_chat_message.id", "enterprise_chat_message.enterprise_id",
             "enterprise_chat_message.conversation_id", "enterprise_chat_message.sender_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_message_scope",
        ),
        ForeignKeyConstraint(
            ["enterprise_id", "conversation_id", "group_id"],
            ["enterprise_conversation.enterprise_id", "enterprise_conversation.id",
             "enterprise_conversation.group_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_conversation_group",
        ),
        ForeignKeyConstraint(
            ["enterprise_id", "conversation_id"],
            ["enterprise_conversation.enterprise_id", "enterprise_conversation.id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_conversation_scope",
        ),
        ForeignKeyConstraint(
            ["enterprise_id", "project_id"],
            ["enterprise_project.enterprise_id", "enterprise_project.project_id"],
            ondelete="RESTRICT", name="fk_ent_agent_team_project_scope",
        ),
        Index("ix_ent_agent_team_project", "enterprise_id", "project_id", "id"),
    )

    assistant_run_id = Column(BigInteger, nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    project_id = Column(BigInteger, nullable=True)
    group_id = Column(BigInteger, nullable=True)
    initiator_user_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    source_tool_call_id = Column(String(120), nullable=False)
    title = Column(String(200), nullable=False)
    objective = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="queued", server_default="queued")
    payer_owner_type = Column(String(24), nullable=False, default="enterprise", server_default="enterprise")
    payer_owner_id = Column(BigInteger, nullable=False)
    trace_id = Column(String(80), nullable=False)
    request_key_prefix = Column(String(160), nullable=False)
    parent_lease_generation = Column(Integer, nullable=False, default=0, server_default="0")
    lease_generation = Column(Integer, nullable=False, default=0, server_default="0")
    lease_expires_at = Column(DateTime, nullable=True)
    deadline_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    plan_sha256 = Column(String(64), nullable=False)
    plan_snapshot_json = Column(_JSON, nullable=False, default="{}")
    permission_snapshot_sha256 = Column(String(64), nullable=False)
    permission_snapshot_json = Column(_JSON, nullable=False, default="{}")
    result_json = Column(_JSON, nullable=False, default="{}")
    error_json = Column(_JSON, nullable=False, default="{}")
    stop_requested = Column(Boolean, nullable=False, default=False, server_default="0")
    cancel_reason = Column(String(500), nullable=False, default="", server_default="")


class EnterpriseAgentTeamMember(Base, IdMixin, TimestampMixin):
    """A fixed registered-Agent or published-asset snapshot within a team."""

    __tablename__ = "enterprise_agent_team_member"
    __table_args__ = (
        UniqueConstraint("team_id", "member_key", name="uq_ent_agent_team_member_key"),
        UniqueConstraint("id", "team_id", name="uq_ent_agent_team_member_scope"),
        CheckConstraint("kind IN ('registered', 'published_asset')", name="ck_ent_agent_team_member_kind"),
        CheckConstraint("role IN ('worker', 'verifier', 'summarizer')", name="ck_ent_agent_team_member_role"),
        Index("ix_ent_agent_team_member_status", "team_id", "status", "id"),
    )

    team_id = Column(
        BigInteger, ForeignKey("enterprise_agent_team.id", ondelete="RESTRICT"), nullable=False,
    )
    member_key = Column(String(80), nullable=False)
    display_name = Column(String(200), nullable=False)
    address = Column(String(200), nullable=False)
    kind = Column(String(24), nullable=False)
    role = Column(String(24), nullable=False, default="worker", server_default="worker")
    publication_id = Column(BigInteger, nullable=True)
    publication_version_id = Column(BigInteger, nullable=True)
    registered_agent_version = Column(String(80), nullable=True)
    snapshot_sha256 = Column(String(64), nullable=False)
    capabilities_json = Column(_JSON, nullable=False, default="{}")
    status = Column(String(24), nullable=False, default="created", server_default="created")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class EnterpriseAgentTeamTask(Base, IdMixin, TimestampMixin):
    """One leased, auditable task in an enterprise Agent team's work graph."""

    __tablename__ = "enterprise_agent_team_task"
    __table_args__ = (
        UniqueConstraint("team_id", "task_key", name="uq_ent_agent_team_task_key"),
        UniqueConstraint("id", "team_id", name="uq_ent_agent_team_task_scope"),
        ForeignKeyConstraint(
            ["member_id", "team_id"],
            ["enterprise_agent_team_member.id", "enterprise_agent_team_member.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_task_member_scope",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND lease_generation >= 0",
            name="ck_ent_agent_team_task_attempts",
        ),
        Index("ix_ent_agent_team_task_queue", "team_id", "status", "priority", "id"),
        Index("ix_ent_agent_team_task_lease", "status", "lease_expires_at"),
    )

    team_id = Column(
        BigInteger, ForeignKey("enterprise_agent_team.id", ondelete="RESTRICT"), nullable=False,
    )
    member_id = Column(BigInteger, nullable=False)
    task_key = Column(String(80), nullable=False)
    title = Column(String(240), nullable=False)
    instructions = Column(Text, nullable=False)
    dependency_keys_json = Column(_JSON, nullable=False, default="[]")
    input_json = Column(_JSON, nullable=False, default="{}")
    status = Column(String(32), nullable=False, default="queued", server_default="queued")
    priority = Column(Integer, nullable=False, default=0, server_default="0")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=3, server_default="3")
    lease_generation = Column(Integer, nullable=False, default=0, server_default="0")
    lease_token = Column(String(80), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)
    approval_sha256 = Column(String(64), nullable=True)
    approved_by_user_id = Column(BigInteger, nullable=True)
    result_json = Column(_JSON, nullable=False, default="{}")
    artifacts_json = Column(_JSON, nullable=False, default="[]")
    errors_json = Column(_JSON, nullable=False, default="[]")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class EnterpriseAgentTeamApproval(Base, IdMixin, TimestampMixin):
    """Human decision bound to one immutable enterprise team/task plan."""

    __tablename__ = "enterprise_agent_team_approval"
    __table_args__ = (
        UniqueConstraint("team_id", "approval_key", name="uq_ent_agent_team_approval_key"),
        ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id"],
            ondelete="RESTRICT", name="fk_ent_agent_approval_team_scope",
        ),
        ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id", "group_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id", "enterprise_agent_team.group_id"],
            ondelete="RESTRICT", name="fk_ent_agent_approval_team_group",
        ),
        ForeignKeyConstraint(
            ["task_id", "team_id"],
            ["enterprise_agent_team_task.id", "enterprise_agent_team_task.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_approval_task_scope",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name="ck_ent_agent_team_approval_status",
        ),
        Index("ix_ent_agent_team_approval_queue", "enterprise_id", "status", "id"),
        Index("ix_ent_agent_team_approval_task", "team_id", "task_id", "status"),
    )

    team_id = Column(BigInteger, nullable=False)
    task_id = Column(BigInteger, nullable=True)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=True)
    initiator_user_id = Column(BigInteger, nullable=False)
    approval_key = Column(String(120), nullable=False)
    plan_sha256 = Column(String(64), nullable=False)
    required_permission_code = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="pending", server_default="pending")
    plan_json = Column(_JSON, nullable=False, default="{}")
    requested_by_user_id = Column(BigInteger, nullable=False)
    decided_by_user_id = Column(BigInteger, nullable=True)
    decision_reason = Column(String(500), nullable=False, default="", server_default="")
    decided_at = Column(DateTime, nullable=True)


class EnterpriseAgentTeamEvent(Base, IdMixin, TimestampMixin):
    """Append-only timeline entries for team, approval, lease and billing state."""

    __tablename__ = "enterprise_agent_team_event"
    __table_args__ = (
        Index("ix_ent_agent_team_event_team", "team_id", "id"),
        Index("ix_ent_agent_team_event_scope", "enterprise_id", "conversation_id", "id"),
        Index("ix_ent_agent_team_event_task", "task_id", "id"),
        ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id"],
            ondelete="RESTRICT", name="fk_ent_agent_event_team_scope",
        ),
        ForeignKeyConstraint(
            ["task_id", "team_id"],
            ["enterprise_agent_team_task.id", "enterprise_agent_team_task.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_event_task_scope",
        ),
        ForeignKeyConstraint(
            ["member_id", "team_id"],
            ["enterprise_agent_team_member.id", "enterprise_agent_team_member.team_id"],
            ondelete="RESTRICT", name="fk_ent_agent_event_member_scope",
        ),
    )

    team_id = Column(BigInteger, nullable=False)
    task_id = Column(BigInteger, nullable=True)
    member_id = Column(BigInteger, nullable=True)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    actor_user_id = Column(BigInteger, nullable=True)
    correlation_id = Column(String(120), nullable=False, default="", server_default="")
    event_type = Column(String(48), nullable=False)
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=True)
    actor_address = Column(String(200), nullable=False)
    detail_json = Column(_JSON, nullable=False, default="{}")
