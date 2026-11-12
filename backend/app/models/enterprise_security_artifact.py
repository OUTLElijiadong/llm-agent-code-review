"""Immutable, tenant-scoped artifacts from enterprise security scans."""

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
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin

_JSON = LONGTEXT().with_variant(Text, "sqlite")


class EnterpriseSecurityArtifact(Base, IdMixin):
    """One append-only report per enterprise team task attempt.

    Report bodies do not belong in the task result JSON: the latter is deliberately
    small and mutable while tasks transition through their lease state machine.
    Database triggers installed by migration 096 make both update and delete fail.
    """

    __tablename__ = "enterprise_security_artifact"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no", name="uq_ent_sec_artifact_attempt"),
        UniqueConstraint("audit_run_id", name="uq_ent_sec_artifact_audit_run"),
        UniqueConstraint("id", "enterprise_id", "project_id", name="uq_ent_sec_artifact_scope"),
        CheckConstraint("attempt_no > 0", name="ck_ent_sec_artifact_attempt"),
        CheckConstraint(
            "coverage_status IN ('complete', 'incomplete', 'failed')",
            name="ck_ent_sec_artifact_coverage",
        ),
        CheckConstraint(
            "length(source_sha256) = 64 AND length(report_sha256) = 64",
            name="ck_ent_sec_artifact_hash_length",
        ),
        Index("ix_ent_sec_artifact_project", "enterprise_id", "project_id", "id"),
        Index("ix_ent_sec_artifact_team", "team_id", "id"),
        ForeignKeyConstraint(
            ["team_id", "enterprise_id", "conversation_id"],
            ["enterprise_agent_team.id", "enterprise_agent_team.enterprise_id",
             "enterprise_agent_team.conversation_id"],
            ondelete="RESTRICT",
            name="fk_ent_sec_artifact_team_scope",
        ),
        ForeignKeyConstraint(
            ["task_id", "team_id"],
            ["enterprise_agent_team_task.id", "enterprise_agent_team_task.team_id"],
            ondelete="RESTRICT",
            name="fk_ent_sec_artifact_task_scope",
        ),
        ForeignKeyConstraint(
            ["enterprise_id", "project_id"],
            ["enterprise_project.enterprise_id", "enterprise_project.project_id"],
            ondelete="RESTRICT",
            name="fk_ent_sec_artifact_project_scope",
        ),
    )

    team_id = Column(BigInteger, nullable=False)
    task_id = Column(BigInteger, nullable=False)
    enterprise_id = Column(BigInteger, nullable=False)
    project_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    attempt_no = Column(Integer, nullable=False)
    lease_generation = Column(Integer, nullable=False)
    audit_run_id = Column(String(64), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    report_sha256 = Column(String(64), nullable=False)
    coverage_status = Column(String(16), nullable=False)
    termination_reason = Column(String(64), nullable=False, default="", server_default="")
    summary_json = Column(_JSON, nullable=False)
    report_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
