"""内测邀请码 API Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class BetaInviteGenerateIn(BaseModel):
    """管理员批量生成邀请码参数。"""

    count: int = Field(default=1, ge=1, le=100)
    expiry_days: int = Field(default=7, ge=1, le=90)
    label: Optional[str] = Field(default=None, max_length=100)


class BetaInviteOut(BaseModel):
    """管理列表中的脱敏邀请码。"""

    id: int
    display_prefix: str
    label: Optional[str] = None
    status: str
    expires_at: datetime
    created_by: int
    used_by: Optional[int] = None
    used_at: Optional[datetime] = None
    create_time: datetime

    model_config = {"from_attributes": True}


class BetaInviteGenerateOut(BaseModel):
    """生成结果；codes 明文不会再次返回。"""

    codes: list[str]
    items: list[BetaInviteOut]
