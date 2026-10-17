"""
报告模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ReportListItem(BaseModel):
    """报告列表项"""
    task_id: int
    task_name: Optional[str] = None
    project_name: str = ""
    total_issues: int
    score: Optional[int] = None
    status: str
    error_message: Optional[str] = None
    coverage: dict = Field(default_factory=dict, description="扫描覆盖、未扫描、跳过与排除范围")
    create_time: datetime
    source: dict = Field(default_factory=dict, description="来源、统计依据及既有领域详情接口")


class ReportDetailOut(BaseModel):
    """报告详情"""
    project: dict
    task: dict
    stats: dict
    summary: Optional[str] = None
    files: list[dict]
    rules_snapshot: list[dict]
    source: dict = Field(default_factory=dict, description="来源、统计依据及既有领域详情接口")


class DomainReportExportOut(BaseModel):
    """领域 JSON 保留原始领域结构，不将沙箱问题或渗透发现伪装为标准问题。"""

    document_type: str = "domain_report"
    schema_version: str = "domain-report-v1"
    source: dict
    project: dict
    task_info: dict
    statistics: dict
    score: Optional[int] = None
    summary: str
    domain_data: dict
    native_exports: list[dict] = Field(default_factory=list)
