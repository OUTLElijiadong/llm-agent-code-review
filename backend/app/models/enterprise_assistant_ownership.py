"""Detached, durable ownership claims for a future fenced assistant runtime.

These rows do not themselves authorize an external action or enable metering.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql

from app.core.database import Base
from app.models.base import IdMixin, _utcnow

_DIGEST = LargeBinary(32).with_variant(mysql.VARBINARY(32), "mysql")


class EnterpriseAssistantRunOwner(Base, IdMixin):
    __tablename__ = "enterprise_assistant_run_owner"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", "generation", name="uq_ent_run_owner_generation"),
        CheckConstraint("generation > 0", name="ck_ent_run_owner_generation"),
        CheckConstraint("length(owner_digest) = 32", name="ck_ent_run_owner_digest"),
    )

    assistant_run_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"), nullable=False
    )
    generation = Column(Integer, nullable=False)
    owner_digest = Column(_DIGEST, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class EnterpriseAssistantToolOwner(Base, IdMixin):
    __tablename__ = "enterprise_assistant_tool_owner"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", "call_digest", name="uq_ent_tool_owner_call"),
        UniqueConstraint("request_digest", name="uq_ent_tool_owner_request"),
        CheckConstraint("generation > 0", name="ck_ent_tool_owner_generation"),
        CheckConstraint("tool_name IN ('search', 'fetch')", name="ck_ent_tool_owner_kind"),
        CheckConstraint(
            "length(owner_digest) = 32 AND length(call_digest) = 32 AND "
            "length(request_digest) = 32 AND length(arguments_digest) = 32",
            name="ck_ent_tool_owner_digests",
        ),
    )

    assistant_run_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"), nullable=False
    )
    run_owner_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run_owner.id", ondelete="RESTRICT"), nullable=False
    )
    generation = Column(Integer, nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    initiator_user_id = Column(BigInteger, nullable=False)
    billing_group_id = Column(BigInteger, nullable=True)
    wallet_id = Column(BigInteger, ForeignKey("billing_wallet.id", ondelete="RESTRICT"), nullable=False)
    tool_name = Column(String(16), nullable=False)
    call_digest = Column(_DIGEST, nullable=False)
    call_bytes = Column(LargeBinary(480), nullable=False)
    request_digest = Column(_DIGEST, nullable=False)
    request_bytes = Column(LargeBinary(640), nullable=False)
    arguments_digest = Column(_DIGEST, nullable=False)
    owner_digest = Column(_DIGEST, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class EnterpriseAssistantModelAttemptOwner(Base, IdMixin):
    """Detached, one-shot association; never by itself authorizes billing or I/O."""

    __tablename__ = "enterprise_assistant_model_attempt_owner"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", "round_digest", name="uq_ent_model_owner_round"),
        UniqueConstraint("reservation_id", name="uq_ent_model_owner_reservation"),
        UniqueConstraint("request_digest", name="uq_ent_model_owner_request"),
        CheckConstraint("generation > 0", name="ck_ent_model_owner_generation"),
        CheckConstraint(
            "(usage_kind = 'member' AND payer_user_id IS NOT NULL AND payer_user_id = initiator_user_id) "
            "OR (usage_kind = 'enterprise_auto' AND payer_user_id IS NULL)",
            name="ck_ent_model_owner_payer",
        ),
        CheckConstraint(
            "length(owner_digest) = 32 AND length(round_digest) = 32 AND "
            "length(request_digest) = 32 AND length(payload_digest) = 32 AND length(billing_digest) = 32",
            name="ck_ent_model_owner_digests",
        ),
    )

    assistant_run_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"), nullable=False
    )
    run_owner_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run_owner.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_id = Column(BigInteger, ForeignKey("billing_reservation.id", ondelete="RESTRICT"), nullable=False)
    generation = Column(Integer, nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    initiator_user_id = Column(BigInteger, nullable=False)
    billing_group_id = Column(BigInteger, nullable=True)
    wallet_id = Column(BigInteger, ForeignKey("billing_wallet.id", ondelete="RESTRICT"), nullable=False)
    payer_user_id = Column(BigInteger, nullable=True)
    usage_kind = Column(String(20), nullable=False)
    model_name = Column(String(80), nullable=False)
    round_digest = Column(_DIGEST, nullable=False)
    round_bytes = Column(LargeBinary(480), nullable=False)
    request_digest = Column(_DIGEST, nullable=False)
    request_bytes = Column(LargeBinary(640), nullable=False)
    payload_digest = Column(_DIGEST, nullable=False)
    billing_digest = Column(_DIGEST, nullable=False)
    owner_digest = Column(_DIGEST, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
