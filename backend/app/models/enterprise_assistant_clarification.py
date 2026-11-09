"""Auditable clarification records; not an authorization grant by themselves."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
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


class EnterpriseAssistantClarification(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_assistant_clarification"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", "phase", "call_id", "version", name="uq_ent_clarify_call_version"),
        UniqueConstraint("id", "enterprise_id", "conversation_id", name="uq_ent_clarify_scope"),
        ForeignKeyConstraint(
            ["assistant_run_id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id"],
            ["enterprise_assistant_run.id", "enterprise_assistant_run.enterprise_id",
             "enterprise_assistant_run.conversation_id", "enterprise_assistant_run.initiator_user_id",
             "enterprise_assistant_run.source_message_id"],
            name="fk_ent_clarify_run_scope", ondelete="RESTRICT",
        ),
        CheckConstraint("phase IN ('model_send', 'tool')", name="ck_ent_clarify_phase"),
        CheckConstraint("status IN ('pending', 'cancelled', 'superseded')", name="ck_ent_clarify_status"),
        Index("ix_ent_clarify_scope_status", "enterprise_id", "conversation_id", "status", "id"),
    )

    assistant_run_id = Column(BigInteger, nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    initiator_user_id = Column(BigInteger, nullable=False)
    phase = Column(String(24), nullable=False)
    call_id = Column(String(120), nullable=False)
    action = Column(String(80), nullable=False)
    target_json = Column(_JSON, nullable=False)
    arguments_json = Column(_JSON, nullable=False)
    arguments_sha256 = Column(String(64), nullable=False)
    authority_sha256 = Column(String(64), nullable=False)
    authority_version_sha256 = Column(String(64), nullable=True)
    version = Column(Integer, nullable=False)
    status = Column(String(24), nullable=False, default="pending", server_default="pending")
    expires_at = Column(DateTime, nullable=False)


class EnterpriseAssistantClarificationDecision(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_assistant_clarification_decision"
    __table_args__ = (
        UniqueConstraint("request_id", "source_message_id", name="uq_ent_clarify_source_decision"),
        ForeignKeyConstraint(
            ["request_id", "enterprise_id", "conversation_id"],
            ["enterprise_assistant_clarification.id", "enterprise_assistant_clarification.enterprise_id",
             "enterprise_assistant_clarification.conversation_id"],
            name="fk_ent_clarify_decision_scope", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_message_id", "enterprise_id", "conversation_id", "author_user_id"],
            ["enterprise_chat_message.id", "enterprise_chat_message.enterprise_id",
             "enterprise_chat_message.conversation_id", "enterprise_chat_message.sender_id"],
            name="fk_ent_clarify_decision_message", ondelete="RESTRICT",
        ),
        CheckConstraint("decision IN ('pending', 'allow', 'deny', 'unrelated')", name="ck_ent_clarify_decision"),
        CheckConstraint(
            "(decision = 'pending' AND decided_by IS NULL AND decided_at IS NULL AND author_rank IS NULL "
            "AND decider_rank IS NULL) OR (decision <> 'pending' AND decided_by IS NOT NULL "
            "AND decided_at IS NOT NULL AND author_rank IS NOT NULL AND decider_rank IS NOT NULL)",
            name="ck_ent_clarify_decision_evidence",
        ),
        Index("ix_ent_clarify_decision_author", "author_user_id", "request_id"),
    )

    request_id = Column(BigInteger, nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    author_user_id = Column(BigInteger, nullable=False)
    decided_by = Column(BigInteger, nullable=True)
    author_rank = Column(Integer, nullable=True)
    decider_rank = Column(Integer, nullable=True)
    decider_version_sha256 = Column(String(64), nullable=True)
    decision = Column(String(16), nullable=False, default="pending", server_default="pending")
    decided_at = Column(DateTime, nullable=True)


class EnterpriseAssistantClarificationReview(Base, IdMixin, TimestampMixin):
    """Append-only higher-authority review; original decision stays intact."""
    __tablename__ = "enterprise_assistant_clarification_review"
    __table_args__ = (
        ForeignKeyConstraint(
            ["request_id", "enterprise_id", "conversation_id"],
            [
                "enterprise_assistant_clarification.id",
                "enterprise_assistant_clarification.enterprise_id",
                "enterprise_assistant_clarification.conversation_id",
            ],
            name="fk_ent_clarify_review_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["request_id", "source_message_id"],
                             ["enterprise_assistant_clarification_decision.request_id",
                              "enterprise_assistant_clarification_decision.source_message_id"],
                             name="fk_ent_clarify_review_decision", ondelete="RESTRICT"),
        CheckConstraint("decision IN ('allow', 'deny', 'unrelated')", name="ck_ent_clarify_review_decision"),
        CheckConstraint("review_kind IN ('higher_review', 'author_revision')", name="ck_ent_clarify_review_kind"),
    )
    request_id = Column(BigInteger, nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=False)
    decided_by = Column(BigInteger, nullable=False)
    review_kind = Column(String(24), nullable=False)
    decider_rank = Column(Integer, nullable=False)
    decider_version_sha256 = Column(String(64), nullable=False)
    decision = Column(String(16), nullable=False)
    decided_at = Column(DateTime, nullable=False)
