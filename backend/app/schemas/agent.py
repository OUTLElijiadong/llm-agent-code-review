"""
Agent 中心相关 Pydantic Schema
"""
from typing import Optional

from pydantic import BaseModel, Field


class AgentProfileOut(BaseModel):
    """代理画像输出: 镜像 app.ai.multi_agent 中的 ReviewAgentProfile (v1.0 兼容)"""
    code: str
    name: str
    focus: str
    issue_types: list[str]
    instruction: str
    enabled: bool = True


class AgentUsageOut(BaseModel):
    """单代理调用统计"""
    code: str
    name: str
    call_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    last_called_at: Optional[str] = None


class ReviewTypeMappingOut(BaseModel):
    """审查类型 → 代理组合映射"""
    review_type: str
    label: str
    agent_codes: list[str]


class AgentOverviewOut(BaseModel):
    """Agent 中心首屏数据聚合 (v1.0 兼容)"""
    agents: list[AgentProfileOut]
    type_mappings: list[ReviewTypeMappingOut]
    usage: list[AgentUsageOut]


# === v2.0 新增 ===


class AgentRuntimeOut(BaseModel):
    """v2.0 真实注册的 Agent 运行时元数据 (与 AgentRegistry 严格同步)"""
    code: str
    name: str
    description: str = ""
    icon: str = "base"
    color: str = "#5B58E8"
    category: str = "general"
    skills: list[str] = []
    status: str = "idle"
    model: str = ""
    call_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    last_called_at: Optional[str] = None


class AgentRuntimeSummaryOut(BaseModel):
    """注册中心汇总: 总数 + category 分桶"""
    total: int
    by_category: list[dict]


class AgentSituationOut(BaseModel):
    """v2.0 态势感知面板数据"""
    online: int = Field(..., description="当前在岗 Agent 数")
    working: int = Field(0, description="工作中数量(M2 阶段接入)")
    idle: int = Field(0, description="空闲数量")
    today_calls: int = Field(0, description="今日累计调用数")
    spectrum: list[dict] = Field(default_factory=list, description="近 N 分钟调用波形")
    hotspots: list[dict] = Field(default_factory=list, description="近期热点 Agent")
