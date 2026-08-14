"""
报告模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ReportListItem(BaseModel):
    """报告列表项"""
    task_id: int
    task_name: Optional[str] = None
    project_name: str = ""
    total_issues: int
    score: int
    status: str
    create_time: datetime


class ReportDetailOut(BaseModel):
    """报告详情"""
    project: dict
    task: dict
    stats: dict
    summary: Optional[str] = None
    files: list[dict]
    rules_snapshot: list[dict]
