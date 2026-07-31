"""
仪表盘模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RecentTaskOut(BaseModel):
    """仪表盘最近审查任务项"""
    id: int
    task_name: str
    project_id: int
    project_name: str
    status: str
    score: int
    create_time: Optional[datetime] = None


class SummaryOut(BaseModel):
    """仪表盘汇总数据"""
    project_count: int
    file_count: int
    review_count: int
    total_issues: int
    severe_issues: int
    avg_score: float
    recent_tasks: list[RecentTaskOut]


class RiskItem(BaseModel):
    """风险等级分布项"""
    severity: str
    count: int


class IssueTypeItem(BaseModel):
    """问题类型统计项"""
    issue_type: str
    count: int


class ScoreTrendItem(BaseModel):
    """评分趋势项"""
    task_id: int
    score: int
    create_time: datetime


class FrequencyItem(BaseModel):
    """审查频次项"""
    date: str
    count: int
