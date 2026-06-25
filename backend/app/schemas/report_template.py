"""
报告模板 Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ReportTemplateIn(BaseModel):
    """报告模板创建/更新请求体"""
    name: str = Field(min_length=1, max_length=128, description="模板名称")
    type: str = Field(pattern="^(simple|detailed|compliance|custom)$", description="模板类型")
    content: str = Field(min_length=1, description="Jinja2 模板字符串")
    description: Optional[str] = Field(default=None, max_length=255, description="模板描述")


class ReportTemplateOut(BaseModel):
    """报告模板输出"""
    id: int
    name: str
    type: str
    content: str
    is_builtin: int
    creator_id: Optional[int] = None
    description: Optional[str] = None
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}
