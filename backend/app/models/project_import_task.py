"""可恢复的远程项目导入任务。"""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin

_JSON = LONGTEXT().with_variant(Text, "sqlite")


class ProjectImportTask(Base, IdMixin, TimestampMixin):
    """远程归档下载、校验和项目入库的持久化工作项。"""

    __tablename__ = "project_import_task"
    __table_args__ = (
        UniqueConstraint("public_id", name="uk_project_import_task_public"),
        UniqueConstraint(
            "user_id",
            "idempotency_key_hash",
            name="uk_project_import_task_idempotency",
        ),
        Index(
            "ix_project_import_task_queue",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "id",
        ),
        Index("ix_project_import_task_owner_status", "user_id", "status", "id"),
    )

    public_id = Column(String(32), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    idempotency_key_hash = Column(String(64), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    request_json = Column(_JSON, nullable=False)
    status = Column(String(24), nullable=False, default="queued")
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    next_attempt_at = Column(DateTime, nullable=True)
    lease_token = Column(String(80), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    project_id = Column(BigInteger, nullable=True)
    result_json = Column(_JSON, nullable=False, default="{}")
    error_code = Column(String(80), nullable=True)
    error_message = Column(Text, nullable=True)
    cancel_reason = Column(Text, nullable=True)
    cancel_requested_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
