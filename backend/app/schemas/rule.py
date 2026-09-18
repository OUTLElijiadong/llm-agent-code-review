"""
审查规则模块Pydantic Schema
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.input_validation import normalize_plain_text, normalize_rule_code


class RuleIn(BaseModel):
    """新增自定义规则请求体"""
    rule_code: str = Field(min_length=1, max_length=50)
    rule_name: str = Field(min_length=1, max_length=100)
    rule_type: Literal[
        "security", "correctness", "performance", "robustness",
        "maintainability", "style", "documentation",
    ]
    rule_content: str = Field(min_length=1, max_length=100_000)
    language: str = Field(default="*", max_length=30)
    severity: Literal["严重", "高", "中", "低"] = "中"

    @field_validator("rule_code", mode="before")
    @classmethod
    def validate_rule_code(cls, value: str) -> str:
        return normalize_rule_code(value)

    @field_validator("rule_name", "rule_content", "language", mode="before")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="规则字段")


class RuleUpdateIn(BaseModel):
    """更新规则请求体"""
    rule_name: Optional[str] = Field(default=None, max_length=100)
    rule_type: Optional[Literal[
        "security", "correctness", "performance", "robustness",
        "maintainability", "style", "documentation",
    ]] = None
    rule_content: Optional[str] = Field(default=None, max_length=100_000)
    language: Optional[str] = Field(default=None, max_length=30)
    severity: Optional[Literal["严重", "高", "中", "低"]] = None

    @field_validator("rule_name", "rule_content", "language", mode="before")
    @classmethod
    def validate_update_text(cls, value: Optional[str]) -> Optional[str]:
        return normalize_plain_text(value, field_name="规则字段") if value is not None else None


class RuleToggleIn(BaseModel):
    """启用/禁用规则请求体"""
    enabled: int = Field(ge=0, le=1)


class RuleOut(BaseModel):
    """规则输出"""
    id: int
    rule_code: str
    rule_name: str
    rule_type: str
    rule_content: str
    language: str = "*"
    severity: str = "中"
    enabled: int
    is_builtin: int
    sort_order: int
    create_time: Optional[str] = None
    update_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("create_time", "update_time", mode="before")
    @classmethod
    def _dt_to_str(cls, v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)
