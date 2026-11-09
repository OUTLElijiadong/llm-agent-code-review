"""企业员工会话。与按账号隔离的小菱 Agent 会话保持独立。"""

from sqlalchemy import (
    BigInteger,
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

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EnterpriseConversation(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_conversation"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "conversation_key", name="uq_enterprise_conversation_key"),
        UniqueConstraint("enterprise_id", "id", name="uq_ent_conversation_scope"),
        UniqueConstraint("enterprise_id", "id", "group_id", name="uq_ent_conversation_scope_group"),
        ForeignKeyConstraint(
            ["enterprise_id", "group_id"],
            ["enterprise_group.enterprise_id", "enterprise_group.id"],
            ondelete="RESTRICT", name="fk_ent_conversation_group_scope",
        ),
        Index("ix_enterprise_conversation_org", "enterprise_id", "id"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    kind = Column(String(16), nullable=False)  # company / group / direct
    conversation_key = Column(String(100), nullable=False)
    group_id = Column(BigInteger, nullable=True)
    participant_low_id = Column(BigInteger, nullable=True)
    participant_high_id = Column(BigInteger, nullable=True)
    created_by = Column(BigInteger, nullable=False)
    proactive_last_event_at = Column(DateTime, nullable=True)


class EnterpriseChatMessage(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_chat_message"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", name="uq_enterprise_chat_assistant_run"),
        UniqueConstraint("id", "enterprise_id", "conversation_id", "sender_id",
                         name="uq_ent_chat_message_team_source"),
        UniqueConstraint("conversation_id", "sender_id", "client_message_id", name="uq_enterprise_chat_message_client"),
        Index("ix_enterprise_chat_message_conversation", "enterprise_id", "conversation_id", "id"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    sender_id = Column(BigInteger, nullable=True)
    sender_type = Column(String(16), nullable=False, default="human", server_default="human")
    assistant_run_id = Column(BigInteger, ForeignKey("enterprise_assistant_run.id"), nullable=True)
    client_message_id = Column(String(80), nullable=False)
    content = Column(Text, nullable=False)


class EnterpriseAssistantRun(Base, IdMixin, TimestampMixin):
    """企业会话小菱问答运行；发起人和企业身份分开记录。"""

    __tablename__ = "enterprise_assistant_run"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_enterprise_assistant_public_id"),
        UniqueConstraint("enterprise_id", "conversation_id", "source_message_id",
                         name="uq_enterprise_assistant_source"),
        UniqueConstraint("request_key", name="uq_enterprise_assistant_request"),
        UniqueConstraint("id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id",
                         name="uq_ent_assistant_run_team_source"),
        UniqueConstraint("id", "enterprise_id", "conversation_id", "group_id", "initiator_user_id",
                         "source_message_id", name="uq_ent_assistant_run_team_group"),
        Index("ix_enterprise_assistant_scope_status", "enterprise_id", "conversation_id", "status", "id"),
    )

    public_id = Column(String(80), nullable=False, unique=True)
    request_key = Column(String(120), nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=True)
    initiator_user_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    billing_group_id = Column(BigInteger, nullable=True)
    billing_attribution_version = Column(Integer, nullable=False, default=0, server_default="0")
    billing_membership_version = Column(BigInteger, nullable=True)
    billing_user_token_version = Column(Integer, nullable=True)
    status = Column(String(24), nullable=False, default="queued", server_default="queued")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    model_name = Column(String(100), nullable=True)
    prompt_sha256 = Column(String(64), nullable=False)
    trigger_kind = Column(String(16), nullable=False, default="manual", server_default="manual")
    proactive_version = Column(Integer, nullable=True)
    response_text = Column(Text, nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    output_tokens = Column(BigInteger, nullable=True)
    error_code = Column(String(64), nullable=True)


class EnterpriseAssistantProactiveEvent(Base, IdMixin, TimestampMixin):
    """A human message and its durable, at-most-once proactive trigger."""

    __tablename__ = "enterprise_assistant_proactive_event"
    __table_args__ = (
        UniqueConstraint("source_message_id", name="uq_ent_proactive_source"),
        ForeignKeyConstraint(
            ["source_message_id", "enterprise_id", "conversation_id", "actor_user_id"],
            ["enterprise_chat_message.id", "enterprise_chat_message.enterprise_id",
             "enterprise_chat_message.conversation_id", "enterprise_chat_message.sender_id"],
            ondelete="RESTRICT", name="fk_ent_proactive_source_scope",
        ),
        Index("ix_ent_proactive_pending", "status", "id"),
        Index("ix_ent_proactive_scope_time", "enterprise_id", "conversation_id", "create_time"),
        CheckConstraint("status IN ('pending', 'queued', 'skipped')", name="ck_ent_proactive_status"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    actor_user_id = Column(BigInteger, nullable=False)
    policy_version = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    run_id = Column(BigInteger, ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"), nullable=True)
