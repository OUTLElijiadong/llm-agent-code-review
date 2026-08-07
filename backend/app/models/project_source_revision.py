"""项目源码修复副本(语法修复 Agent 产物)。

沙箱语法修复后保留一份"修复后源码"作为项目副本,下次审计可选用
原始源码或任一修复副本;原始归档始终不变。
"""
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import deferred

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ProjectSourceRevision(Base, IdMixin, TimestampMixin):
    """项目源码修复副本,每项目可有多个(按 revision_no 递增)。"""

    __tablename__ = "project_source_revision"
    __table_args__ = (
        Index("uk_project_source_revision_no", "project_id", "revision_no", unique=True),
        Index("ix_project_source_revision_project", "project_id"),
    )

    project_id = Column(BigInteger, nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    revision_no = Column(Integer, nullable=False, comment="项目内副本序号,从 1 开始")
    source_sha256 = Column(String(64), nullable=False)
    parent_sha256 = Column(String(64), nullable=True, comment="原始源码 sha")
    repaired_files_json = Column(
        Text, nullable=False, comment="修复文件清单 JSON [{file, line, message}]"
    )
    repair_notes = Column(String(500), nullable=False, default="")
    archive_blob = deferred(
        Column(LONGBLOB, nullable=False, comment="修复后源码 zip")
    )
    create_time = Column(DateTime, nullable=False)
    update_time = Column(DateTime, nullable=False)
