"""Agent 治理平台 Pydantic Schema。"""
import json
from datetime import datetime
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, field_validator


def to_datetime_str(value: Any) -> Optional[str]:
    """将时间值转换为 ISO 字符串。

    Args:
        value: 任意时间输入，通常为 datetime 或字符串。

    Returns:
        Optional[str]: ISO 时间字符串；空值返回 None。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def parse_json_value(value: Any) -> Optional[Union[dict, list]]:
    """解析数据库 JSON 文本字段。

    Args:
        value: 数据库字段值，可能是 JSON 字符串、dict、list 或空值。

    Returns:
        Optional[Union[dict, list]]: 解析后的 JSON 对象；解析失败返回 None。
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, (dict, list)) else None
    return None


class AgentProfileOut(BaseModel):
    """Agent 治理画像输出。

    R5 修复(2026-06-25):补齐 config_json 字段,
    使管理员可查看 Agent 扩展配置(对齐 AgentProfile ORM 与 AgentProfileUpdateIn)。
    """

    code: str
    name: str
    description: str = ""
    category: str = "general"
    status: str = "idle"
    model: Optional[str] = None
    icon: str = "base"
    color: str = "#5B58E8"
    budget_tokens_daily: int = 0
    priority: int = 50
    auto_approval_threshold: float = 0.75
    is_enabled: int = 1
    # R5 修复:补齐 config_json,使管理员可查看扩展配置
    config_json: Optional[Union[dict, list]] = None
    skills: list[str] = Field(default_factory=list)
    tool_count: int = 0
    memory_count: int = 0
    knowledge_count: int = 0
    create_time: Optional[str] = None
    update_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("config_json", mode="before")
    @classmethod
    def parse_config(cls, value: Any) -> Optional[Union[dict, list]]:
        """解析 Agent 扩展配置 JSON。

        Args:
            value: 数据库 JSON 字段。

        Returns:
            Optional[Union[dict, list]]: 解析后的 JSON。
        """
        return parse_json_value(value)

    @field_validator("create_time", "update_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化时间字段。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class AgentProfileUpdateIn(BaseModel):
    """Agent 治理画像更新输入。"""

    status: Optional[str] = None
    budget_tokens_daily: Optional[int] = Field(default=None, ge=0)
    priority: Optional[int] = Field(default=None, ge=0, le=100)
    auto_approval_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    is_enabled: Optional[int] = Field(default=None, ge=0, le=1)
    config_json: Optional[dict] = None


class GovernanceOverviewOut(BaseModel):
    """管理端治理大屏总览输出。"""

    agents_total: int = 0
    agents_enabled: int = 0
    approvals_pending: int = 0
    approvals_auto_today: int = 0
    policy_decisions_today: int = 0
    tool_calls_today: int = 0
    alerts_open: int = 0
    knowledge_docs_total: int = 0
    memory_items_total: int = 0
    jobs_enabled: int = 0
    reward_score_total: float = 0.0
    risk_distribution: list[dict] = Field(default_factory=list)
    recent_alerts: list[dict] = Field(default_factory=list)


class PolicyRuleOut(BaseModel):
    """策略规则输出。"""

    id: int
    rule_code: str
    name: str
    subject: str
    action: str
    resource: str
    effect: str
    risk_level: str
    condition_json: Optional[Union[dict, list]] = None
    priority: int
    enabled: int
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("condition_json", mode="before")
    @classmethod
    def parse_condition(cls, value: Any) -> Optional[Union[dict, list]]:
        """解析策略条件 JSON。

        Args:
            value: 数据库 JSON 字段。

        Returns:
            Optional[Union[dict, list]]: 解析后的 JSON。
        """
        return parse_json_value(value)

    @field_validator("create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化创建时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class PolicyRuleUpsertIn(BaseModel):
    """策略规则创建或更新输入。"""

    rule_code: str = Field(..., min_length=1, max_length=120)
    name: str = Field(..., min_length=1, max_length=160)
    subject: str = Field(default="*", max_length=120)
    action: str = Field(default="*", max_length=120)
    resource: str = Field(default="*", max_length=120)
    effect: str = Field(default="allow")
    risk_level: str = Field(default="low")
    condition_json: Optional[Union[dict, list]] = None
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: int = Field(default=1, ge=0, le=1)


class PolicyEvaluateIn(BaseModel):
    """策略试算输入。"""

    subject: str = Field(default="agent:unknown")
    action: str
    resource: str = "*"
    context: dict = Field(default_factory=dict)


class PolicyDecisionOut(BaseModel):
    """策略决策输出。"""

    id: Optional[int] = None
    subject: str
    action: str
    resource: str
    decision: str
    risk_level: str
    risk_score: float
    reason: Optional[str] = None
    matched_rule_id: Optional[int] = None
    context_json: Optional[Union[dict, list]] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("context_json", mode="before")
    @classmethod
    def parse_context(cls, value: Any) -> Optional[Union[dict, list]]:
        """解析决策上下文 JSON。

        Args:
            value: 数据库 JSON 字段。

        Returns:
            Optional[Union[dict, list]]: 解析后的 JSON。
        """
        return parse_json_value(value)

    @field_validator("create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化创建时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class ApprovalItemOut(BaseModel):
    """审批事项输出。"""

    id: int
    title: str
    agent_code: Optional[str] = None
    action: str
    resource: str
    risk_level: str
    status: str
    decision: Optional[str] = None
    decision_reason: Optional[str] = None
    request_json: Optional[Union[dict, list]] = None
    decided_by: Optional[int] = None
    decided_at: Optional[str] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("request_json", mode="before")
    @classmethod
    def parse_request(cls, value: Any) -> Optional[Union[dict, list]]:
        """解析审批请求 JSON。

        Args:
            value: 数据库 JSON 字段。

        Returns:
            Optional[Union[dict, list]]: 解析后的 JSON。
        """
        return parse_json_value(value)

    @field_validator("decided_at", "create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化时间字段。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class ApprovalDecisionIn(BaseModel):
    """审批处理输入。"""

    note: str = Field(default="", max_length=500)


class ToolCallLogOut(BaseModel):
    """工具调用日志输出。"""

    id: int
    agent_code: str
    tool_code: str
    action: str
    resource: str
    status: str
    risk_level: str
    decision: str
    input_summary: Optional[str] = None
    output_summary: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0
    policy_decision_id: Optional[int] = None
    approval_id: Optional[int] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化创建时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class AgentMemoryOut(BaseModel):
    """Agent 记忆输出。"""

    id: int
    agent_code: str
    memory_type: str
    title: str
    content: str
    weight: float
    status: str
    source_ref: Optional[str] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化创建时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class AgentMemoryCreateIn(BaseModel):
    """Agent 记忆创建输入。"""

    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)
    memory_type: str = Field(default="long_term", max_length=30)
    weight: float = Field(default=1.0, ge=0, le=10)
    source_ref: str = Field(default="", max_length=160)


class AgentKnowledgeDocOut(BaseModel):
    """Agent 知识文档输出。"""

    id: int
    agent_code: str
    source_type: str
    source_ref: Optional[str] = None
    title: str
    risk_level: str
    confidence: float
    status: str
    char_count: int
    chunk_count: int
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化创建时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class AgentKnowledgeSourceOut(BaseModel):
    """Agent 知识来源输出。"""

    id: int
    agent_code: str
    source_type: str
    source_uri: str
    whitelist: int
    enabled: int
    config_json: Optional[Union[dict, list]] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("config_json", mode="before")
    @classmethod
    def parse_config(cls, value: Any) -> Optional[Union[dict, list]]:
        """解析知识来源配置 JSON。

        Args:
            value: 数据库 JSON 字段。

        Returns:
            Optional[Union[dict, list]]: 解析后的 JSON。
        """
        return parse_json_value(value)

    @field_validator("create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化创建时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class AgentKnowledgeSourceUpsertIn(BaseModel):
    """Agent 知识来源创建或更新输入。"""

    agent_code: str = Field(..., min_length=1, max_length=80)
    source_type: str = Field(..., min_length=1, max_length=30)
    source_uri: str = Field(..., min_length=1, max_length=500)
    whitelist: int = Field(default=1, ge=0, le=1)
    enabled: int = Field(default=1, ge=0, le=1)
    config_json: Optional[dict] = None


class AgentKnowledgeDocCreateIn(BaseModel):
    """Agent 知识文档创建输入。"""

    agent_code: str = Field(..., min_length=1, max_length=80)
    title: str = Field(..., min_length=1, max_length=240)
    content: str = Field(..., min_length=1)
    source_type: str = Field(default="manual", max_length=30)
    source_ref: str = Field(default="", max_length=160)
    risk_level: str = Field(default="low")
    confidence: float = Field(default=1.0, ge=0, le=1)


class AgentJobOut(BaseModel):
    """Agent 调度任务输出。"""

    id: int
    job_code: str
    job_type: str
    agent_code: Optional[str] = None
    schedule: str
    status: str
    last_run_at: Optional[str] = None
    config_json: Optional[Union[dict, list]] = None

    model_config = {"from_attributes": True}

    @field_validator("config_json", mode="before")
    @classmethod
    def parse_config(cls, value: Any) -> Optional[Union[dict, list]]:
        """解析任务配置 JSON。

        Args:
            value: 数据库 JSON 字段。

        Returns:
            Optional[Union[dict, list]]: 解析后的 JSON。
        """
        return parse_json_value(value)

    @field_validator("last_run_at", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化运行时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class AgentAlertOut(BaseModel):
    """Agent 告警输出。"""

    id: int
    alert_type: str
    severity: str
    status: str
    title: str
    detail_json: Optional[Union[dict, list]] = None
    # 安全监控弹窗扩展字段（旧字段保持兼容）
    category: Optional[str] = None
    source: Optional[str] = None
    user_id: Optional[int] = None
    read_at: Optional[str] = None
    fingerprint: Optional[str] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("detail_json", mode="before")
    @classmethod
    def parse_detail(cls, value: Any) -> Optional[Union[dict, list]]:
        """解析告警详情 JSON。

        Args:
            value: 数据库 JSON 字段。

        Returns:
            Optional[Union[dict, list]]: 解析后的 JSON。
        """
        return parse_json_value(value)

    @field_validator("create_time", "read_at", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化时间字段。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class SecurityStatusOut(BaseModel):
    """安全态势聚合输出。"""

    since_hours: int = 24
    ssh: dict = Field(default_factory=dict)
    attacks: dict = Field(default_factory=dict)
    backup: dict = Field(default_factory=dict)
    open_alerts: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


class SecurityMonitorRunOut(BaseModel):
    """安全巡检执行摘要输出。"""

    success: bool = True
    created_alerts: list[dict] = Field(default_factory=list)
    actions: dict = Field(default_factory=dict)
    errors: list[dict] = Field(default_factory=list)
    job_id: Optional[int] = None


class AgentToolPermissionOut(BaseModel):
    """Agent 工具权限输出。"""

    id: int
    agent_code: str
    tool_code: str
    permission: str
    risk_level: str
    enabled: int
    note: Optional[str] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("create_time", mode="before")
    @classmethod
    def serialize_time(cls, value: Any) -> Optional[str]:
        """序列化创建时间。

        Args:
            value: 原始时间值。

        Returns:
            Optional[str]: ISO 时间字符串。
        """
        return to_datetime_str(value)


class AgentToolPermissionUpsertIn(BaseModel):
    """Agent 工具权限创建或更新输入。"""

    agent_code: str = Field(..., min_length=1, max_length=80)
    tool_code: str = Field(..., min_length=1, max_length=120)
    permission: str = Field(default="allow")
    risk_level: str = Field(default="low")
    enabled: int = Field(default=1, ge=0, le=1)
    note: str = Field(default="", max_length=300)


class AgentJobUpdateIn(BaseModel):
    """Agent 调度任务更新输入。"""

    schedule: Optional[str] = Field(default=None, max_length=120)
    status: Optional[str] = Field(default=None, max_length=30)
    config_json: Optional[dict] = None


class AgentRewardCreateIn(BaseModel):
    """Agent 奖惩事件创建输入。"""

    agent_code: str = Field(..., min_length=1, max_length=80)
    event_type: str = Field(default="reward")
    score: float = Field(..., ge=-100, le=100)
    reason: str = Field(..., min_length=1, max_length=500)
    impact: Optional[dict] = None


class AgentArtifactVersionCreateIn(BaseModel):
    """Agent artifact 版本创建输入。"""

    agent_code: str = Field(..., min_length=1, max_length=80)
    artifact_type: str = Field(..., min_length=1, max_length=50)
    version: str = Field(..., min_length=1, max_length=50)
    content: str = Field(..., min_length=1)
    snapshot: str = ""
    status: str = Field(default="draft")


class AgentAlertResolveIn(BaseModel):
    """Agent 告警处理输入。"""

    note: str = Field(default="", max_length=300)
