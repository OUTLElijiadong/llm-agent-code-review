"""
维修工单 Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import StrictInputModel
from app.utils.input_validation import normalize_plain_text


class TicketIn(StrictInputModel):
    """创建工单"""
    title: str = Field(min_length=1, max_length=150)
    description: str = Field(min_length=1, max_length=50_000)
    category: str = Field(default="bug", pattern="^(bug|account|feature|performance|other)$")
    priority: str = Field(default="medium", pattern="^(low|medium|high)$")

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="工单标题", allow_newlines=False)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="工单描述")


class TicketHandleIn(StrictInputModel):
    """管理员受理工单"""
    status: Optional[str] = Field(default=None, pattern="^(pending|processing|resolved|closed)$")
    admin_reply: Optional[str] = Field(default=None, max_length=50_000)
    priority: Optional[str] = Field(default=None, pattern="^(low|medium|high)$")

    @field_validator("admin_reply")
    @classmethod
    def validate_admin_reply(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return normalize_plain_text(value, field_name="工单回复", allow_empty=True)


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
