"""Strict contracts for enterprise Xiaoling registered-Agent plans."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EnterpriseAgentTeamMemberPlan(_StrictModel):
    member_key: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    agent_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="worker", pattern=r"^(worker|verifier|summarizer)$")

    @field_validator("display_name")
    @classmethod
    def nonblank_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Agent 显示名称不能为空")
        return value.strip()


class EnterpriseAgentTeamTaskPlan(_StrictModel):
    task_key: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    member_key: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    input: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "instructions")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("团队任务标题和指令不能为空")
        return value.strip()

    @field_validator("depends_on")
    @classmethod
    def unique_dependencies(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("团队任务依赖不能重复")
        if any(not item or not all(char.isalnum() or char in "_-" for char in item) for item in value):
            raise ValueError("团队任务依赖标识无效")
        return value


class EnterpriseAgentTeamCreatePlan(_StrictModel):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4000)
    project_id: int | None = Field(default=None, gt=0)
    members: list[EnterpriseAgentTeamMemberPlan] = Field(min_length=2, max_length=6)
    tasks: list[EnterpriseAgentTeamTaskPlan] = Field(min_length=2, max_length=20)

    @field_validator("title", "objective")
    @classmethod
    def nonblank_header(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("团队标题和目标不能为空")
        return value.strip()

    @model_validator(mode="after")
    def validate_graph(self) -> "EnterpriseAgentTeamCreatePlan":
        member_keys = [member.member_key for member in self.members]
        agent_codes = [member.agent_code for member in self.members]
        task_keys = [task.task_key for task in self.tasks]
        if len(member_keys) != len(set(member_keys)) or len(agent_codes) != len(set(agent_codes)):
            raise ValueError("团队成员标识和 Agent 必须唯一")
        if len(task_keys) != len(set(task_keys)):
            raise ValueError("团队任务标识必须唯一")
        known_members, known_tasks = set(member_keys), set(task_keys)
        if any(task.member_key not in known_members for task in self.tasks):
            raise ValueError("团队任务引用了不存在的成员")
        if any(task.task_key in task.depends_on or not set(task.depends_on) <= known_tasks for task in self.tasks):
            raise ValueError("团队任务存在自依赖或未知依赖")
        graph = {task.task_key: set(task.depends_on) for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("团队任务依赖不能形成循环")
            if key in visited:
                return
            visiting.add(key)
            for dependency in graph[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)
        return self


class EnterpriseAgentTeamCancelIn(_StrictModel):
    reason: str = Field(default="用户停止", max_length=500)


class EnterpriseAgentTeamRetryIn(_StrictModel):
    task_id: int = Field(gt=0)
