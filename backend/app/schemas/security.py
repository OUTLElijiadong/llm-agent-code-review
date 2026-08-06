"""安全审计模块 Pydantic Schema (v2.1)"""
from typing import List, Optional

from pydantic import BaseModel, Field

# ---- Input ----


class SecurityScanFileIn(BaseModel):
    file_id: int = Field(..., description="代码文件 ID")
    scan_depth: str = Field("standard", description="quick / standard / deep")


class SecurityScanTaskIn(BaseModel):
    task_id: int = Field(..., description="审查任务 ID")


class SecurityScanProjectIn(BaseModel):
    project_id: int = Field(..., description="项目 ID")
    scan_mode: str = Field(
        "static_full",
        pattern="^(full|static_full|triage)$",
        description="full=全量语义, static_full=整包静态+有界语义, triage=风险优先抽样",
    )
    top_n: int = Field(50, ge=1, le=200, description="triage 模式最多扫描的文件数; full 模式忽略")
    trace_dataflow: bool = Field(True, description="是否启用跨文件数据流追踪")


class SecurityScanAllProjectsIn(BaseModel):
    top_n_per_project: int = Field(50, ge=1, le=200, description="每个项目最多扫描的文件数")
    trace_dataflow: bool = Field(True, description="是否启用跨文件数据流追踪")


class FullChainAuditIn(BaseModel):
    """全链路源码审计输入 (v3.3)"""

    project_id: int = Field(..., description="项目 ID")
    top_n: int = Field(100, ge=1, le=200, description="语义审计候选文件数")
    trace_dataflow: bool = Field(True, description="是否启用跨文件数据流追踪")
    enable_sandbox: bool = Field(
        False, description="是否在验证阶段调用真实沙箱跑 PoC(需有在线 worker)",
    )


# ---- Output ----


class SecurityFindingOut(BaseModel):
    """单条安全发现"""

    title: str = Field(..., description="问题标题")
    category: str = Field("", description="问题类别")
    owasp: str = Field("", description="OWASP Top10 分类编号")
    cwe: str = Field("", description="CWE 编号")
    severity: str = Field(..., description="严重/高/中/低")
    file_path: str = Field("", description="文件路径")
    file_id: Optional[int] = Field(None, description="文件 ID(若已落库)")
    lines: str = Field("", description="问题区间,如 L23 或 L23-L40")
    line_number: int = Field(0, description="问题起始行")
    end_line: int = Field(0, description="问题结束行")
    evidence: str = Field("", description="证据片段(已脱敏)")
    exploit_scenario: str = Field("", description="攻击场景描述")
    fix_suggestion: str = Field("", description="修复建议")
    references: List[str] = Field(default_factory=list, description="参考链接")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="置信度")
    source: str = Field("llm", description="发现来源: regex / llm")


class DataFlowOut(BaseModel):
    """跨文件数据流"""

    from_loc: str = Field("", alias="from", description="入口位置")
    via: List[str] = Field(default_factory=list, description="中间经过的模块/函数")
    to: str = Field("", description="抵达的危险接收点")
    risk_type: str = Field("", description="攻击类型")
    severity: str = Field("中", description="严重度")

    model_config = {"populate_by_name": True}


class ApiEndpointOut(BaseModel):
    """接口扫描结果"""

    method: str = Field("", description="HTTP 方法")
    path: str = Field("", description="接口路径")
    file_path: str = Field("", description="定义接口的文件路径")
    line_number: int = Field(0, description="接口定义行号")
    handler: str = Field("", description="处理函数或回调")
    auth_hint: str = Field("", description="认证/授权线索")
    source: str = Field("", description="识别来源")


class CodeLinkOut(BaseModel):
    """接口到代码/危险接收点的联动关系"""

    from_loc: str = Field("", alias="from", description="起点")
    to: str = Field("", description="终点")
    relation: str = Field("", description="关系类型")
    risk_type: str = Field("", description="风险类型")
    severity: str = Field("中", description="严重度")

    model_config = {"populate_by_name": True}


class EntryPointOut(BaseModel):
    """外部输入入口"""

    file: str = ""
    function: str = ""
    line: int = 0
    risk: str = ""


class ThreatModelOut(BaseModel):
    """项目级威胁建模结果"""

    entry_points: List[EntryPointOut] = Field(default_factory=list)
    data_flows: List[DataFlowOut] = Field(default_factory=list)
    api_endpoints: List[ApiEndpointOut] = Field(default_factory=list)
    code_links: List[CodeLinkOut] = Field(default_factory=list)
    attack_surface_summary: str = ""


class DiscussionTurnOut(BaseModel):
    """多 Agent 讨论发言"""

    agent_code: str = ""
    agent_name: str = ""
    role: str = "agent"
    content: str = ""


class SecurityDiscussionOut(BaseModel):
    """多 Agent 讨论审查摘要"""

    mode: str = "multi_agent_summary"
    participants: List[str] = Field(default_factory=list)
    turns: List[DiscussionTurnOut] = Field(default_factory=list)
    consensus: str = ""
    action_items: List[str] = Field(default_factory=list)


class SecurityScanOut(BaseModel):
    """扫描总输出"""

    findings: List[SecurityFindingOut] = Field(default_factory=list)
    threat_model: Optional[ThreatModelOut] = None
    discussion: Optional[SecurityDiscussionOut] = None
    compliance: dict = Field(default_factory=dict)
    risk_score: int = Field(100, ge=0, le=100)
    summary: str = ""
    file_count: int = 0
    duration_ms: int = 0
    source_archive_sha256: str = ""
    source_archive_bytes: int = 0
    source_archive_filename: str = ""


# ---- Checklist ----


class SecurityChecklistItem(BaseModel):
    code: str
    name: str
    owasp: str = ""
    cwe: str = ""
    description: str = ""


class SecurityChecklistOut(BaseModel):
    """检查清单:OWASP Top10 + 内置正则规则 + 静态语义规则"""

    owasp_top10: List[SecurityChecklistItem] = Field(default_factory=list)
    secret_patterns: List[SecurityChecklistItem] = Field(default_factory=list)
    static_rules: List[SecurityChecklistItem] = Field(default_factory=list)


# ---- Dashboard 安全态势 (v2.1.1) ----


class OwaspHotspotOut(BaseModel):
    owasp: str
    count: int


class RiskyProjectOut(BaseModel):
    project_id: int
    project_name: str
    risk_score: Optional[int] = None
    severe_issues: int = 0
    high_issues: int = 0


class TrendPointOut(BaseModel):
    date: str
    severe: int = 0
    high: int = 0


class SecurityDashboardSummaryOut(BaseModel):
    """工作台安全态势汇总"""

    user_scope: str = Field("self", description="self / global(admin)")
    project_count: int = 0
    scanned_project_count: int = 0
    avg_risk_score: Optional[int] = None
    severe_issues_total: int = 0
    high_issues_total: int = 0
    medium_issues_total: int = 0
    low_issues_total: int = 0
    owasp_hotspots: List[OwaspHotspotOut] = Field(default_factory=list)
    top_risky_projects: List[RiskyProjectOut] = Field(default_factory=list)
    trend: List[TrendPointOut] = Field(default_factory=list)
