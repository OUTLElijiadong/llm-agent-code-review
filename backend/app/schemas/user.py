"""
用户管理Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_serializer

from app.core.server_time import iso_utc


class UserListItem(BaseModel):
    """用户列表项

    R6 修复(2026-06-25):补齐 update_time,对齐 User ORM。
    """
    id: int
    username: str
    nickname: Optional[str] = None
    email: Optional[str] = None
    avatar: Optional[str] = None
    role: str
    status: int
    last_login: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    create_time: Optional[datetime] = None
    # R6 修复:补齐 update_time,对齐 User ORM
    update_time: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @field_serializer("last_login", "create_time", "update_time")
    def _serialize_datetime(self, dt: Optional[datetime], _info) -> Optional[str]:
        """将datetime对象序列化为带 UTC 标记的 ISO 字符串(库内 naive 值即 UTC)"""
        return iso_utc(dt)


class PasswordResetOut(BaseModel):
    """只在本次管理员响应中返回的随机密码。"""

    temporary_password: str = Field(min_length=24, max_length=24)


class RoleIn(BaseModel):
    """设置角色请求体"""
    role: str = Field(pattern="^(admin|user|reviewer)$")


class StatusIn(BaseModel):
    """启用/禁用请求体"""
    status: int = Field(ge=0, le=1)
