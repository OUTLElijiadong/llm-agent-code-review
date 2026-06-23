"""
全局大模型(LLM)提供商配置 Pydantic Schema(管理员)
"""
from typing import Optional

from pydantic import BaseModel, Field


class LlmConfigOut(BaseModel):
    """脱敏输出"""
    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    active: bool = False
    api_key_masked: str = ""
    is_set: bool = False
    source: str = "default"  # default=走系统DeepSeek | global=走自定义覆盖


class LlmConfigIn(BaseModel):
    """更新全局 LLM 配置;api_key 留空(None)表示不修改,空串表示清空"""
    provider: Optional[str] = Field(default=None, pattern="^(deepseek|openai|custom)$")
    base_url: Optional[str] = Field(default=None, max_length=512)
    model: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=512)
    active: Optional[bool] = None


class LlmTestIn(BaseModel):
    """测试连接;字段留空则用已保存配置"""
    base_url: Optional[str] = Field(default=None, max_length=512)
    model: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=512)
