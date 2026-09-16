"""
仪表盘模块Pydantic Schema
"""

from __future__ import annotations

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
    score: Optional[int] = None
    review_type: Optional[str] = None
    create_time: Optional[datetime] = None


class SummaryOut(BaseModel):
    """仪表盘汇总数据"""

    project_count: int
    file_count: int
    archive_file_count: int = 0
    review_count: int
    total_issues: int
    severe_issues: int
    avg_score: float
    code_review_count: int = 0
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


class RunningReviewItem(BaseModel):
    """进行中的审查任务(进度=processed_files/total_files)"""

    id: int
    task_name: str
    project_id: int
    project_name: str
    review_type: str
    status: str
    processed_files: int = 0
    total_files: int = 0
    create_time: Optional[str] = None


class RunningAgentItem(BaseModel):
    """本人进行中的小菱/管理 Agent 运行"""

    run_id: str
    surface: str
    session_key: str
    status: str
    update_time: Optional[str] = None


class RunningOut(BaseModel):
    """首页「后台进行中」面板数据"""

    reviews: list[RunningReviewItem] = []
    agents: list[RunningAgentItem] = []
