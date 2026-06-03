"""
鉴权模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterIn(BaseModel):
    """用户注册请求体"""
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=32)
    email: Optional[EmailStr] = None
    nickname: Optional[str] = Field(default=None, max_length=50)


class LoginIn(BaseModel):
    """用户登录请求体"""
    username: str
    password: str


class UserOut(BaseModel):
    """用户信息公开响应"""
    id: int
    username: str
    nickname: Optional[str] = None
    email: Optional[str] = None
    role: str
    status: int = 1
    last_login: Optional[str] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("last_login", "create_time", mode="before")
    @classmethod
    def _datetime_to_str(cls, v):
        """将ORM返回的datetime对象转为字符串"""
        if isinstance(v, datetime):
            return v.isoformat()
        return v


class LoginOut(BaseModel):
    """登录成功响应"""
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user: UserOut


class ChangePasswordIn(BaseModel):
    """修改密码请求体"""
    old_password: str = Field(min_length=6, max_length=32)
    new_password: str = Field(min_length=6, max_length=32)
