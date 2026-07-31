"""ORM models for reviewer-authored declarative review agents and skills."""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, SmallInteger, String, Text, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class CustomAgent(Base, IdMixin, TimestampMixin):
    """Stable identity for a reviewer-authored review agent."""

    __tablename__ = "custom_agent"
    __table_args__ = (
        UniqueConstraint("code", name="uk_custom_agent_code"),
        Index("ix_custom_agent_owner", "owner_id"),
        Index("ix_custom_agent_enabled", "is_enabled"),
    )

    code = Column(String(80), nullable=False)
    name = Column(String(120), nullable=False)
    description = Column(Text)
    owner_id = Column(BigInteger, nullable=False)
    current_published_version_id = Column(BigInteger)
    status = Column(String(30), nullable=False, default="draft")
    is_enabled = Column(SmallInteger, nullable=False, default=0)


class CustomAgentVersion(Base, IdMixin, TimestampMixin):
    """Immutable-after-submission version of a custom agent."""

    __tablename__ = "custom_agent_version"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_number", name="uk_custom_agent_version"),
        Index("ix_custom_agent_version_status", "status"),
    )

    agent_id = Column(BigInteger, nullable=False)
    version_number = Column(Integer, nullable=False)
    prompt = Column(Text, nullable=False)
    review_focus = Column(Text, nullable=False)
    model_config_json = Column(Text, nullable=False)
    input_schema_json = Column(Text, nullable=False)
    output_schema_json = Column(Text, nullable=False)
    checksum = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False, default="draft")
    original_author_id = Column(BigInteger, nullable=False)
    revised_by = Column(BigInteger)
    revision_note = Column(String(500))
    test_evidence_json = Column(Text)
    tested_checksum = Column(String(64))
    submitted_at = Column(DateTime)


class CustomSkill(Base, IdMixin, TimestampMixin):
    """Stable identity for a reviewer-authored declarative skill."""

    __tablename__ = "custom_skill"
    __table_args__ = (
        UniqueConstraint("code", name="uk_custom_skill_code"),
        Index("ix_custom_skill_owner", "owner_id"),
    )

    code = Column(String(100), nullable=False)
    name = Column(String(120), nullable=False)
    description = Column(Text)
    owner_id = Column(BigInteger, nullable=False)
    current_published_version_id = Column(BigInteger)
    status = Column(String(30), nullable=False, default="draft")


class CustomSkillVersion(Base, IdMixin, TimestampMixin):
    """Immutable-after-submission declarative skill definition."""

    __tablename__ = "custom_skill_version"
    __table_args__ = (
        UniqueConstraint("skill_id", "version_number", name="uk_custom_skill_version"),
        Index("ix_custom_skill_version_status", "status"),
    )

    skill_id = Column(BigInteger, nullable=False)
    version_number = Column(Integer, nullable=False)
    skill_type = Column(String(30), nullable=False)
    definition_json = Column(Text, nullable=False)
    requested_capabilities_json = Column(Text, nullable=False)
    checksum = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False, default="draft")
    original_author_id = Column(BigInteger, nullable=False)
    revised_by = Column(BigInteger)
    revision_note = Column(String(500))
    test_evidence_json = Column(Text)
    tested_checksum = Column(String(64))


class CustomAgentSkillBinding(Base, IdMixin, TimestampMixin):
    """Exact skill-version binding owned by one agent version."""

    __tablename__ = "custom_agent_skill_binding"
    __table_args__ = (
        UniqueConstraint("agent_version_id", "skill_version_id", name="uk_custom_agent_skill_binding"),
        UniqueConstraint("agent_version_id", "position", name="uk_custom_agent_skill_position"),
    )

    agent_version_id = Column(BigInteger, nullable=False)
    skill_version_id = Column(BigInteger, nullable=False)
    position = Column(Integer, nullable=False)
    config_json = Column(Text, nullable=False)


class CustomAgentRelease(Base, IdMixin, TimestampMixin):
    """Atomic, immutable package published after administrator approval."""

    __tablename__ = "custom_agent_release"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uk_custom_agent_release_approval"),
        Index("ix_custom_agent_release_agent", "agent_id", "status"),
    )

    agent_id = Column(BigInteger, nullable=False)
    agent_version_id = Column(BigInteger, nullable=False)
    approval_id = Column(BigInteger)
    previous_release_id = Column(BigInteger)
    rollback_of_release_id = Column(BigInteger)
    package_manifest_json = Column(Text, nullable=False)
    package_checksum = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False, default="published")
    published_by = Column(BigInteger, nullable=False)
    published_at = Column(DateTime, nullable=False)
    disabled_at = Column(DateTime)


class ReviewTaskAgentRelease(Base, IdMixin, TimestampMixin):
    """Version snapshot used by a review task for reproducibility."""

    __tablename__ = "review_task_agent_release"
    __table_args__ = (
        UniqueConstraint("task_id", "release_id", name="uk_review_task_agent_release"),
        Index("ix_review_task_agent_release_task", "task_id"),
    )

    task_id = Column(BigInteger, nullable=False)
    release_id = Column(BigInteger, nullable=False)
    agent_version_id = Column(BigInteger, nullable=False)
    package_manifest_json = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default="snapshotted")
