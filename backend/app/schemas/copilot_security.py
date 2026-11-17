"""小菱安全中心 API 数据结构。

策略本身是一份扁平配置，这里给出显式字段模型：前端据此生成表单，
后端据它校验入参，避免前后端各写一套字段名。
"""
# ┏━ 【文件导读】app/schemas/copilot_security.py
# ┃ 层级：数据契约层（Pydantic 请求/响应模型）
# ┃ 作用：小菱安全中心的策略、预设、停机开关、演练扫描、态势评分与事件列表的数据结构。
# ┃ 引用了：schemas/common
# ┃ 被引用：v1/copilot_security
# ┗━ 以上导读只是注释，不影响运行；撤销方法见 docs/后端代码导读20261024/README.md

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import StrictInputModel
from app.utils.input_validation import normalize_plain_text

# 策略模式与预设名，前后端共用同一口径。
PolicyMode = Literal["monitor", "balanced", "strict", "max"]
PresetName = Literal["monitor", "balanced", "strict", "max"]


# ▎完整策略：一次 GET 就把表单要填的 31 个字段全给全。
class CopilotSecurityPolicyOut(BaseModel):
    """生效中的小菱安全策略。"""

    enabled: bool = True
    mode: PolicyMode = "balanced"
    preset: str = "balanced"
    kill_switch: bool = False
    kill_switch_reason: str = ""
    governed_agents: List[str] = Field(default_factory=list)
    session_guard_surfaces: List[str] = Field(default_factory=list)
    ip_allowlist: List[str] = Field(default_factory=list)
    trust_proxy_headers: bool = False
    bind_session_to_ip: bool = False
    session_idle_timeout_seconds: int = 0
    session_absolute_timeout_seconds: int = 0
    max_message_chars: int = 0
    injection_scan_enabled: bool = True
    injection_block_threshold: int = 60
    injection_warn_threshold: int = 30
    injection_blocked_surfaces: List[str] = Field(default_factory=list)
    redaction_enabled: bool = True
    redact_in_audit: bool = True
    redaction_kinds: List[str] = Field(default_factory=list)
    tool_allowlist: List[str] = Field(default_factory=list)
    tool_denylist: List[str] = Field(default_factory=list)
    deny_shell: bool = True
    deny_write_actions: bool = False
    deny_production_config: bool = True
    require_approval_for_writes: bool = False
    dangerous_tools_require_approval: bool = True
    max_tool_calls_per_turn: int = 0
    max_tool_calls_per_hour: int = 0
    audit_events_enabled: bool = True
    alert_on_block: bool = True
    step_up_window_seconds: int = 0


# ▎策略局部更新：所有字段可选，只提交要改的项；未提交的保持原值。
class CopilotSecurityPolicyIn(StrictInputModel):
    """策略局部更新（PATCH 语义）。"""

    enabled: Optional[bool] = None
    mode: Optional[PolicyMode] = None
    preset: Optional[str] = None
    kill_switch: Optional[bool] = None
    kill_switch_reason: Optional[str] = Field(default=None, max_length=200)
    governed_agents: Optional[List[str]] = None
    session_guard_surfaces: Optional[List[str]] = None
    ip_allowlist: Optional[List[str]] = None
    trust_proxy_headers: Optional[bool] = None
    bind_session_to_ip: Optional[bool] = None
    session_idle_timeout_seconds: Optional[int] = None
    session_absolute_timeout_seconds: Optional[int] = None
    max_message_chars: Optional[int] = None
    injection_scan_enabled: Optional[bool] = None
    injection_block_threshold: Optional[int] = None
    injection_warn_threshold: Optional[int] = None
    injection_blocked_surfaces: Optional[List[str]] = None
    redaction_enabled: Optional[bool] = None
    redact_in_audit: Optional[bool] = None
    redaction_kinds: Optional[List[str]] = None
    tool_allowlist: Optional[List[str]] = None
    tool_denylist: Optional[List[str]] = None
    deny_shell: Optional[bool] = None
    deny_write_actions: Optional[bool] = None
    deny_production_config: Optional[bool] = None
    require_approval_for_writes: Optional[bool] = None
    dangerous_tools_require_approval: Optional[bool] = None
    max_tool_calls_per_turn: Optional[int] = None
    max_tool_calls_per_hour: Optional[int] = None
    audit_events_enabled: Optional[bool] = None
    alert_on_block: Optional[bool] = None
    step_up_window_seconds: Optional[int] = None

    def to_patch(self) -> Dict[str, Any]:
        """只返回显式提交的字段。"""
        return {key: value for key, value in self.model_dump(exclude_unset=True).items() if value is not None}


# ▎一键切换预设。
class CopilotSecurityPresetIn(StrictInputModel):
    """套用预设请求。"""

    preset: PresetName
    # 强制确认位：切到 max 时前端要求管理员勾选，避免误触。
    confirm: bool = False


# ▎紧急停机开关。
class CopilotSecurityKillSwitchIn(StrictInputModel):
    """紧急停机开关请求。"""

    enabled: bool
    reason: str = Field(default="", max_length=200)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="停机原因", allow_empty=True)


# ▎演练扫描：只做检测与脱敏，不落库、不拦截。
class CopilotSecurityScanIn(StrictInputModel):
    """注入检测演练请求。"""

    text: str = Field(min_length=1, max_length=20_000)


# ▎注入命中详情。
class InjectionHitOut(BaseModel):
    """单条注入规则命中。"""

    code: str
    category: str
    score: int
    description: str
    evidence: str = ""


# ▎注入检测结果。
class InjectionScanOut(BaseModel):
    """注入检测结果。"""

    score: int = 0
    hits: List[InjectionHitOut] = Field(default_factory=list)
    obfuscation: Dict[str, Any] = Field(default_factory=dict)
    normalized_chars: int = 0


# ▎脱敏命中详情。
class RedactionHitOut(BaseModel):
    """单条脱敏规则命中。"""

    kind: str
    count: int
    description: str


# ▎演练结果。
class CopilotSecurityScanOut(BaseModel):
    """演练扫描结果。"""

    injection: InjectionScanOut
    redacted: str
    redaction_hits: List[RedactionHitOut] = Field(default_factory=list)
    original_length: int = 0


# ▎单项安全检查。
class PostureCheckOut(BaseModel):
    """态势检查项。"""

    key: str
    title: str
    weight: int
    critical: bool
    passed: bool
    remediation: str = ""


# ▎事件计数。
class SecurityEventCountersOut(BaseModel):
    """事件计数窗口。"""

    window_hours: int = 24
    total: int = 0
    blocked: int = 0
    by_action: Dict[str, int] = Field(default_factory=dict)


# ▎态势摘要里的策略速览。
class PosturePolicyOut(BaseModel):
    """态势摘要内的策略速览。"""

    enabled: bool = True
    mode: str = "balanced"
    preset: str = "balanced"
    kill_switch: bool = False
    kill_switch_reason: str = ""
    governed_agents: List[str] = Field(default_factory=list)
    session_guard_surfaces: List[str] = Field(default_factory=list)
    ip_allowlist: List[str] = Field(default_factory=list)
    tool_allowlist: List[str] = Field(default_factory=list)
    tool_denylist: List[str] = Field(default_factory=list)


# ▎安全态势：评分 + 检查项 + 建议 + 事件计数。
class CopilotSecurityPostureOut(BaseModel):
    """小菱安全态势。"""

    score: int = 0
    grade: str = "E"
    checks: List[PostureCheckOut] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    critical_gaps: List[str] = Field(default_factory=list)
    events: SecurityEventCountersOut = Field(default_factory=SecurityEventCountersOut)
    policy: PosturePolicyOut = Field(default_factory=PosturePolicyOut)
    generated_at: str = ""


# ▎一条审计事件。
class CopilotSecurityEventOut(BaseModel):
    """安全事件条目。"""

    id: int
    kind: str
    status: str
    actor_id: Optional[int] = None
    actor_name: Optional[str] = None
    target: Optional[str] = None
    ip: Optional[str] = None
    detail: Optional[str] = None
    create_time: Optional[str] = None


# ▎事件分页。
class CopilotSecurityEventListOut(BaseModel):
    """安全事件分页结果。"""

    items: List[CopilotSecurityEventOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 0


# ▎下拉选项：事件类型。
class EventKindOptionOut(BaseModel):
    """事件类型选项。"""

    kind: str
    label: str


# ▎预设选项。
class PresetOptionOut(BaseModel):
    """预设选项。"""

    preset: str
    label: str


# ▎安全中心首屏聚合包：一次请求拿全静态目录 + 当前策略 + 态势。
class CopilotSecurityCenterOut(BaseModel):
    """安全中心首屏数据。"""

    policy: CopilotSecurityPolicyOut
    posture: CopilotSecurityPostureOut
    presets: List[PresetOptionOut] = Field(default_factory=list)
    event_kinds: List[EventKindOptionOut] = Field(default_factory=list)
    governed_agent: str = "admin_copilot"


# ▎策略保存结果。
class CopilotSecurityPolicySavedOut(BaseModel):
    """策略保存结果。"""

    policy: CopilotSecurityPolicyOut
    changed: List[str] = Field(default_factory=list)
    posture: CopilotSecurityPostureOut
