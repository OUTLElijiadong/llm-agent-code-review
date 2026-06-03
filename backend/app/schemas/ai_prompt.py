"""AI 提示词生成模块 Pydantic Schema"""
from typing import Optional

from pydantic import BaseModel, Field


class AiPromptIssueIn(BaseModel):
    issue_id: int = Field(..., description="问题ID")
    target_tool: str = Field("generic", description="目标 AI 工具")
    use_llm: bool = Field(True, description="是否让 LLM 润色")


class AiPromptTaskIn(BaseModel):
    task_id: int = Field(..., description="审查任务ID")
    target_tool: str = Field("generic", description="目标 AI 工具")
    severity: Optional[list[str]] = Field(None, description="严重度过滤,空表示全选")
    use_llm: bool = Field(True, description="是否让 LLM 润色")


class AiPromptProjectIn(BaseModel):
    project_id: int = Field(..., description="项目ID")
    target_tool: str = Field("generic", description="目标 AI 工具")
    top_n: int = Field(30, ge=1, le=100, description="生成前 N 条(按严重度优先)")
    use_llm: bool = Field(True, description="是否让 LLM 润色")


class AiPromptItemOut(BaseModel):
    issue_id: int
    kind: str = "issue"          # issue=逐条修复; aggregate=一键修复全部(按任务聚合)
    title: str
    target_tool: str
    target_label: str
    file_path: str
    lines: str
    language: str
    severity: str
    issue_type: str
    prompt_text: str
    code_context: str
    tokens: dict = {}


class AiPromptBundleOut(BaseModel):
    prompts: list[AiPromptItemOut]
    aggregates: list[AiPromptItemOut] = []   # 一键修复全部(按审查任务聚合)
    summary: str


class AiPromptToolOut(BaseModel):
    value: str
    label: str
