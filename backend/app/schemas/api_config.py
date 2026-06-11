"""
用户 API 配置 Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ApiConfigOut(BaseModel):
    """API 配置输出（key 脱敏）"""
    provider: str = "deepseek"
    api_key_masked: str = ""
    base_url: str = ""
    model: str = ""
    is_active: bool = True
    is_custom: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ApiConfigSaveIn(BaseModel):
    """保存/更新 API 配置请求"""
    provider: str = Field(default="deepseek", description="提供商: deepseek | openai | custom")
    api_key: str = Field(..., min_length=1, max_length=256, description="API Key")
    base_url: str = Field(default="https://api.deepseek.com", max_length=512, description="API 端点")
    model: str = Field(default="deepseek-chat", max_length=128, description="模型名称")


class ApiConfigTestIn(BaseModel):
    """测试连接请求（key 不存储）"""
    provider: str = Field(default="deepseek")
    api_key: str = Field(..., min_length=1, max_length=256)
    base_url: str = Field(default="https://api.deepseek.com", max_length=512)
    model: str = Field(default="deepseek-chat", max_length=128)


class ApiConfigTestOut(BaseModel):
    """测试连接结果"""
    success: bool
    message: str
    model: str = ""
    duration_ms: int = 0
