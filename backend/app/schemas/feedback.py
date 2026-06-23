"""
用户反馈 Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class FeedbackIn(BaseModel):
    """提交反馈"""
    feedback_type: str = Field(default="suggestion",
                               pattern="^(suggestion|complaint|praise|bug|other)$")
    content: str = Field(min_length=1)
    contact: Optional[str] = Field(default=None, max_length=100)


class FeedbackReplyIn(BaseModel):
    """管理员回复反馈"""
    admin_reply: Optional[str] = None
    status: Optional[str] = Field(default=None, pattern="^(new|read|replied|closed)$")


class FeedbackOut(BaseModel):
    id: int
    user_id: int
    feedback_type: str
    content: str
    contact: Optional[str] = None
    status: str
    admin_reply: Optional[str] = None
    handled_by: Optional[int] = None
    handled_at: Optional[datetime] = None
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}
