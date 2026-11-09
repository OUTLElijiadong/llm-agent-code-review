"""企业远测申请的隔离审批记录；该表不授予任何远程执行能力。"""
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EnterpriseRemoteApproval(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_remote_approval"
    __table_args__ = (
        UniqueConstraint("execution_task_public_id", name="uq_enterprise_remote_approval_execution"),
        Index("ix_enterprise_remote_approval_status", "enterprise_id", "status", "id"),
        Index("ix_enterprise_remote_approval_group", "enterprise_id", "group_id", "status", "id"),
        Index("ix_enterprise_remote_approval_owner", "enterprise_id", "owner_id", "id"),
        Index("ix_enterprise_remote_approval_project", "enterprise_id", "project_id", "status", "id"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=False)
    # Nullable only for legacy rows created before enterprise-project binding;
    # such rows can be rejected/cancelled but may no longer be executed.
    project_id = Column(BigInteger, nullable=True)
    owner_id = Column(BigInteger, nullable=False)
    target_url = Column(String(512), nullable=False)
    target_host = Column(String(255), nullable=False)
    authorization_basis = Column(Text, nullable=False)
    scope_notes = Column(Text, nullable=False, default="")
    requested_window_hours = Column(Integer, nullable=False)
    scope_version = Column(String(32), nullable=False)
    scope_digest = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    decided_by = Column(BigInteger, nullable=True)
    decision_reason = Column(String(500), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    execution_task_public_id = Column(String(32), nullable=True)
