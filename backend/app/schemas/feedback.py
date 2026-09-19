"""
用户反馈 Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import StrictInputModel
from app.utils.input_validation import normalize_plain_text


class FeedbackIn(StrictInputModel):
    """提交反馈"""
    feedback_type: str = Field(default="suggestion",
                               pattern="^(suggestion|complaint|praise|bug|other)$")
    content: str = Field(min_length=1, max_length=20_000)
    contact: Optional[str] = Field(default=None, max_length=100)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="反馈内容")

    @field_validator("contact")
    @classmethod
    def validate_contact(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return normalize_plain_text(
            value,
            field_name="联系方式",
            allow_empty=True,
            allow_newlines=False,
        )


class FeedbackReplyIn(StrictInputModel):
    """管理员回复反馈"""
    admin_reply: Optional[str] = Field(default=None, max_length=20_000)
    status: Optional[str] = Field(default=None, pattern="^(new|read|replied|closed)$")

    @field_validator("admin_reply")
    @classmethod
    def validate_reply(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return normalize_plain_text(value, field_name="管理员回复", allow_empty=True)


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
