"""Orchestrator 固定工具的单一参数契约与 Planner JSON Schema 来源。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class FixedToolArguments(BaseModel):
    """固定工具参数基类：严格类型并拒绝未声明字段。"""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )


class NoArguments(FixedToolArguments):
    """不接受模型参数的固定工具契约。"""


class ListProjectsArguments(FixedToolArguments):
    """项目列表工具参数。"""

    keyword: str = Field(default="", description="项目名称关键字")
    language: str = Field(default="", description="编程语言过滤")
    status: str = Field(default="active", description="项目状态过滤")
    page: int = Field(default=1, description="页码")
    page_size: int = Field(default=20, description="每页数量")

    @model_validator(mode="before")
    @classmethod
    def normalize_query_aliases(cls, value: Any) -> Any:
        """把历史 project_query/query 显式迁移为 canonical keyword。"""
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        aliases = [name for name in ("project_query", "query") if name in normalized]
        if "keyword" in normalized and aliases:
            raise ValueError("keyword 与 project_query/query 不能同时提供")
        if len(aliases) > 1:
            raise ValueError("project_query 与 query 不能同时提供")
        if aliases:
            normalized["keyword"] = normalized.pop(aliases[0])
        return normalized


class CreateProjectArguments(FixedToolArguments):
    """创建项目工具参数。"""

    project_name: str = Field(description="项目名称")
    description: str = Field(default="", description="项目描述")
    language: str = Field(default="plaintext", description="主要编程语言")


class DeleteProjectArguments(FixedToolArguments):
    """删除项目工具参数。"""

    project_id: int = Field(description="项目 ID")


class UpdateProjectArguments(FixedToolArguments):
    """编辑项目元数据工具参数。"""

    project_id: int = Field(description="项目 ID")
    project_name: Optional[str] = Field(default=None, description="新的项目名称")
    description: Optional[str] = Field(default=None, description="新的项目描述")
    language: Optional[str] = Field(default=None, description="新的主语言")
    status: Optional[Literal["active", "archived"]] = Field(default=None, description="项目状态")


class ImportRemoteProjectArguments(FixedToolArguments):
    """远程源码归档导入工具参数。"""

    url: str = Field(description="公开 HTTPS 源码归档地址")
    project_name: str = Field(description="新项目名称")
    description: str = Field(default="", description="项目描述")
    language: Optional[str] = Field(default=None, description="主语言")
    audit_mode: bool = Field(default=False, description="是否作为隔离整包源码审计")


class DownloadProjectSourceArguments(FixedToolArguments):
    """项目源码归档下载工具参数。"""

    project_id: int = Field(description="项目 ID")


class StartReviewArguments(FixedToolArguments):
    """启动审查工具参数；用户身份只允许由 Orchestrator 注入。"""

    project_id: int = Field(description="项目 ID")
    file_ids: List[int] = Field(
        default_factory=list,
        description="可选待审查文件 ID；为空时由 Orchestrator 选择项目 active 文件",
    )
    review_type: str = Field(default="quick", description="审查类型")
    task_name: str = Field(default="", description="任务名称")


class ListReviewTasksArguments(FixedToolArguments):
    """审查任务列表工具参数。"""

    project_id: Optional[int] = Field(default=None, description="可选项目 ID")
    status: str = Field(default="", description="任务状态过滤")
    page: int = Field(default=1, description="页码")
    page_size: int = Field(default=20, description="每页数量")


class ListReviewIssuesArguments(FixedToolArguments):
    """审查问题列表工具参数。"""

    task_id: int = Field(description="审查任务 ID")
    severity: str = Field(default="", description="严重级别过滤")
    issue_type: str = Field(default="", description="问题类型过滤")
    page: int = Field(default=1, description="页码")
    page_size: int = Field(default=50, description="每页数量")


class ListCodeFilesArguments(FixedToolArguments):
    """代码文件列表工具参数。"""

    project_id: int = Field(description="项目 ID")
    language: str = Field(default="", description="编程语言过滤")
    page: int = Field(default=1, description="页码")
    page_size: int = Field(default=50, description="每页数量")


class ListReportsArguments(FixedToolArguments):
    """报告列表工具参数。"""

    project_id: Optional[int] = Field(default=None, description="可选项目 ID")
    page: int = Field(default=1, description="页码")
    page_size: int = Field(default=20, description="每页数量")


class DetectLanguageArguments(FixedToolArguments):
    """语言检测工具参数。"""

    project_name: str = Field(description="项目名称")
    description: str = Field(default="", description="项目描述")


class AnalyzeProjectArguments(FixedToolArguments):
    """项目结构分析工具参数。"""

    folder_name: str = Field(description="项目文件夹名称")
    file_names: List[str] = Field(description="项目文件名列表")


class ReviewCodeArguments(FixedToolArguments):
    """旧版单段代码审查工具参数。"""

    code: str = Field(description="待审查代码")
    rules: str = Field(description="审查规则文本")
    language: str = Field(description="编程语言")
    file_name: str = Field(default="", description="文件名")
    line_offset: int = Field(default=0, description="行号偏移")


class GenerateIssuePromptArguments(FixedToolArguments):
    """按问题生成 AI 修复提示词的参数。"""

    issue_id: int = Field(description="问题 ID")
    target_tool: str = Field(default="generic", description="目标编码工具")
    use_llm: bool = Field(default=True, description="是否使用 LLM 增强")


class GenerateTaskPromptArguments(FixedToolArguments):
    """按任务生成 AI 修复提示词的参数。"""

    task_id: int = Field(description="审查任务 ID")
    target_tool: str = Field(default="generic", description="目标编码工具")
    severity_filter: Optional[List[str]] = Field(default=None, description="严重级别过滤")
    use_llm: bool = Field(default=True, description="是否使用 LLM 增强")


class GenerateProjectPromptArguments(FixedToolArguments):
    """按项目生成 AI 修复提示词的参数。"""

    project_id: int = Field(description="项目 ID")
    target_tool: str = Field(default="generic", description="目标编码工具")
    top_n: int = Field(default=30, description="最多纳入的问题数")
    use_llm: bool = Field(default=True, description="是否使用 LLM 增强")


class AuditSecurityFileArguments(FixedToolArguments):
    """文件安全审计工具参数。"""

    file_id: int = Field(description="代码文件 ID")
    scan_depth: str = Field(default="standard", description="扫描深度")


class AuditSecurityTaskArguments(FixedToolArguments):
    """任务安全审计工具参数。"""

    task_id: int = Field(description="审查任务 ID")


class AuditSecurityProjectArguments(FixedToolArguments):
    """项目安全审计工具参数。"""

    project_id: int = Field(description="项目 ID")
    scan_mode: Literal["full", "static_full", "triage"] = Field(
        default="static_full",
        description="full=全量语义, static_full=整包静态加有界语义, triage=风险优先抽样",
    )
    top_n: int = Field(default=50, description="static_full/triage 的语义候选文件数")
    trace_dataflow: bool = Field(default=True, description="是否追踪数据流")


class RunProjectTestsArguments(FixedToolArguments):
    """在登记 worker 上运行固定测试配置，不接受任意命令。"""

    project_id: int = Field(gt=0, description="项目 ID")
    language: Literal["python", "node", "java", "go", "php"]
    test_mode: Literal["whitebox", "blackbox", "combined"] = "whitebox"
    worker_code: str = Field(default="", max_length=80)
    remote_target_url: str = Field(default="", max_length=500)
    remote_target_authorized: bool = Field(default=False)


class DeployProjectSandboxArguments(FixedToolArguments):
    """部署持续沙箱的固定参数。"""

    project_id: int = Field(gt=0, description="项目 ID")
    language: Literal["python", "node", "java", "go", "php"]
    ttl_hours: int = Field(default=72, ge=1, le=720)
    worker_code: str = Field(default="", max_length=80)


class SandboxIdArguments(FixedToolArguments):
    public_id: str = Field(pattern=r"^sbx_[0-9a-f]{24}$")


class ExtendSandboxArguments(SandboxIdArguments):
    hours: int = Field(ge=1, le=168)


class TriggerEvolutionArguments(FixedToolArguments):
    """Agent 自进化触发参数。"""

    agent_name: str = Field(default="evolution", description="目标 Agent 名称")
    window_days: int = Field(default=90, description="反馈统计窗口天数")


class ListAgentSkillsArguments(FixedToolArguments):
    """Agent Skill 元数据查询参数。"""

    agent_name: Optional[str] = Field(default=None, description="可选 Agent 名称；为空时返回全部")


# ── 管理员 AI 代管后台工具(仅管理员可用;写操作强制审批)──


class AdminListUsersArguments(FixedToolArguments):
    """管理员查询用户列表参数。"""

    keyword: str = Field(default="", description="用户名/昵称搜索关键字")
    role: str = Field(default="", description="角色过滤: admin/user/reviewer")
    page: int = Field(default=1, description="页码")
    page_size: int = Field(default=20, description="每页数量")


class AdminUserIdArguments(FixedToolArguments):
    """管理员按用户 ID 操作参数。"""

    user_id: int = Field(description="目标用户 ID")


class AdminDeleteUsersArguments(FixedToolArguments):
    """管理员一次性批量删除精确用户 ID 列表。"""

    user_ids: List[int] = Field(
        min_length=1,
        max_length=200,
        description="经查询和用户澄清后得到的精确用户 ID 列表；不得传列表序号",
    )

    @model_validator(mode="after")
    def validate_user_ids(self) -> "AdminDeleteUsersArguments":
        if any(user_id <= 0 for user_id in self.user_ids):
            raise ValueError("user_ids 只能包含正整数")
        if len(set(self.user_ids)) != len(self.user_ids):
            raise ValueError("user_ids 不能重复")
        return self


class AdminSetRoleArguments(FixedToolArguments):
    """管理员设置用户角色参数(敏感,强制审批)。"""

    user_id: int = Field(description="目标用户 ID")
    role: str = Field(description="目标角色: admin/user/reviewer")


class AdminToggleAgentArguments(FixedToolArguments):
    """管理员启停 Agent 参数(敏感,强制审批)。"""

    agent_code: str = Field(description="目标 Agent 编码")
    enable: bool = Field(description="True 启用 / False 停用")


class AdminListApprovalsArguments(FixedToolArguments):
    """管理员查询审批事项参数。"""

    status: str = Field(default="pending", description="状态过滤: pending/approved/rejected/auto_approved")


class SearchPublishedAgentsArguments(FixedToolArguments):
    """搜索当前用户可调用的已发布自定义 Agent。"""

    query: str = Field(default="", max_length=200, description="Agent 编码、名称、描述或自然语言能力意图")
    limit: int = Field(default=8, ge=1, le=20, description="最多返回的候选数量")


class InvokePublishedAgentArguments(FixedToolArguments):
    """调用一个已经精确确定的已发布自定义 Agent。"""

    agent_code: str = Field(min_length=1, max_length=80, description="搜索结果返回的精确 Agent 编码")
    code: str = Field(min_length=1, max_length=200_000, description="待审查代码")
    language: str = Field(default="plaintext", max_length=40, description="代码语言")
    file_name: str = Field(default="snippet.txt", max_length=255, description="文件名")
    rules: List[Dict[str, Any]] = Field(default_factory=list, max_length=100, description="附加审查规则")
    line_offset: int = Field(default=0, ge=0, le=10_000_000, description="起始行号偏移")
    experience: str = Field(default="", max_length=12_000, description="可选审查经验上下文")


class AdminReleaseApprovalsArguments(FixedToolArguments):
    """管理员查询自定义 Agent 发布审批详情。"""

    approval_id: Optional[int] = Field(default=None, gt=0, description="指定审批 ID；为空时返回列表")
    status: str = Field(default="pending", description="状态过滤；传空字符串表示全部状态")
    limit: int = Field(default=50, ge=1, le=100, description="最多返回数量")


class AdminDecideAgentReleaseArguments(FixedToolArguments):
    """管理员批准或驳回一个待处理的 Agent 发布审批。"""

    approval_id: int = Field(gt=0, description="目标发布审批 ID")
    decision: Literal["approve", "reject"] = Field(description="approve 批准发布，reject 驳回")
    note: str = Field(default="", max_length=500, description="审批说明")


@dataclass(frozen=True)
class FixedToolContract:
    """单个固定工具的参数模型、说明和上下文注入策略。"""

    name: str
    description: str
    arguments_model: Type[FixedToolArguments]
    inject_ctx: bool


class FixedToolArgumentError(ValueError):
    """固定工具参数校验失败，错误详情不包含原始输入值。"""

    def __init__(self, tool_name: str, issues: List[Dict[str, str]]):
        """保存经过脱敏的字段级校验问题。"""
        self.tool_name = tool_name
        self.issues = issues
        details = "; ".join(f"{item['field']}: {item['message']}" for item in issues)
        super().__init__(f"工具 {tool_name} 参数校验失败: {details}")


_FIXED_TOOL_CONTRACTS: Tuple[FixedToolContract, ...] = (
    FixedToolContract("list_agents", "列出平台注册的 Agent 元数据", NoArguments, False),
    FixedToolContract("list_projects", "分页查询当前用户可见项目", ListProjectsArguments, True),
    FixedToolContract("create_project", "创建代码审查项目", CreateProjectArguments, True),
    FixedToolContract("delete_project", "删除指定项目", DeleteProjectArguments, True),
    FixedToolContract("start_review", "为指定项目文件启动代码审查", StartReviewArguments, True),
    FixedToolContract("list_review_tasks", "分页查询审查任务", ListReviewTasksArguments, True),
    FixedToolContract("list_review_issues", "分页查询指定任务的问题", ListReviewIssuesArguments, True),
    FixedToolContract("list_code_files", "分页查询项目代码文件", ListCodeFilesArguments, True),
    FixedToolContract("dashboard_summary", "获取当前用户仪表盘摘要", NoArguments, True),
    FixedToolContract("list_rules", "列出可用审查规则", NoArguments, True),
    FixedToolContract("list_reports", "分页查询审查报告", ListReportsArguments, True),
    FixedToolContract("detect_language", "根据项目名称与描述检测语言", DetectLanguageArguments, False),
    FixedToolContract("analyze_project", "根据目录和文件名分析项目", AnalyzeProjectArguments, False),
    FixedToolContract("review_code", "按规则审查单段代码", ReviewCodeArguments, False),
    FixedToolContract(
        "generate_ai_prompt_for_issue",
        "为单个问题生成 AI 修复提示词",
        GenerateIssuePromptArguments,
        True,
    ),
    FixedToolContract(
        "generate_ai_prompt_for_task",
        "为审查任务生成 AI 修复提示词",
        GenerateTaskPromptArguments,
        True,
    ),
    FixedToolContract(
        "generate_ai_prompt_for_project",
        "为项目生成 AI 修复提示词",
        GenerateProjectPromptArguments,
        True,
    ),
    FixedToolContract("audit_security_for_file", "审计单个代码文件的安全风险", AuditSecurityFileArguments, True),
    FixedToolContract("audit_security_for_task", "审计单个审查任务的安全风险", AuditSecurityTaskArguments, True),
    FixedToolContract(
        "audit_security_for_project",
        "审计项目级安全风险与数据流",
        AuditSecurityProjectArguments,
        True,
    ),
    FixedToolContract(
        "run_project_tests",
        "调用测试验证 Agent 在隔离 worker 上执行项目级白盒、黑盒或组合测试",
        RunProjectTestsArguments,
        True,
    ),
    FixedToolContract(
        "deploy_project_sandbox",
        "调用沙箱部署 Agent 启动持续预览环境",
        DeployProjectSandboxArguments,
        True,
    ),
    FixedToolContract("close_sandbox", "调用沙箱部署 Agent 关闭环境", SandboxIdArguments, True),
    FixedToolContract("extend_sandbox", "调用沙箱部署 Agent 续期环境", ExtendSandboxArguments, True),
    FixedToolContract("trigger_evolution", "触发指定 Agent 的自进化", TriggerEvolutionArguments, True),
    FixedToolContract("list_agent_skills", "列出指定或全部 Agent Skill 元数据", ListAgentSkillsArguments, False),
    FixedToolContract(
        "search_published_agents",
        "按编码、名称、描述或能力意图模糊搜索当前用户可调用的已发布 Agent；候选不唯一时应调用 ask_user 让用户确认",
        SearchPublishedAgentsArguments,
        True,
    ),
    FixedToolContract(
        "invoke_published_agent",
        "使用搜索结果中的精确 agent_code 调用已发布自定义 Agent 完成代码审查",
        InvokePublishedAgentArguments,
        True,
    ),
    # ── 管理员 AI 代管后台(仅管理员;写操作强制审批)──
    FixedToolContract("admin_list_users", "管理员查询平台用户列表", AdminListUsersArguments, True),
    FixedToolContract("admin_list_roles", "管理员查询 RBAC 角色列表", NoArguments, True),
    FixedToolContract("admin_governance_overview", "管理员查询 Agent 治理观测概览", NoArguments, True),
    FixedToolContract("admin_list_agents", "管理员查询 Agent 配置档案", NoArguments, True),
    FixedToolContract("admin_list_approvals", "管理员查询审批事项", AdminListApprovalsArguments, True),
    FixedToolContract(
        "admin_list_agent_release_approvals",
        "管理员查询 Agent 发布审批的修改前后内容、依赖、测试证据与风险详情",
        AdminReleaseApprovalsArguments,
        True,
    ),
    FixedToolContract("admin_system_status", "超级管理员查询服务器运行状态", NoArguments, True),
    FixedToolContract("admin_set_user_role", "管理员申请修改用户角色(敏感,需审批)", AdminSetRoleArguments, True),
    FixedToolContract("admin_delete_user", "管理员申请删除用户(高危,需审批)", AdminUserIdArguments, True),
    FixedToolContract(
        "admin_delete_users",
        "管理员一次审批原子软删除精确 user_ids 列表；调用前必须先查询并澄清列表序号与用户 ID",
        AdminDeleteUsersArguments,
        True,
    ),
    FixedToolContract("admin_toggle_agent", "管理员申请启停某个 Agent(敏感,需审批)", AdminToggleAgentArguments, True),
    FixedToolContract(
        "admin_decide_agent_release",
        "管理员批准或驳回一个 Agent 发布审批(敏感,需 Responses 审批)",
        AdminDecideAgentReleaseArguments,
        True,
    ),
)
_FIXED_TOOL_BY_NAME: Dict[str, FixedToolContract] = {
    contract.name: contract for contract in _FIXED_TOOL_CONTRACTS
}


def get_fixed_tool_names() -> List[str]:
    """按稳定顺序返回全部固定工具名称。"""
    return [contract.name for contract in _FIXED_TOOL_CONTRACTS]


def get_fixed_tool_description(tool_name: str) -> str:
    """返回固定工具面向 Planner 的功能说明。"""
    return _get_contract(tool_name).description


def get_fixed_tool_schema(tool_name: str) -> Dict[str, Any]:
    """返回由执行参数模型直接生成的 JSON Schema 副本。"""
    schema = _get_contract(tool_name).arguments_model.model_json_schema()
    return deepcopy(schema)


def is_fixed_tool(tool_name: str) -> bool:
    """判断名称是否属于受支持的固定工具。"""
    return tool_name in _FIXED_TOOL_BY_NAME


def fixed_tool_accepts_ctx(tool_name: str) -> bool:
    """返回执行器是否应向目标固定方法注入 AgentContext。"""
    return _get_contract(tool_name).inject_ctx


def validate_fixed_tool_arguments(tool_name: str, arguments: Any) -> Dict[str, Any]:
    """严格校验固定工具参数并返回 canonical、仅显式字段字典。"""
    contract = _get_contract(tool_name)
    try:
        validated = contract.arguments_model.model_validate(arguments)
    except ValidationError as exc:
        raise FixedToolArgumentError(tool_name, _sanitize_validation_errors(exc)) from exc
    return validated.model_dump(exclude_unset=True)


def _get_contract(tool_name: str) -> FixedToolContract:
    """读取固定工具契约；未知名称以 KeyError 明确失败。"""
    try:
        return _FIXED_TOOL_BY_NAME[tool_name]
    except KeyError as exc:
        raise KeyError(f"未知固定工具: {tool_name}") from exc


def _sanitize_validation_errors(exc: ValidationError) -> List[Dict[str, str]]:
    """把 Pydantic 错误转换为不包含 input/context/url 的稳定中文摘要。"""
    messages = {
        "missing": "缺少必填参数",
        "extra_forbidden": "不允许的额外参数",
        "int_type": "必须是整数",
        "string_type": "必须是字符串",
        "bool_type": "必须是布尔值",
        "list_type": "必须是数组",
        "model_type": "参数必须是对象",
    }
    issues: List[Dict[str, str]] = []
    for item in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in item.get("loc", ())) or "参数"
        error_type = str(item.get("type", "validation_error"))
        message = messages.get(error_type, str(item.get("msg", "参数不合法")))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        issues.append({"field": location, "message": message, "type": error_type})
    return issues
