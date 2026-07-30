"""
用户管理Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_serializer


class UserListItem(BaseModel):
    """用户列表项

    R6 修复(2026-06-25):补齐 update_time,对齐 User ORM。
    """
    id: int
    username: str
    nickname: Optional[str] = None
    email: Optional[str] = None
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
        """将datetime对象序列化为ISO格式字符串"""
        if dt is None:
            return None
        return dt.isoformat()


class RoleIn(BaseModel):
    """设置角色请求体"""
    role: str = Field(pattern="^(admin|user|reviewer)$")


class StatusIn(BaseModel):
    """启用/禁用请求体"""
    status: int = Field(ge=0, le=1)
