"""
项目成员 Pydantic Schema

v2.4: 用于 project_member 管理 API 的请求/响应
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MemberAddIn(BaseModel):
    """添加项目成员请求体"""
    user_id: int = Field(..., description="被加入的用户ID")
    role_in_project: str = Field(default="reviewer", pattern="^(owner|reviewer)$", description="项目内角色")


class MemberRoleUpdateIn(BaseModel):
    """更新成员角色请求体"""
    role_in_project: str = Field(..., pattern="^(owner|reviewer)$", description="新的项目内角色")


class MemberOut(BaseModel):
    """项目成员响应项

    R8 修复(2026-06-25):补齐 update_time,对齐 ProjectMember ORM。
    """
    id: int
    user_id: int
    username: str
    nickname: Optional[str] = None
    role_in_project: str
    create_time: datetime
    # R8 修复:补齐 update_time,对齐 ProjectMember ORM
    update_time: Optional[datetime] = None

    model_config = {"from_attributes": True}
