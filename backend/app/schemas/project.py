"""
项目模块Pydantic Schema
"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ProjectIn(BaseModel):
    """创建项目请求体"""
    project_name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    language: Optional[str] = Field(default=None, max_length=50)


class ProjectUpdateIn(BaseModel):
    """更新项目请求体"""
    project_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    language: Optional[str] = Field(default=None, max_length=50)
    status: Optional[str] = Field(default=None, pattern="^(active|archived)$")


class RemoteProjectImportIn(BaseModel):
    """公开 HTTPS 源码归档导入请求。"""

    url: str = Field(min_length=12, max_length=2048)
    project_name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    language: Optional[str] = Field(default=None, max_length=50)
    audit_mode: bool = False


class RemoteProjectImportTaskOut(BaseModel):
    """可恢复远程导入任务的公开状态。"""

    task_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    attempt_count: int
    max_attempts: int
    project_id: Optional[int] = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[dict[str, str]] = None
    next_attempt_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    create_time: datetime
    update_time: datetime


class ProjectSourceArchiveOut(BaseModel):
    """隔离整包源码的安全摘要，不包含原包字节或源码。"""

    original_filename: str
    archive_sha256: str
    compressed_size: int
    expanded_size: int
    file_count: int
    max_member_size: int
    max_compression_ratio: float
    storage_status: str
    malware_status: str
    audit_status: str
    audit_started_at: Optional[datetime] = None
    audit_heartbeat_at: Optional[datetime] = None
    audit_completed_at: Optional[datetime] = None
    quarantined: bool
    threat_count: int


class ProjectOut(BaseModel):
    """项目列表项响应

    v2.0 新增 `score`: 最近一次成功审查的评分,前端 ProjectList 使用真实评分,
    不再 hash 派生假数字。无审查记录时为 None。
    R6 修复(2026-06-25):补齐 update_time,对齐 Project ORM。
    """
    id: int
    project_name: str
    description: Optional[str] = None
    language: Optional[str] = None
    status: str
    file_count: int = 0
    source_mode: str = "files"
    source_malware_status: Optional[str] = None
    can_update: bool = False
    can_delete: bool = False
    last_review_at: Optional[datetime] = None
    score: Optional[int] = None
    agent_run_count: int = 0
    last_agent_run_at: Optional[datetime] = None
    create_time: datetime
    # R6 修复:补齐 update_time,对齐 Project ORM
    update_time: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RecentTaskOut(BaseModel):
    """最近审查任务概要"""
    id: int
    score: int
    total_issues: int
    status: str
    create_time: datetime


class SourceRevisionOut(BaseModel):
    """项目源码修复副本摘要。"""
    id: int
    revision_no: int
    source_sha256: str
    parent_sha256: Optional[str] = None
    repaired_files: list[str] = Field(default_factory=list)
    repair_notes: str = ""
    create_time: Optional[datetime] = None


class ProjectDetailOut(BaseModel):
    """项目详情响应"""
    id: int
    project_name: str
    description: Optional[str] = None
    language: Optional[str] = None
    status: str
    file_count: int = 0
    source_mode: str = "files"
    source_archive: Optional[ProjectSourceArchiveOut] = None
    can_update: bool = False
    can_delete: bool = False
    agent_run_count: int = 0
    last_agent_run_at: Optional[datetime] = None
    create_time: datetime
    update_time: datetime
    recent_tasks: list[RecentTaskOut] = []
    source_revisions: list[SourceRevisionOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}
