"""小菱跨 Agent、跨会话通信的持久化账本。"""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class AgentMeshConversation(Base, IdMixin, TimestampMixin):
    """同一账户可寻址的小菱会话。"""

    __tablename__ = "agent_mesh_conversation"
    __table_args__ = (
        UniqueConstraint("user_id", "surface", "session_key", name="uk_agent_mesh_conversation_owner"),
        Index("ix_agent_mesh_conversation_owner_status", "user_id", "status", "last_seen_at"),
    )

    user_id = Column(BigInteger, nullable=False)
    surface = Column(String(24), nullable=False)
    session_key = Column(String(128), nullable=False)
    title = Column(String(200), nullable=False, default="新对话")
    status = Column(String(24), nullable=False, default="active")
    active_run_id = Column(String(80), nullable=True)
    active_run_status = Column(String(32), nullable=True)
    last_seen_at = Column(DateTime, nullable=False)
    last_message_at = Column(DateTime, nullable=True)


class AgentMeshMessage(Base, IdMixin, TimestampMixin):
    """标准化消息信封及其当前投递状态。"""

    __tablename__ = "agent_mesh_message"
    __table_args__ = (
        UniqueConstraint("message_id", name="uk_agent_mesh_message_id"),
        UniqueConstraint("user_id", "idempotency_key", name="uk_agent_mesh_message_idempotency"),
        Index("ix_agent_mesh_message_inbox", "user_id", "send_to", "status", "id"),
        Index("ix_agent_mesh_message_trace", "user_id", "trace_id", "id"),
        Index("ix_agent_mesh_message_correlation", "correlation_id"),
    )

    message_id = Column(String(80), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    schema_version = Column(String(16), nullable=False, default="1.0")
    idempotency_key = Column(String(160), nullable=False)
    trace_id = Column(String(80), nullable=False)
    correlation_id = Column(String(80), nullable=False, default="")
    causation_id = Column(String(80), nullable=False, default="")
    sent_from = Column(String(200), nullable=False)
    send_to = Column(String(200), nullable=False)
    message_type = Column(String(40), nullable=False)
    priority = Column(String(16), nullable=False, default="normal")
    subject = Column(String(240), nullable=False)
    payload_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, default="{}")
    context_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, default="{}")
    artifacts_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, default="[]")
    errors_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, default="[]")
    status = Column(String(24), nullable=False, default="queued")
    requires_ack = Column(SmallInteger, nullable=False, default=1)
    max_attempts = Column(Integer, nullable=False, default=3)
    attempt_count = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    processing_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)


class AgentMeshMessageEvent(Base, IdMixin, TimestampMixin):
    """消息状态机的不可变事件时间线。"""

    __tablename__ = "agent_mesh_message_event"
    __table_args__ = (
        Index("ix_agent_mesh_message_event_message", "message_id", "id"),
        Index("ix_agent_mesh_message_event_trace", "user_id", "trace_id", "id"),
    )

    message_id = Column(String(80), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    trace_id = Column(String(80), nullable=False)
    status = Column(String(24), nullable=False)
    actor_address = Column(String(200), nullable=False)
    detail_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, default="{}")
