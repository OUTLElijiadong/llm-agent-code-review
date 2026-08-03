"""待审计整包源码归档模型。"""

from sqlalchemy import BigInteger, Column, DateTime, Float, Index, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT
from sqlalchemy.orm import deferred

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ProjectSourceArchive(Base, IdMixin, TimestampMixin):
    """保存原始压缩包与全包扫描摘要，不将危险成员写入可编辑文件表。"""

    __tablename__ = "project_source_archive"
    __table_args__ = (
        Index("uk_project_source_archive_project", "project_id", unique=True),
        Index("ix_project_source_archive_owner", "owner_id"),
        Index("ix_project_source_archive_malware", "malware_status"),
    )

    project_id = Column(BigInteger, nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    original_filename = Column(String(255), nullable=False)
    media_type = Column(String(120), nullable=False, default="application/zip")
    archive_sha256 = Column(String(64), nullable=False)
    compressed_size = Column(Integer, nullable=False)
    expanded_size = Column(BigInteger, nullable=False)
    file_count = Column(Integer, nullable=False)
    max_member_size = Column(BigInteger, nullable=False)
    max_compression_ratio = Column(Float, nullable=False, default=1.0)
    storage_status = Column(String(30), nullable=False, default="active")
    malware_status = Column(String(30), nullable=False, comment="clean/infected/degraded/error")
    audit_status = Column(String(30), nullable=False, default="not_started")
    audit_run_id = Column(String(64), nullable=True)
    audit_started_at = Column(DateTime, nullable=True)
    audit_heartbeat_at = Column(DateTime, nullable=True)
    audit_completed_at = Column(DateTime, nullable=True)
    audit_result_json = deferred(
        Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
    )
    threat_count = Column(Integer, nullable=False, default=0)
    scan_summary_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    archive_blob = deferred(
        Column(LONGBLOB().with_variant(LargeBinary, "sqlite"), nullable=False)
    )
