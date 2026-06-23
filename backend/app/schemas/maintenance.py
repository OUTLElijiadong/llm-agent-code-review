"""
维修工单 Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TicketIn(BaseModel):
    """创建工单"""
    title: str = Field(min_length=1, max_length=150)
    description: str = Field(min_length=1)
    category: str = Field(default="bug", pattern="^(bug|account|feature|performance|other)$")
    priority: str = Field(default="medium", pattern="^(low|medium|high)$")


class TicketHandleIn(BaseModel):
    """管理员受理工单"""
    status: Optional[str] = Field(default=None, pattern="^(pending|processing|resolved|closed)$")
    admin_reply: Optional[str] = None
    priority: Optional[str] = Field(default=None, pattern="^(low|medium|high)$")


class TicketOut(BaseModel):
    id: int
    user_id: int
    title: str
    category: str
    description: str
    priority: str
    status: str
    admin_reply: Optional[str] = None
    handled_by: Optional[int] = None
    handled_at: Optional[datetime] = None
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}
