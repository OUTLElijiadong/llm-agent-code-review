"""Tenant-scoped checkpoints and approvals for enterprise Xiaoling runs."""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin

_JSON = LONGTEXT().with_variant(Text, "sqlite")


class EnterpriseAssistantRuntimeState(Base, IdMixin, TimestampMixin):
    """Durable Responses checkpoint fenced by enterprise run and lease generation."""

    __tablename__ = "enterprise_assistant_runtime_state"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", name="uq_ent_assistant_runtime_run"),
        Index("ix_ent_assistant_runtime_scope", "enterprise_id", "conversation_id", "status", "id"),
        Index("ix_ent_assistant_runtime_lease", "status", "lease_expires_at"),
    )

    assistant_run_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"), nullable=False,
    )
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=True)
    initiator_user_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    status = Column(String(32), nullable=False, default="queued", server_default="queued")
    checkpoint_json = Column(_JSON, nullable=False, default="{}")
    permission_snapshot_json = Column(_JSON, nullable=False, default="{}")
    permission_snapshot_sha256 = Column(String(64), nullable=False)
    stop_requested = Column(Boolean, nullable=False, default=False, server_default="0")
    cancel_reason = Column(String(500), nullable=False, default="", server_default="")
    lease_generation = Column(Integer, nullable=False, default=0, server_default="0")
    lease_expires_at = Column(DateTime, nullable=True)
    billing_invocation_count = Column(Integer, nullable=False, default=0, server_default="0")
    proactive_attempt_started_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    error_code = Column(String(64), nullable=True)


class EnterpriseAssistantApproval(Base, IdMixin, TimestampMixin):
    """Independent enterprise approval bound to one run, call and exact payload."""

    __tablename__ = "enterprise_assistant_approval"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", "call_id", name="uq_ent_assistant_approval_call"),
        Index("ix_ent_assistant_approval_queue", "enterprise_id", "status", "id"),
        Index("ix_ent_assistant_approval_run", "enterprise_id", "conversation_id", "assistant_run_id"),
    )

    assistant_run_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"), nullable=False,
    )
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=True)
    initiator_user_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    run_public_id = Column(String(80), nullable=False)
    call_id = Column(String(120), nullable=False)
    tool_name = Column(String(80), nullable=False)
    required_role = Column(String(32), nullable=False)
    status = Column(String(24), nullable=False, default="pending", server_default="pending")
    arguments_json = Column(_JSON, nullable=False)
    arguments_sha256 = Column(String(64), nullable=False)
    requested_message_id = Column(BigInteger, nullable=True)
    decided_by = Column(BigInteger, nullable=True)
    decision_reason = Column(String(500), nullable=False, default="", server_default="")
    decided_at = Column(DateTime, nullable=True)
    result_object_id = Column(BigInteger, nullable=True)
