"""动态子 Agent 团队 API 的严格输入契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TemporaryAgentDefinition(_StrictModel):
    """任务内的专项分析职责；不接受权限、代码或模型端点覆盖。"""

    purpose: str = Field(min_length=1, max_length=1000)
    instructions: str = Field(min_length=1, max_length=8000)

    @field_validator("purpose", "instructions")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("临时 Agent 职责和指令不能为空")
        return value.strip()


class AgentTeamMemberIn(_StrictModel):
    member_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=200, pattern=r"^(?:agent|custom|temporary):[A-Za-z0-9_-]+$")
    role: Literal["worker", "verifier", "summarizer"] = "worker"
    template_id: Optional[int] = Field(default=None, gt=0)
    template_version_id: Optional[int] = Field(default=None, gt=0)
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    definition: Optional[TemporaryAgentDefinition] = None

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if value.startswith("agent:"):
            code = value[6:]
        elif value.startswith("custom:"):
            code = value[7:]
        elif value.startswith("temporary:"):
            code = value[10:]
        else:
            raise ValueError("成员目标只能是 agent:<code>、custom:<code> 或 temporary:<member_key>")
        if not code or not all(char.isalnum() or char in "_-" for char in code):
            raise ValueError("成员目标编码非法")
        return value

    @model_validator(mode="after")
    def validate_definition(self) -> "AgentTeamMemberIn":
        if self.address.startswith("temporary:"):
            if self.address != f"temporary:{self.member_key}" or self.definition is None:
                raise ValueError("临时成员必须使用 temporary:<member_key> 并提供 definition")
            if self.template_id is not None or self.template_version_id is not None:
                raise ValueError("临时成员不能绑定永久模板或版本")
        elif self.definition is not None:
            raise ValueError("仅 temporary 成员可以提供 definition")
        return self


class AgentTeamTaskIn(_StrictModel):
    task_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    member_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=240)
    instructions: str = Field(min_length=1, max_length=12000)
    depends_on: List[str] = Field(default_factory=list, max_length=50)
    input: Dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, value: List[str]) -> List[str]:
        if len(value) != len(set(value)):
            raise ValueError("任务依赖不能重复")
        if any(not item or not all(char.isalnum() or char in "_-" for char in item) for item in value):
            raise ValueError("任务依赖标识非法")
        return value


class AgentTeamCreateIn(_StrictModel):
    surface: Literal["user", "admin"]
    session_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=12000)
    members: List[AgentTeamMemberIn] = Field(min_length=1, max_length=16)
    tasks: List[AgentTeamTaskIn] = Field(min_length=1, max_length=100)
    max_active_children: int = Field(default=3, ge=1, le=32)
    max_attempts: int = Field(default=3, ge=1, le=10)
    priority: int = Field(default=0, ge=-100, le=100)
    deadline_at: Optional[datetime] = None
    trace_id: str = Field(default="", max_length=80)

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        if value and not all(char.isalnum() or char in "-_.:" for char in value):
            raise ValueError("trace_id 标识非法")
        return value


class AgentTeamRetryIn(_StrictModel):
    task_keys: List[str] = Field(default_factory=list, max_length=100)
    strategy_changes: Dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("strategy_changes")
    @classmethod
    def validate_strategy_changes(cls, value: Dict[str, str]) -> Dict[str, str]:
        for task_key, strategy in value.items():
            if not task_key or not all(char.isalnum() or char in "_-" for char in task_key):
                raise ValueError("改道策略的任务标识非法")
            if len(strategy.strip()) < 8 or len(strategy) > 4000:
                raise ValueError("每条改道策略必须为 8 到 4000 个字符")
        return {key: strategy.strip() for key, strategy in value.items()}


class AgentTeamCancelIn(_StrictModel):
    reason: str = Field(default="用户取消", max_length=1000)


class AgentTeamArchiveIn(_StrictModel):
    reason: str = Field(default="归档", max_length=1000)
