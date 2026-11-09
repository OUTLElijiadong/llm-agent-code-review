"""Account-owned personal Agent definitions and immutable versions."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class PersonalAgent(Base, IdMixin, TimestampMixin):
    __tablename__ = "personal_agent"
    __table_args__ = (
        UniqueConstraint("owner_id", "create_key", name="uq_personal_agent_create"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_personal_agent_status"),
        Index("ix_personal_agent_owner", "owner_id", "id"),
    )
    owner_id = Column(BigInteger, nullable=False)
    create_key = Column(String(80), nullable=False)
    create_hash = Column(String(64), nullable=False)
    current_version = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="active")


class PersonalAgentVersion(Base, IdMixin, TimestampMixin):
    __tablename__ = "personal_agent_version"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_number", name="uq_personal_agent_version"),
        UniqueConstraint("agent_id", "request_key", name="uq_personal_agent_version_req"),
    )
    agent_id = Column(BigInteger, ForeignKey("personal_agent.id", ondelete="RESTRICT"), nullable=False)
    version_number = Column(Integer, nullable=False)
    title = Column(String(120), nullable=False)
    prompt = Column(Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False)
    request_key = Column(String(80), nullable=False)
    request_hash = Column(String(64), nullable=False)


class PersonalAgentPublication(Base, IdMixin, TimestampMixin):
    __tablename__ = "personal_agent_publication"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_pa_publication_request"),
        UniqueConstraint("active_version_id", name="uq_pa_publication_active_version"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'withdrawn', 'revoked')",
                        name="ck_pa_publication_status"),
        Index("ix_pa_publication_catalog", "status", "id"),
        Index("ix_pa_publication_version", "agent_id", "version_id", "status"),
    )
    owner_id = Column(BigInteger, nullable=False)
    agent_id = Column(BigInteger, ForeignKey("personal_agent.id", ondelete="RESTRICT"), nullable=False)
    version_id = Column(BigInteger, ForeignKey("personal_agent_version.id", ondelete="RESTRICT"), nullable=False)
    version_number = Column(Integer, nullable=False)
    title = Column(String(120), nullable=False)
    prompt = Column(Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False)
    content_hash = Column(String(64), nullable=False)
    request_key = Column(String(80), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    active_version_id = Column(
        BigInteger,
        Computed("CASE WHEN status IN ('pending', 'approved') THEN version_id ELSE NULL END", persisted=True),
    )
    decided_by = Column(BigInteger, nullable=True)
    decision_reason = Column(String(500), nullable=True)
    revoked_by = Column(BigInteger, nullable=True)
    revocation_reason = Column(String(500), nullable=True)


class PersonalAgentRun(Base, IdMixin, TimestampMixin):
    """One owner-scoped, one-provider-attempt paid run."""

    __tablename__ = "personal_agent_run"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_personal_agent_run_key"),
        UniqueConstraint("reservation_key", name="uq_personal_agent_run_res"),
        CheckConstraint("status IN ('running', 'succeeded', 'awaiting_reconciliation', 'result_unavailable')",
                        name="ck_personal_agent_run_status"),
        CheckConstraint(
            "(enterprise_publication_id IS NULL AND agent_id IS NOT NULL AND version_id IS NOT NULL) OR "
            "(enterprise_publication_id IS NOT NULL AND agent_id IS NULL AND version_id IS NULL "
            "AND publication_id IS NULL)", name="ck_pa_run_source",
        ),
        Index("ix_personal_agent_run_owner", "owner_id", "id"),
    )
    owner_id = Column(BigInteger, nullable=False)
    agent_id = Column(BigInteger, ForeignKey("personal_agent.id", ondelete="RESTRICT"), nullable=True)
    version_id = Column(BigInteger, ForeignKey("personal_agent_version.id", ondelete="RESTRICT"), nullable=True)
    version_number = Column(Integer, nullable=False)
    version_hash = Column(String(64), nullable=False)
    publication_id = Column(BigInteger, ForeignKey("personal_agent_publication.id", ondelete="RESTRICT"), nullable=True)
    enterprise_publication_id = Column(
        BigInteger, ForeignKey("enterprise_asset_publication.id", ondelete="RESTRICT"), nullable=True,
    )
    request_key = Column(String(80), nullable=False)
    request_hash = Column(String(64), nullable=False)
    reservation_key = Column(String(160), nullable=False)
    status = Column(String(32), nullable=False)
    result_text = Column(Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True)
    result_reason = Column(String(32), nullable=True)
