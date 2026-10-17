"""圆桌讨论请求与任务级参会者契约。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RoundtableTemporaryExpert(BaseModel):
    """只在单次圆桌中生效的专家定义，不包含持久化模板或权限字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=80)
    focus: str = Field(min_length=1, max_length=500)
    issue_types: list[str] = Field(default_factory=list, max_length=8)
    instruction: str = Field(min_length=1, max_length=2000)

    @field_validator("name", "focus", "instruction")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("临时专家字段不能为空")
        return value.strip()

    @field_validator("issue_types")
    @classmethod
    def normalize_issue_types(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(value):
            raise ValueError("临时专家问题类型不能包含空值")
        if len(set(normalized)) != len(normalized):
            raise ValueError("临时专家问题类型不能重复")
        return normalized
