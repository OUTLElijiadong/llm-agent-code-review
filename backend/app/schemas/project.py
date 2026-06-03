"""
项目模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

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


class ProjectOut(BaseModel):
    """项目列表项响应

    v2.0 新增 `score`: 最近一次成功审查的评分,前端 ProjectList 使用真实评分,
    不再 hash 派生假数字。无审查记录时为 None。
    """
    id: int
    project_name: str
    description: Optional[str] = None
    language: Optional[str] = None
    status: str
    file_count: int = 0
    last_review_at: Optional[datetime] = None
    score: Optional[int] = None
    create_time: datetime

    model_config = {"from_attributes": True}


class RecentTaskOut(BaseModel):
    """最近审查任务概要"""
    id: int
    score: int
    total_issues: int
    status: str
    create_time: datetime


class ProjectDetailOut(BaseModel):
    """项目详情响应"""
    id: int
    project_name: str
    description: Optional[str] = None
    language: Optional[str] = None
    status: str
    file_count: int = 0
    create_time: datetime
    update_time: datetime
    recent_tasks: list[RecentTaskOut] = []

    model_config = {"from_attributes": True}
