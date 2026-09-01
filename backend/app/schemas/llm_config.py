"""
全局大模型(LLM)提供商配置 Pydantic Schema(管理员)
"""
from typing import List, Optional

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
    fallback_reason: str = ""
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    temperature: float = Field(default=0.2, ge=0, le=2)


class LlmConfigIn(BaseModel):
    """更新全局 LLM 配置;api_key 留空(None)表示不修改,空串表示清空"""
    provider: Optional[str] = Field(default=None, pattern="^(deepseek|openai|custom)$")
    base_url: Optional[str] = Field(default=None, max_length=512)
    model: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=512)
    active: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=5, le=600)
    max_retries: Optional[int] = Field(default=None, ge=0, le=5)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)


class LlmTestIn(BaseModel):
    """测试连接;字段留空则用已保存配置"""
    provider: Optional[str] = Field(default=None, pattern="^(deepseek|openai|custom)$")
    base_url: Optional[str] = Field(default=None, max_length=512)
    model: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=512)
    timeout_seconds: Optional[int] = Field(default=None, ge=5, le=600)
    max_retries: Optional[int] = Field(default=None, ge=0, le=5)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)


class LlmModelsIn(LlmTestIn):
    """模型发现请求;允许使用尚未保存的表单配置。"""


class LlmModelsOut(BaseModel):
    """模型发现结果;上游不支持列表时可保留手工模型继续。"""
    success: bool
    message: str
    models: List[str] = Field(default_factory=list)
    selected_model: str = ""
    duration_ms: int = 0
    attempts: int = 0
    fallback: bool = False
    retryable: bool = False
    next_action: str = ""
