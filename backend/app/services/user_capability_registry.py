"""Responses 普通用户 Agent 可调用的声明式页面能力白名单。

模型只能选择固定 ``code`` 并提供业务参数。HTTP 方法和路径由
注册表锁定，参数契约则从同一 FastAPI OpenAPI operation 解析。
本注册表只收录普通用户界面使用的 JSON API；管理路由、流式路由、
二进制/多段上传和密码/API Key 参数路由不得通过通用执行器暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from app.services.admin_capability_registry import operation_contract

READ = "read"
WRITE = "write"
CRITICAL = "critical"
_VALID_RISKS = {READ, WRITE, CRITICAL}
_FORBIDDEN_PREFIXES = (
    "/api/admin",
    "/api/auth",
    "/api/rbac",
    "/api/users",
    "/api/agent-responses",
)


@dataclass(frozen=True)
class UserCapabilitySpec:
    code: str
    page: str
    description: str
    method: str
    path: str
    risk: str = READ
    permission: Optional[str] = None

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"不支持的 HTTP 方法: {self.method}")
        if self.risk not in _VALID_RISKS:
            raise ValueError(f"不支持的风险等级: {self.risk}")
        if not self.path.startswith("/api/") or self.path.startswith(_FORBIDDEN_PREFIXES):
            raise ValueError(f"用户能力路径不在白名单边界内: {self.path}")
        object.__setattr__(self, "method", method)


def _cap(
    code: str,
    page: str,
    description: str,
    method: str,
    path: str,
    risk: str = READ,
    permission: Optional[str] = None,
) -> UserCapabilitySpec:
    return UserCapabilitySpec(code, page, description, method, path, risk, permission)


USER_PAGE_ROUTES: tuple[str, ...] = (
    "/dashboard",
    "/projects",
    "/code",
    "/reviews",
    "/issues",
    "/reports",
    "/report/templates",
    "/rules",
    "/security",
    "/agents",
    "/agent-studio",
    "/forum",
    "/knowledge",
    "/support/maintenance",
    "/support/feedback",
    "/profile",
    "/profile/api-config",
)


USER_CAPABILITIES: tuple[UserCapabilitySpec, ...] = (
    # 工作台
    _cap("dashboard.summary", "/dashboard", "查询工作台汇总", "GET", "/api/dashboard/summary"),
    _cap("dashboard.risk_distribution", "/dashboard", "查询风险分布", "GET", "/api/dashboard/risk-distribution"),
    _cap("dashboard.issue_types", "/dashboard", "查询问题类型统计", "GET", "/api/dashboard/issue-type-statistics"),
    _cap("dashboard.score_trend", "/dashboard", "查询质量分趋势", "GET", "/api/dashboard/score-trend"),
    _cap("dashboard.review_frequency", "/dashboard", "查询审查频率", "GET", "/api/dashboard/review-frequency"),
    # 项目与成员
    _cap("projects.list", "/projects", "查询当前用户可见项目", "GET", "/api/projects", permission="project:view"),
    _cap("projects.get", "/projects", "查询项目详情", "GET", "/api/projects/{project_id}", permission="project:view"),
    _cap("projects.create", "/projects", "创建项目", "POST", "/api/projects", WRITE, "project:create"),
    _cap("projects.update", "/projects", "更新项目", "PUT", "/api/projects/{project_id}", WRITE, "project:update"),
    _cap(
        "projects.delete", "/projects", "删除项目", "DELETE", "/api/projects/{project_id}", CRITICAL, "project:delete"
    ),
    _cap(
        "projects.import_remote",
        "/projects",
        "导入公开 HTTPS 源码归档",
        "POST",
        "/api/projects/import-remote",
        WRITE,
        "project:import",
    ),
    _cap(
        "projects.audit_archive.status",
        "/projects",
        "查询隔离整包源码的存储、扫描与审计状态",
        "GET",
        "/api/projects/{project_id}/audit-source-archive",
        permission="project:view",
    ),
    _cap(
        "projects.audit_archive.result",
        "/projects",
        "查询与隔离原包摘要绑定的最近白盒审计报告",
        "GET",
        "/api/projects/{project_id}/audit-source-archive/result",
        permission="security:view",
    ),
    _cap("project_members.list", "/projects", "查询项目成员", "GET", "/api/projects/{project_id}/members"),
    _cap("project_members.add", "/projects", "添加项目成员", "POST", "/api/projects/{project_id}/members", WRITE),
    _cap(
        "project_members.update_role",
        "/projects",
        "更新项目成员角色",
        "PUT",
        "/api/projects/{project_id}/members/{user_id}",
        WRITE,
    ),
    _cap(
        "project_members.remove",
        "/projects",
        "移除项目成员",
        "DELETE",
        "/api/projects/{project_id}/members/{user_id}",
        CRITICAL,
    ),
    # 代码文件；二进制下载和 multipart 上传由专用端点处理，不伪装成 JSON 工具。
    _cap("code_files.list", "/code", "查询项目代码文件", "GET", "/api/code-files", permission="file:view"),
    _cap("code_files.create", "/code", "在线新建文本代码文件", "POST", "/api/code-files", WRITE, "file:upload"),
    _cap("code_files.get", "/code", "查询文本代码文件详情", "GET", "/api/code-files/{file_id}", permission="file:view"),
    _cap(
        "code_files.meta",
        "/code",
        "查询代码文件元数据",
        "GET",
        "/api/code-files/{file_id}/meta",
        permission="file:view",
    ),
    _cap(
        "code_files.update", "/code", "更新代码文件并创建新版本", "PUT", "/api/code-files/{file_id}", WRITE, "file:edit"
    ),
    _cap(
        "code_files.rename", "/code", "重命名代码文件", "POST", "/api/code-files/{file_id}/rename", WRITE, "file:edit"
    ),
    _cap("code_files.delete", "/code", "删除代码文件", "DELETE", "/api/code-files/{file_id}", CRITICAL, "file:delete"),
    _cap(
        "code_files.versions.list",
        "/code",
        "查询代码文件版本",
        "GET",
        "/api/code-files/{file_id}/versions",
        permission="file:view",
    ),
    _cap(
        "code_files.versions.get",
        "/code",
        "查询指定代码文件版本",
        "GET",
        "/api/code-files/{file_id}/versions/{version_no}",
        permission="file:view",
    ),
    _cap(
        "code_files.versions.restore",
        "/code",
        "回滚代码文件到指定版本",
        "POST",
        "/api/code-files/{file_id}/versions/{version_no}/restore",
        CRITICAL,
        "file:edit",
    ),
    # 审查任务与问题
    _cap("reviews.list", "/reviews", "查询审查任务", "GET", "/api/review/tasks", permission="review:view"),
    _cap("reviews.get", "/reviews", "查询审查任务详情", "GET", "/api/review/tasks/{task_id}", permission="review:view"),
    _cap(
        "reviews.issues",
        "/reviews",
        "查询审查任务问题",
        "GET",
        "/api/review/tasks/{task_id}/issues",
        permission="issue:view",
    ),
    _cap("reviews.start", "/reviews", "启动代码审查", "POST", "/api/review/start", WRITE, "review:start"),
    _cap(
        "reviews.cancel",
        "/reviews",
        "取消运行中的审查任务",
        "POST",
        "/api/review/tasks/{task_id}/cancel",
        WRITE,
        "review:cancel",
    ),
    _cap(
        "reviews.delete", "/reviews", "删除审查任务", "DELETE", "/api/review/tasks/{task_id}", CRITICAL, "review:cancel"
    ),
    _cap("issues.list", "/issues", "跨任务查询问题", "GET", "/api/issues", permission="issue:view"),
    _cap("issues.get", "/issues", "查询问题详情", "GET", "/api/issues/{issue_id}", permission="issue:view"),
    _cap(
        "issues.update_status", "/issues", "更新问题状态", "PUT", "/api/issues/{issue_id}/status", WRITE, "issue:handle"
    ),
    _cap(
        "issues.batch_update_status",
        "/issues",
        "批量更新问题状态",
        "POST",
        "/api/issues/batch-status",
        WRITE,
        "issue:batch",
    ),
    # 报告 JSON 管理路由；HTML/PDF/Word/源文件下载不经通用 JSON 执行器。
    _cap("reports.list", "/reports", "查询报告", "GET", "/api/reports"),
    _cap("reports.get", "/reports", "查询报告详情", "GET", "/api/reports/{task_id}"),
    _cap("reports.delete", "/reports", "删除报告", "DELETE", "/api/reports/{task_id}", CRITICAL),
    _cap(
        "report_templates.list",
        "/report/templates",
        "查询报告模板",
        "GET",
        "/api/reports/templates",
        permission="report:template_manage",
    ),
    _cap(
        "report_templates.create",
        "/report/templates",
        "创建报告模板",
        "POST",
        "/api/reports/templates",
        WRITE,
        "report:template_manage",
    ),
    _cap(
        "report_templates.update",
        "/report/templates",
        "更新报告模板",
        "PUT",
        "/api/reports/templates/{template_id}",
        WRITE,
        "report:template_manage",
    ),
    _cap(
        "report_templates.delete",
        "/report/templates",
        "删除报告模板",
        "DELETE",
        "/api/reports/templates/{template_id}",
        CRITICAL,
        "report:template_manage",
    ),
    # 规则和安全审计
    _cap("rules.list", "/rules", "查询审查规则", "GET", "/api/rules"),
    _cap("rules.create", "/rules", "创建审查规则", "POST", "/api/rules", WRITE),
    _cap("rules.update", "/rules", "更新审查规则", "PUT", "/api/rules/{rule_id}", WRITE),
    _cap("rules.toggle", "/rules", "启用或停用审查规则", "POST", "/api/rules/{rule_id}/toggle", WRITE),
    _cap("rules.delete", "/rules", "删除审查规则", "DELETE", "/api/rules/{rule_id}", CRITICAL),
    _cap(
        "security.checklist", "/security", "查询安全审查清单", "GET",
        "/api/security/checklist", permission="security:view",
    ),
    _cap(
        "security.dashboard", "/security", "查询安全态势汇总", "GET",
        "/api/security/dashboard-summary", permission="security:view",
    ),
    _cap(
        "security.findings", "/security", "查询已落库安全发现", "GET",
        "/api/security/findings", permission="security:view",
    ),
    _cap(
        "security.scan_file", "/security", "扫描单个代码文件", "POST",
        "/api/security/scan-file", WRITE, "security:scan",
    ),
    _cap(
        "security.scan_task", "/security", "复审审查任务的安全发现", "POST",
        "/api/security/scan-task", WRITE, "security:scan",
    ),
    _cap(
        "security.scan_project", "/security", "执行项目级白盒安全审计", "POST",
        "/api/security/scan-project", WRITE, "security:scan",
    ),
    _cap(
        "security.scan_all_projects",
        "/security",
        "扫描当前用户可审计的全部项目",
        "POST",
        "/api/security/scan-all-projects",
        WRITE,
        "security:scan",
    ),
    # Agent 中心和用户拥有的 Agent/Skill 工坊资产
    _cap("agents.list", "/agents", "查询 Agent 画像", "GET", "/api/agents", permission="agent:view"),
    _cap(
        "agents.type_mappings",
        "/agents",
        "查询审查类型与 Agent 映射",
        "GET",
        "/api/agents/type-mappings",
        permission="agent:view",
    ),
    _cap("agents.usage", "/agents", "查询 Agent 调用统计", "GET", "/api/agents/usage", permission="agent:view"),
    _cap("agents.overview", "/agents", "查询 Agent 中心总览", "GET", "/api/agents/overview", permission="agent:view"),
    _cap("agents.runtime", "/agents", "查询运行时 Agent", "GET", "/api/agents/runtime", permission="agent:view"),
    _cap(
        "agents.runtime_summary",
        "/agents",
        "查询运行时 Agent 汇总",
        "GET",
        "/api/agents/runtime/summary",
        permission="agent:view",
    ),
    _cap("agents.situation", "/agents", "查询 Agent 态势", "GET", "/api/agents/situation", permission="agent:view"),
    _cap(
        "agents.skills",
        "/agents",
        "查询指定 Agent 的 Skill",
        "GET",
        "/api/agents/{agent_name}/skills",
        permission="agent:view",
    ),
    _cap(
        "agents.metagpt_info",
        "/agents",
        "查询 MetaGPT 编排层信息",
        "GET",
        "/api/agents/metagpt/info",
        permission="agent:view",
    ),
    _cap(
        "agents.metagpt_preview",
        "/agents",
        "预览 MetaGPT 编排环境",
        "GET",
        "/api/agents/metagpt/preview",
        permission="agent:view",
    ),
    _cap(
        "agent_catalog.list",
        "/agents",
        "查询已发布 Agent 目录",
        "GET",
        "/api/agent-catalog",
        permission="custom_agent:invoke",
    ),
    _cap(
        "agent_catalog.invoke",
        "/agents",
        "调用已发布的自定义 Agent",
        "POST",
        "/api/agent-catalog/{agent_code}/invoke",
        READ,
        "custom_agent:invoke",
    ),
    _cap(
        "agent_studio.agents.list",
        "/agent-studio",
        "查询我创建的 Agent",
        "GET",
        "/api/agent-studio/agents",
        permission="agent_asset:update_own",
    ),
    _cap(
        "agent_studio.agents.create",
        "/agent-studio",
        "创建自定义 Agent",
        "POST",
        "/api/agent-studio/agents",
        WRITE,
        "agent_asset:create",
    ),
    _cap(
        "agent_studio.agent_versions.list",
        "/agent-studio",
        "查询 Agent 版本",
        "GET",
        "/api/agent-studio/agents/{agent_id}/versions",
        permission="agent_asset:update_own",
    ),
    _cap(
        "agent_studio.agent_versions.get",
        "/agent-studio",
        "查询 Agent 版本详情",
        "GET",
        "/api/agent-studio/agent-versions/{version_id}",
        permission="agent_asset:update_own",
    ),
    _cap(
        "agent_studio.agent_versions.create",
        "/agent-studio",
        "创建 Agent 修订版",
        "POST",
        "/api/agent-studio/agents/{agent_id}/versions",
        WRITE,
        "agent_asset:update_own",
    ),
    _cap(
        "agent_studio.agent_versions.bind_skill",
        "/agent-studio",
        "绑定 Skill 到 Agent 版本",
        "POST",
        "/api/agent-studio/agent-versions/{version_id}/skills",
        WRITE,
        "agent_asset:update_own",
    ),
    _cap(
        "agent_studio.agent_versions.test",
        "/agent-studio",
        "测试 Agent 版本",
        "POST",
        "/api/agent-studio/agent-versions/{version_id}/test",
        WRITE,
        "agent_asset:test",
    ),
    _cap(
        "agent_studio.agent_versions.submit",
        "/agent-studio",
        "提交 Agent 版本发布审批",
        "POST",
        "/api/agent-studio/agent-versions/{version_id}/submit",
        WRITE,
        "agent_asset:submit",
    ),
    _cap(
        "agent_studio.agent_versions.withdraw",
        "/agent-studio",
        "撤回 Agent 版本发布申请",
        "POST",
        "/api/agent-studio/agent-versions/{version_id}/withdraw",
        WRITE,
        "agent_asset:submit",
    ),
    _cap(
        "agent_studio.skills.list",
        "/agent-studio",
        "查询我创建的 Skill",
        "GET",
        "/api/agent-studio/skills",
        permission="skill_asset:update_own",
    ),
    _cap(
        "agent_studio.skills.create",
        "/agent-studio",
        "创建自定义 Skill",
        "POST",
        "/api/agent-studio/skills",
        WRITE,
        "skill_asset:create",
    ),
    _cap(
        "agent_studio.skill_versions.list",
        "/agent-studio",
        "查询 Skill 版本",
        "GET",
        "/api/agent-studio/skills/{skill_id}/versions",
        permission="skill_asset:update_own",
    ),
    _cap(
        "agent_studio.skill_versions.get",
        "/agent-studio",
        "查询 Skill 版本详情",
        "GET",
        "/api/agent-studio/skill-versions/{version_id}",
        permission="skill_asset:update_own",
    ),
    _cap(
        "agent_studio.skill_versions.create",
        "/agent-studio",
        "创建 Skill 修订版",
        "POST",
        "/api/agent-studio/skills/{skill_id}/versions",
        WRITE,
        "skill_asset:update_own",
    ),
    _cap(
        "agent_studio.bindings.delete",
        "/agent-studio",
        "解绑 Agent 版本的 Skill",
        "DELETE",
        "/api/agent-studio/bindings/{binding_id}",
        CRITICAL,
        "agent_asset:update_own",
    ),
    # 论坛、知识库和服务支持
    _cap("forum.posts.list", "/forum", "查询论坛帖子", "GET", "/api/forum/posts"),
    _cap("forum.posts.get", "/forum", "查询论坛帖子详情", "GET", "/api/forum/posts/{post_id}"),
    _cap("forum.posts.create", "/forum", "创建论坛帖子", "POST", "/api/forum/posts", WRITE),
    _cap("forum.posts.update", "/forum", "更新自己的论坛帖子", "PUT", "/api/forum/posts/{post_id}", WRITE),
    _cap("forum.posts.delete", "/forum", "删除自己的论坛帖子", "DELETE", "/api/forum/posts/{post_id}", CRITICAL),
    _cap("forum.replies.create", "/forum", "回复论坛帖子", "POST", "/api/forum/posts/{post_id}/replies", WRITE),
    _cap("forum.replies.delete", "/forum", "删除自己的论坛回复", "DELETE", "/api/forum/replies/{reply_id}", CRITICAL),
    _cap("forum.assist", "/forum", "使用知识库辅助撰写帖子", "POST", "/api/forum/assist", READ),
    _cap("knowledge.docs.list", "/knowledge", "查询个人知识文档", "GET", "/api/knowledge/docs"),
    _cap("knowledge.docs.create", "/knowledge", "创建个人知识文档", "POST", "/api/knowledge/docs", WRITE),
    _cap("knowledge.docs.delete", "/knowledge", "删除个人知识文档", "DELETE", "/api/knowledge/docs/{doc_id}", CRITICAL),
    _cap("knowledge.search", "/knowledge", "检索个人知识库", "POST", "/api/knowledge/search", READ),
    _cap("knowledge.sync", "/knowledge", "从当前用户可见平台数据同步知识库", "POST", "/api/knowledge/sync", WRITE),
    _cap("knowledge.stats", "/knowledge", "查询个人知识库统计", "GET", "/api/knowledge/stats"),
    _cap("maintenance.list", "/support/maintenance", "查询我的维修工单", "GET", "/api/maintenance"),
    _cap("maintenance.get", "/support/maintenance", "查询维修工单详情", "GET", "/api/maintenance/{ticket_id}"),
    _cap("maintenance.create", "/support/maintenance", "提交维修工单", "POST", "/api/maintenance", WRITE),
    _cap(
        "maintenance.close",
        "/support/maintenance",
        "关闭或撤销自己的维修工单",
        "POST",
        "/api/maintenance/{ticket_id}/close",
        WRITE,
    ),
    _cap("feedback.list", "/support/feedback", "查询我的反馈", "GET", "/api/feedback"),
    _cap("feedback.get", "/support/feedback", "查询反馈详情", "GET", "/api/feedback/{feedback_id}"),
    _cap("feedback.create", "/support/feedback", "提交用户反馈", "POST", "/api/feedback", WRITE),
    # 个人资料。密码和 API Key 明文操作不进入模型通用工具参数。
    _cap("profile.get", "/profile", "查询我的个人画像", "GET", "/api/me/profile"),
    _cap("profile.update", "/profile", "更新我的个人画像", "PUT", "/api/me/profile", WRITE),
    _cap("profile.relearn", "/profile", "重新学习我的个人画像", "POST", "/api/me/profile/relearn", WRITE),
    _cap("api_config.get", "/profile/api-config", "查询当前脱敏 API 配置", "GET", "/api/api-config"),
    _cap("api_config.delete", "/profile/api-config", "删除我的 API 配置", "DELETE", "/api/api-config", CRITICAL),
)


CAPABILITY_BY_CODE: dict[str, UserCapabilitySpec] = {spec.code: spec for spec in USER_CAPABILITIES}
if len(CAPABILITY_BY_CODE) != len(USER_CAPABILITIES):
    raise RuntimeError("用户 Agent 能力编码重复")


def describe_capabilities(
    openapi: Mapping[str, Any],
    *,
    page: str = "",
    query: str = "",
    specs: Iterable[UserCapabilitySpec] = USER_CAPABILITIES,
) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    rows: list[dict[str, Any]] = []
    for spec in specs:
        if page and spec.page != page:
            continue
        haystack = f"{spec.code} {spec.page} {spec.description}".lower()
        if needle and needle not in haystack:
            continue
        contract = operation_contract(spec, openapi)  # type: ignore[arg-type]
        rows.append(
            {
                "capability": spec.code,
                "page": spec.page,
                "description": spec.description,
                "risk": spec.risk,
                "permission": spec.permission or "route_enforced",
                "parameters": contract.schema,
            }
        )
    return rows


def discovery_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "user_describe_capabilities",
        "description": "查询普通用户页面的可用能力与精确参数契约；执行页面操作前先调用本工具",
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "enum": list(USER_PAGE_ROUTES), "description": "普通用户页路由"},
                "query": {"type": "string", "description": "按能力编码、页面或中文描述过滤"},
            },
            "additionalProperties": False,
        },
    }


def execution_tool_schema(specs: Iterable[UserCapabilitySpec] = USER_CAPABILITIES) -> dict[str, Any]:
    return {
        "type": "function",
        "name": "user_execute_capability",
        "description": (
            "以当前登录用户身份执行已注册的普通页面真实 JSON API 能力；"
            "必须先用 user_describe_capabilities 获得参数契约，写操作会暂停等待批准"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {"type": "string", "enum": [spec.code for spec in specs]},
                "params": {"type": "object", "description": "严格符合能力契约的参数"},
            },
            "required": ["capability", "params"],
            "additionalProperties": False,
        },
    }
