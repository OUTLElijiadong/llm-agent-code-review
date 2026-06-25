"""
Agent 中心相关 Pydantic Schema
"""
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


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

    @field_validator("skills", mode="before")
    @classmethod
    def _normalize_skills(cls, v: Any) -> list[str]:
        """v3.0 起 AgentRegistry 的 skills 为结构化 list[dict]。

        前端契约仍是 string[],故此处兼容性地把结构化技能拍平为技能名,
        避免响应序列化触发 ResponseValidationError。结构化元数据由
        GET /api/agents/{name}/skills 单独提供。
        """
        if not v:
            return []
        return [
            (item.get("name", "") if isinstance(item, dict) else item)
            for item in v
        ]


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


# === v3.0 AgentSkill 升级: Skill 调用相关 Schema ===


class SkillInvokeIn(BaseModel):
    """手动调用 Skill 请求体

    用于 POST /api/agents/{name}/skills/{skill}/invoke 端点,
    admin 通过此接口手动触发任意 Agent 的任意 Skill。

    Attributes:
        action: Skill 子动作(可选), 如 evolve / check_proactive /
                scan_domain / reflect_from_logs
        params: 透传给 Skill 的额外参数(可选)
    """
    action: Optional[str] = Field(
        default=None,
        description="Skill 子动作, 如 evolve/check_proactive/scan_domain/reflect_from_logs",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="透传给 Skill 的额外参数(action 会合并进去)",
    )


class SkillInvokeOut(BaseModel):
    """手动调用 Skill 响应体

    Attributes:
        success: 是否成功
        data: Skill 调用返回的数据(任意类型)
        error: 失败原因(success=False 时填充)
        effect: 效果标签(success/failed/no_op/proposal_created)
        duration_ms: 执行耗时(毫秒)
        record_id: agent_skill_record 记录 ID
    """
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    effect: str = "success"
    duration_ms: int = 0
    record_id: Optional[int] = None


class AgentSkillRecordOut(BaseModel):
    """Skill 调用记录输出

    供 SkillManager 页面展示调用历史使用。

    Attributes:
        id: 记录 ID
        agent_name: Agent 名称
        skill_name: Skill 名称
        trigger_type: 触发类型(manual/scheduled/event/proactive)
        trigger_source: 触发来源描述
        effect: 效果标签
        success: 是否成功(由 effect == "success" 派生)
        duration_ms: 执行耗时(毫秒)
        output_summary: 输出摘要
        create_time: 创建时间
    """
    id: int
    agent_name: str
    skill_name: str
    trigger_type: str
    trigger_source: str = ""
    effect: str = "success"
    success: bool = True
    duration_ms: int = 0
    output_summary: str = ""
    create_time: Optional[str] = None


class SkillMetaOut(BaseModel):
    """Skill 元数据输出

    供 /api/agents/{name}/skills 列出指定 Agent 的 Skill 元数据使用。

    Attributes:
        name: Skill name(形如 "code_reviewer.self_improve")
        description: Skill 描述
        type: Skill 类型(self_improvement / proactive)
        invocable: 是否可被外部调用
        agent_name: 所属 Agent name
    """
    name: str
    description: str = ""
    type: str = ""
    invocable: bool = True
    agent_name: str = ""
