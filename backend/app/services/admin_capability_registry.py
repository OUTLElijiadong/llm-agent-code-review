"""Responses 管理 Agent 可调用的固定后台能力注册表。

模型只能选择 ``code`` 并提供业务参数；HTTP 方法和路径不暴露为
可自由填写的参数。具体参数契约从同一个 FastAPI OpenAPI operation 解析，
避免 Agent 工具与页面 API 的 Pydantic 契约漂移。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

READ = "read"
WRITE = "write"
CRITICAL = "critical"
_VALID_RISKS = {READ, WRITE, CRITICAL}


@dataclass(frozen=True)
class AdminCapabilitySpec:
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
        if not self.path.startswith("/api/"):
            raise ValueError(f"管理能力必须绑定 /api 路径: {self.path}")
        object.__setattr__(self, "method", method)


def _cap(
    code: str,
    page: str,
    description: str,
    method: str,
    path: str,
    risk: str = READ,
    permission: Optional[str] = None,
) -> AdminCapabilitySpec:
    return AdminCapabilitySpec(code, page, description, method, path, risk, permission)


ADMIN_PAGE_ROUTES: tuple[str, ...] = (
    "/admin/overview",
    "/admin/agents",
    "/admin/approvals",
    "/admin/agent-releases",
    "/admin/beta-codes",
    "/admin/policies",
    "/admin/tools",
    "/admin/knowledge",
    "/admin/jobs",
    "/admin/observability",
    "/admin/rewards",
    "/admin/rollback",
    "/admin/users",
    "/admin/rbac/roles",
    "/admin/rbac/permissions",
    "/admin/rbac/users",
    "/admin/ai-logs",
    "/admin/audit",
    "/admin/evolution",
    "/admin/skills",
    "/admin/embedding",
    "/admin/llm",
    "/admin/mcp-workers",
    "/admin/report-templates",
)


ADMIN_CAPABILITIES: tuple[AdminCapabilitySpec, ...] = (
    # 总览大屏
    _cap(
        "overview.system",
        "/admin/overview",
        "查询系统运行状态",
        "GET",
        "/api/admin/overview/system",
        permission="server_ops:view",
    ),
    _cap(
        "overview.security",
        "/admin/overview",
        "查询安全态势",
        "GET",
        "/api/admin/overview/security",
        permission="security:view",
    ),
    _cap(
        "overview.geo",
        "/admin/overview",
        "查询登录来源地理分布",
        "GET",
        "/api/admin/overview/geo",
        permission="security:view",
    ),
    _cap(
        "overview.agent_activity",
        "/admin/overview",
        "查询 Agent 活跃状态",
        "GET",
        "/api/admin/overview/agents-activity",
        permission="agent:view",
    ),
    # Agent 治理
    _cap(
        "governance.overview",
        "/admin/agents",
        "查询 Agent 治理总览",
        "GET",
        "/api/admin/governance/overview",
        permission="agent:view",
    ),
    _cap(
        "governance.agents.list",
        "/admin/agents",
        "查询 Agent 治理档案",
        "GET",
        "/api/admin/governance/agents",
        permission="agent:view",
    ),
    _cap(
        "governance.agents.get",
        "/admin/agents",
        "查询单个 Agent 治理档案",
        "GET",
        "/api/admin/governance/agents/{agent_code}",
        permission="agent:view",
    ),
    _cap(
        "governance.agents.update",
        "/admin/agents",
        "更新 Agent 状态、预算、优先级和自动审批阈值",
        "PUT",
        "/api/admin/governance/agents/{agent_code}",
        CRITICAL,
        "agent:configure",
    ),
    # 通用审批
    _cap("approvals.list", "/admin/approvals", "查询审批事项", "GET", "/api/admin/approvals", permission="agent:view"),
    _cap(
        "approvals.approve",
        "/admin/approvals",
        "批准审批事项",
        "POST",
        "/api/admin/approvals/{item_id}/approve",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "approvals.reject",
        "/admin/approvals",
        "驳回审批事项",
        "POST",
        "/api/admin/approvals/{item_id}/reject",
        CRITICAL,
        "agent:configure",
    ),
    # Agent 发布
    _cap(
        "agent_releases.approvals.list",
        "/admin/agent-releases",
        "查询 Agent 发布审批",
        "GET",
        "/api/admin/agent-releases",
        permission="agent_asset:approve",
    ),
    _cap(
        "agent_releases.agents.list",
        "/admin/agent-releases",
        "查询已发布 Agent 与版本历史",
        "GET",
        "/api/admin/agent-releases/agents",
        permission="agent_asset:approve",
    ),
    _cap(
        "agent_releases.revise",
        "/admin/agent-releases",
        "创建管理员修订版",
        "POST",
        "/api/admin/agent-releases/{approval_id}/revise",
        CRITICAL,
        "agent_asset:approve",
    ),
    _cap(
        "agent_releases.approve",
        "/admin/agent-releases",
        "批准并发布 Agent",
        "POST",
        "/api/admin/agent-releases/{approval_id}/approve",
        CRITICAL,
        "agent_asset:approve",
    ),
    _cap(
        "agent_releases.reject",
        "/admin/agent-releases",
        "驳回 Agent 发布",
        "POST",
        "/api/admin/agent-releases/{approval_id}/reject",
        CRITICAL,
        "agent_asset:approve",
    ),
    _cap(
        "agent_releases.disable",
        "/admin/agent-releases",
        "停用已发布 Agent",
        "POST",
        "/api/admin/agent-releases/agents/{agent_id}/disable",
        CRITICAL,
        "agent_asset:disable",
    ),
    _cap(
        "agent_releases.rollback",
        "/admin/agent-releases",
        "回滚已发布 Agent",
        "POST",
        "/api/admin/agent-releases/agents/{agent_id}/rollback/{release_id}",
        CRITICAL,
        "agent_asset:rollback",
    ),
    # 内测码
    _cap("beta_codes.list", "/admin/beta-codes", "查询内测码", "GET", "/api/admin/beta-codes", permission="user:view"),
    _cap(
        "beta_codes.generate",
        "/admin/beta-codes",
        "生成一次性内测码",
        "POST",
        "/api/admin/beta-codes",
        CRITICAL,
        "user:create",
    ),
    _cap(
        "beta_codes.revoke",
        "/admin/beta-codes",
        "撤销内测码",
        "POST",
        "/api/admin/beta-codes/{invite_id}/revoke",
        CRITICAL,
        "user:update",
    ),
    _cap(
        "beta_codes.delete",
        "/admin/beta-codes",
        "删除内测码记录",
        "DELETE",
        "/api/admin/beta-codes/{invite_id}",
        CRITICAL,
        "user:update",
    ),
    # 策略
    _cap("policies.list", "/admin/policies", "查询治理策略", "GET", "/api/admin/policies", permission="agent:view"),
    _cap(
        "policies.upsert",
        "/admin/policies",
        "新增或更新治理策略",
        "POST",
        "/api/admin/policies",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "policies.evaluate",
        "/admin/policies",
        "试算治理策略并记录决策",
        "POST",
        "/api/admin/policies/evaluate",
        WRITE,
        "agent:configure",
    ),
    _cap(
        "policies.decisions.list",
        "/admin/policies",
        "查询策略决策记录",
        "GET",
        "/api/admin/policies/decisions",
        permission="agent:view",
    ),
    # 工具权限
    _cap(
        "tools.calls.list",
        "/admin/tools",
        "查询全局工具调用记录",
        "GET",
        "/api/admin/tools/calls",
        permission="audit:view",
    ),
    _cap(
        "tools.permissions.list",
        "/admin/tools",
        "查询 Agent 工具权限",
        "GET",
        "/api/admin/tools/permissions",
        permission="agent:view",
    ),
    _cap(
        "tools.permissions.upsert",
        "/admin/tools",
        "新增或更新 Agent 工具权限",
        "POST",
        "/api/admin/tools/permissions",
        CRITICAL,
        "agent:configure",
    ),
    # 知识与记忆
    _cap(
        "knowledge.memory.list",
        "/admin/knowledge",
        "查询 Agent 记忆",
        "GET",
        "/api/admin/governance/agents/{agent_code}/memory",
        permission="agent:view",
    ),
    _cap(
        "knowledge.memory.create",
        "/admin/knowledge",
        "沉淀 Agent 记忆",
        "POST",
        "/api/admin/governance/agents/{agent_code}/memory",
        WRITE,
        "agent:configure",
    ),
    _cap(
        "knowledge.docs.list",
        "/admin/knowledge",
        "查询 Agent 知识文档",
        "GET",
        "/api/admin/governance/agents/{agent_code}/knowledge",
        permission="agent:view",
    ),
    _cap(
        "knowledge.docs.create",
        "/admin/knowledge",
        "创建 Agent 知识文档",
        "POST",
        "/api/admin/governance/knowledge/docs",
        WRITE,
        "agent:configure",
    ),
    _cap(
        "knowledge.docs.activate",
        "/admin/knowledge",
        "激活 Agent 知识文档",
        "POST",
        "/api/admin/governance/knowledge/docs/{doc_id}/activate",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "knowledge.sources.list",
        "/admin/knowledge",
        "查询 Agent 知识来源",
        "GET",
        "/api/admin/governance/knowledge/sources",
        permission="agent:view",
    ),
    _cap(
        "knowledge.sources.upsert",
        "/admin/knowledge",
        "新增或更新 Agent 知识来源",
        "POST",
        "/api/admin/governance/knowledge/sources",
        WRITE,
        "server_ops:execute",
    ),
    _cap(
        "knowledge.sources.crawl",
        "/admin/knowledge",
        "抓取已配置知识来源",
        "POST",
        "/api/admin/governance/knowledge/crawl",
        CRITICAL,
        "server_ops:execute",
    ),
    # 任务调度
    _cap("jobs.list", "/admin/jobs", "查询 Agent 调度任务", "GET", "/api/admin/jobs", permission="agent:view"),
    _cap(
        "jobs.update",
        "/admin/jobs",
        "更新 Agent 调度任务",
        "PUT",
        "/api/admin/jobs/{job_id}",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "jobs.run",
        "/admin/jobs",
        "立即运行 Agent 调度任务",
        "POST",
        "/api/admin/jobs/{job_id}/run",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "jobs.runs.list",
        "/admin/jobs",
        "查询 Agent 任务运行历史",
        "GET",
        "/api/admin/jobs/runs",
        permission="agent:view",
    ),
    # 监控、奖惩、治理回滚
    _cap(
        "observability.overview",
        "/admin/observability",
        "查询 Agent 可观测总览",
        "GET",
        "/api/admin/observability/overview",
        permission="agent:view",
    ),
    _cap(
        "observability.alerts.list",
        "/admin/observability",
        "查询 Agent 告警",
        "GET",
        "/api/admin/observability/alerts",
        permission="agent:view",
    ),
    _cap(
        "observability.alerts.resolve",
        "/admin/observability",
        "解决 Agent 告警",
        "POST",
        "/api/admin/observability/alerts/{alert_id}/resolve",
        WRITE,
        "agent:configure",
    ),
 _cap(
        "observability.security.unread",
        "/admin/observability",
        "查询当前管理员未读安全告警",
        "GET",
        "/api/admin/observability/alerts/unread",
        permission="security:view",
    ),

_cap(
        "observability.security.read",
        "/admin/observability",
        "标记安全告警已读",
        "POST",
        "/api/admin/observability/alerts/{alert_id}/read",
        permission="security:view",
    ),

_cap(
        "observability.security.run_monitor",
        "/admin/observability",
        "手动触发一轮安全巡检",
        "POST",
        "/api/admin/observability/security/run-monitor",
        WRITE,
        "server_ops:view",
    ),

_cap(
        "observability.security.status",
        "/admin/observability",
        "查询安全态势聚合",
        "GET",
        "/api/admin/observability/security/status",
        permission="security:view",
    ),
    _cap(
        "rewards.events.list",
        "/admin/rewards",
        "查询 Agent 奖惩事件",
        "GET",
        "/api/admin/rewards/events",
        permission="agent:view",
    ),
    _cap(
        "rewards.events.create",
        "/admin/rewards",
        "记录 Agent 奖惩事件",
        "POST",
        "/api/admin/rewards/events",
        WRITE,
        "agent:configure",
    ),
    _cap(
        "rollback.versions.list",
        "/admin/rollback",
        "查询 Agent 治理制品版本",
        "GET",
        "/api/admin/rollback/versions",
        permission="agent:view",
    ),
    _cap(
        "rollback.versions.create",
        "/admin/rollback",
        "创建 Agent 治理制品版本",
        "POST",
        "/api/admin/rollback/versions",
        WRITE,
        "agent:configure",
    ),
    _cap(
        "rollback.versions.rollback",
        "/admin/rollback",
        "回滚 Agent 治理制品",
        "POST",
        "/api/admin/rollback/versions/{version_id}/rollback",
        CRITICAL,
        "agent:configure",
    ),
    # 用户
    _cap("users.list", "/admin/users", "查询用户", "GET", "/api/users", permission="user:view"),
    _cap(
        "users.reset_password",
        "/admin/users",
        "重置用户密码",
        "POST",
        "/api/users/{user_id}/reset-password",
        CRITICAL,
        "user:update",
    ),
    _cap(
        "users.toggle_status",
        "/admin/users",
        "启用或禁用用户",
        "POST",
        "/api/users/{user_id}/toggle-status",
        CRITICAL,
        "user:update",
    ),
    _cap(
        "users.set_legacy_role",
        "/admin/users",
        "设置用户历史单角色",
        "POST",
        "/api/users/{user_id}/role",
        CRITICAL,
        "user:update",
    ),
    _cap("users.delete", "/admin/users", "软删除用户", "DELETE", "/api/users/{user_id}", CRITICAL, "user:delete"),
    # RBAC 角色、权限与用户绑定
    _cap("rbac.roles.list", "/admin/rbac/roles", "查询 RBAC 角色", "GET", "/api/rbac/roles", permission="role:manage"),
    _cap(
        "rbac.roles.create", "/admin/rbac/roles", "创建 RBAC 角色", "POST", "/api/rbac/roles", CRITICAL, "role:manage"
    ),
    _cap(
        "rbac.roles.update",
        "/admin/rbac/roles",
        "更新 RBAC 角色",
        "PUT",
        "/api/rbac/roles/{role_id}",
        CRITICAL,
        "role:manage",
    ),
    _cap(
        "rbac.roles.delete",
        "/admin/rbac/roles",
        "删除非内置 RBAC 角色",
        "DELETE",
        "/api/rbac/roles/{role_id}",
        CRITICAL,
        "role:manage",
    ),
    _cap(
        "rbac.roles.permissions.get",
        "/admin/rbac/roles",
        "查询角色权限",
        "GET",
        "/api/rbac/roles/{role_id}/permissions",
        permission="role:manage",
    ),
    _cap(
        "rbac.roles.permissions.assign",
        "/admin/rbac/roles",
        "覆盖分配角色权限",
        "PUT",
        "/api/rbac/roles/{role_id}/permissions",
        CRITICAL,
        "role:manage",
    ),
    _cap(
        "rbac.roles.data_scope.get",
        "/admin/rbac/roles",
        "查询角色数据范围",
        "GET",
        "/api/rbac/roles/{role_id}/data-scope",
        permission="role:manage",
    ),
    _cap(
        "rbac.roles.data_scope.update",
        "/admin/rbac/roles",
        "更新角色数据范围",
        "PUT",
        "/api/rbac/roles/{role_id}/data-scope",
        CRITICAL,
        "role:manage",
    ),
    _cap(
        "rbac.roles.users",
        "/admin/rbac/roles",
        "按角色查询用户",
        "GET",
        "/api/rbac/roles/{role_code}/users",
        permission="role:manage",
    ),
    _cap(
        "rbac.roles.projects.list",
        "/admin/rbac/roles",
        "查询数据范围可选项目",
        "GET",
        "/api/projects",
        permission="project:view",
    ),
    _cap(
        "rbac.permissions.list",
        "/admin/rbac/permissions",
        "查询权限点",
        "GET",
        "/api/rbac/permissions",
        permission="role:manage",
    ),
    _cap(
        "rbac.menus.list",
        "/admin/rbac/permissions",
        "查询 RBAC 菜单树",
        "GET",
        "/api/rbac/menus",
        permission="menu:manage",
    ),
    _cap(
        "rbac.users.roles.assign",
        "/admin/rbac/users",
        "覆盖分配用户 RBAC 角色",
        "POST",
        "/api/rbac/users/{user_id}/roles",
        CRITICAL,
        "role:manage",
    ),
    _cap(
        "rbac.users.roles.get",
        "/admin/rbac/users",
        "查询用户 RBAC 角色",
        "GET",
        "/api/rbac/users/{user_id}/roles",
        permission="user:view",
    ),
    _cap(
        "rbac.users.permissions.get",
        "/admin/rbac/users",
        "查询用户有效权限",
        "GET",
        "/api/rbac/users/{user_id}/permissions",
        permission="user:view",
    ),
    _cap(
        "rbac.users.menus.get",
        "/admin/rbac/users",
        "查询用户可见菜单",
        "GET",
        "/api/rbac/users/{user_id}/menus",
        permission="user:view",
    ),
    _cap(
        "rbac.users.data_scope.get",
        "/admin/rbac/users",
        "查询用户有效数据范围",
        "GET",
        "/api/rbac/users/{user_id}/data-scope",
        permission="user:view",
    ),
    # 日志与审计
    _cap("ai_logs.list", "/admin/ai-logs", "查询 Agent 调用日志", "GET", "/api/ai-logs", permission="ai_log:view"),
    _cap(
        "ai_logs.get",
        "/admin/ai-logs",
        "查询 Agent 调用日志详情",
        "GET",
        "/api/ai-logs/{log_id}",
        permission="ai_log:view",
    ),
    _cap("audit.list", "/admin/audit", "查询系统操作审计", "GET", "/api/admin/audit", permission="audit:view"),
    # 进化
    _cap(
        "evolution.feedback",
        "/admin/evolution",
        "查询反馈信号总览",
        "GET",
        "/api/evolution/feedback",
        permission="agent:view",
    ),
    _cap(
        "evolution.experiences",
        "/admin/evolution",
        "查询进化经验记忆",
        "GET",
        "/api/evolution/experiences",
        permission="agent:view",
    ),
    _cap(
        "evolution.eval_cases",
        "/admin/evolution",
        "查询进化回归用例",
        "GET",
        "/api/evolution/eval-cases",
        permission="agent:view",
    ),
    _cap(
        "evolution.run",
        "/admin/evolution",
        "运行一轮全局进化",
        "POST",
        "/api/evolution/run",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "evolution.proposals.list",
        "/admin/evolution",
        "查询进化提案",
        "GET",
        "/api/evolution/proposals",
        permission="agent:view",
    ),
    _cap(
        "evolution.proposals.get",
        "/admin/evolution",
        "查询进化提案详情",
        "GET",
        "/api/evolution/proposals/{proposal_id}",
        permission="agent:view",
    ),
    _cap(
        "evolution.proposals.evaluate",
        "/admin/evolution",
        "评测进化提案",
        "POST",
        "/api/evolution/proposals/{proposal_id}/evaluate",
        WRITE,
        "agent:configure",
    ),
    _cap(
        "evolution.proposals.approve",
        "/admin/evolution",
        "批准进化提案",
        "POST",
        "/api/evolution/proposals/{proposal_id}/approve",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "evolution.proposals.reject",
        "/admin/evolution",
        "驳回进化提案",
        "POST",
        "/api/evolution/proposals/{proposal_id}/reject",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "evolution.proposals.rollback",
        "/admin/evolution",
        "回滚已批准的进化提案",
        "POST",
        "/api/evolution/proposals/{proposal_id}/rollback",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "evolution.trigger",
        "/admin/evolution",
        "触发指定 Agent 自进化 Skill",
        "POST",
        "/api/evolution/trigger",
        CRITICAL,
        "agent:configure",
    ),
    # Skill
    _cap(
        "skills.agents.list", "/admin/skills", "查询运行时 Agent", "GET", "/api/agents/runtime", permission="agent:view"
    ),
    _cap(
        "skills.list",
        "/admin/skills",
        "查询指定 Agent 的 Skill",
        "GET",
        "/api/agents/{agent_name}/skills",
        permission="agent:view",
    ),
    _cap(
        "skills.invoke",
        "/admin/skills",
        "手动调用指定 Agent Skill",
        "POST",
        "/api/agents/{agent_name}/skills/{skill_name}/invoke",
        CRITICAL,
        "agent:configure",
    ),
    _cap(
        "skills.records.list",
        "/admin/skills",
        "查询 Skill 调用记录",
        "GET",
        "/api/agents/skill-records",
        permission="agent:view",
    ),
    # 全局配置
    _cap(
        "embedding.config.get",
        "/admin/embedding",
        "查询脱敏的 RAG 嵌入配置",
        "GET",
        "/api/knowledge/embedding-config",
        permission="server_ops:view",
    ),
    _cap(
        "embedding.config.update",
        "/admin/embedding",
        "更新 RAG 嵌入配置",
        "PUT",
        "/api/knowledge/embedding-config",
        CRITICAL,
        "server_ops:execute",
    ),
    _cap(
        "embedding.config.reembed",
        "/admin/embedding",
        "按当前配置重建全部存量切片向量",
        "POST",
        "/api/knowledge/embedding-config/reembed",
        CRITICAL,
        "server_ops:execute",
    ),
    _cap(
        "llm.config.get",
        "/admin/llm",
        "查询脱敏的全局 LLM 配置",
        "GET",
        "/api/admin/llm/config",
        permission="server_ops:view",
    ),
    _cap(
        "llm.config.update",
        "/admin/llm",
        "更新并应用全局 LLM 配置",
        "PUT",
        "/api/admin/llm/config",
        CRITICAL,
        "server_ops:execute",
    ),
    _cap(
        "llm.config.test",
        "/admin/llm",
        "测试全局 LLM 连接",
        "POST",
        "/api/admin/llm/test",
        WRITE,
        "server_ops:execute",
    ),
    # MCP 与受控沙箱节点。所有能力均由 API 的 require_super_admin 再次强制校验；
    # 注册表仅向管理 Agent 暴露稳定能力码和 OpenAPI 参数契约。
    _cap(
        "mcp.servers.list", "/admin/mcp-workers", "查询 MCP Server", "GET", "/api/admin/mcp/servers",
        permission="server_ops:view",
    ),
    _cap(
        "mcp.servers.seed_recommended", "/admin/mcp-workers", "登记推荐 MCP Server", "POST",
        "/api/admin/mcp/servers/recommended", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "mcp.servers.create", "/admin/mcp-workers", "创建 MCP Server", "POST", "/api/admin/mcp/servers",
        CRITICAL, "server_ops:execute",
    ),
    _cap(
        "mcp.servers.update", "/admin/mcp-workers", "更新 MCP Server", "PUT",
        "/api/admin/mcp/servers/{server_id}", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "mcp.servers.delete", "/admin/mcp-workers", "删除 MCP Server", "DELETE",
        "/api/admin/mcp/servers/{server_id}", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "mcp.servers.health", "/admin/mcp-workers", "检查 MCP Server 健康状态", "POST",
        "/api/admin/mcp/servers/{server_id}/health", WRITE, "server_ops:execute",
    ),
    _cap(
        "mcp.servers.sync_tools", "/admin/mcp-workers", "同步 MCP 工具清单", "POST",
        "/api/admin/mcp/servers/{server_id}/sync", WRITE, "server_ops:execute",
    ),
    _cap(
        "mcp.tools.list", "/admin/mcp-workers", "查询 MCP 工具", "GET", "/api/admin/mcp/tools",
        permission="server_ops:view",
    ),
    _cap(
        "mcp.tools.update", "/admin/mcp-workers", "更新 MCP 工具风险与启用状态", "PUT",
        "/api/admin/mcp/tools/{tool_id}", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "mcp.bindings.list", "/admin/mcp-workers", "查询 Agent MCP 绑定", "GET", "/api/admin/mcp/bindings",
        permission="server_ops:view",
    ),
    _cap(
        "mcp.bindings.upsert", "/admin/mcp-workers", "配置 Agent MCP 工具绑定", "PUT",
        "/api/admin/mcp/bindings", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "mcp.bindings.delete", "/admin/mcp-workers", "删除 Agent MCP 工具绑定", "DELETE",
        "/api/admin/mcp/bindings/{binding_id}", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "mcp.aliases.list", "/admin/mcp-workers", "查询能力近义词别名", "GET", "/api/admin/mcp/aliases",
        permission="server_ops:view",
    ),
    _cap(
        "mcp.aliases.create", "/admin/mcp-workers", "创建能力近义词别名", "POST", "/api/admin/mcp/aliases",
        WRITE, "server_ops:execute",
    ),
    _cap(
        "mcp.aliases.update", "/admin/mcp-workers", "更新能力近义词别名", "PUT",
        "/api/admin/mcp/aliases/{alias_id}", WRITE, "server_ops:execute",
    ),
    _cap(
        "mcp.aliases.delete", "/admin/mcp-workers", "删除能力近义词别名", "DELETE",
        "/api/admin/mcp/aliases/{alias_id}", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "sandbox.workers.list", "/admin/mcp-workers", "查询沙箱 Worker", "GET", "/api/sandboxes/workers",
        permission="server_ops:view",
    ),
    _cap(
        "sandbox.workers.create", "/admin/mcp-workers", "创建远程或受控沙箱 Worker", "POST",
        "/api/sandboxes/workers", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "sandbox.workers.update", "/admin/mcp-workers", "更新沙箱 Worker", "PUT",
        "/api/sandboxes/workers/{worker_id}", CRITICAL, "server_ops:execute",
    ),
    _cap(
        "sandbox.workers.health", "/admin/mcp-workers", "检查沙箱 Worker 健康状态", "POST",
        "/api/sandboxes/workers/{worker_id}/health", WRITE, "server_ops:execute",
    ),
    _cap(
        "sandbox.workers.seed_production", "/admin/mcp-workers", "登记生产受控沙箱 Worker", "POST",
        "/api/sandboxes/workers/seed-production", CRITICAL, "server_ops:execute",
    ),
    # 管理侧栏中的报告模板页
    _cap(
        "report_templates.list",
        "/admin/report-templates",
        "查询报告模板",
        "GET",
        "/api/reports/templates",
        permission="report:template_manage",
    ),
    _cap(
        "report_templates.create",
        "/admin/report-templates",
        "创建报告模板",
        "POST",
        "/api/reports/templates",
        WRITE,
        "report:template_manage",
    ),
    _cap(
        "report_templates.update",
        "/admin/report-templates",
        "更新报告模板",
        "PUT",
        "/api/reports/templates/{template_id}",
        WRITE,
        "report:template_manage",
    ),
    _cap(
        "report_templates.delete",
        "/admin/report-templates",
        "删除报告模板",
        "DELETE",
        "/api/reports/templates/{template_id}",
        CRITICAL,
        "report:template_manage",
    ),
)


CAPABILITY_BY_CODE: dict[str, AdminCapabilitySpec] = {spec.code: spec for spec in ADMIN_CAPABILITIES}
if len(CAPABILITY_BY_CODE) != len(ADMIN_CAPABILITIES):
    raise RuntimeError("管理 Agent 能力编码重复")


@dataclass(frozen=True)
class OperationContract:
    schema: dict[str, Any]
    path_names: tuple[str, ...]
    query_names: tuple[str, ...]
    body_names: tuple[str, ...]


def _resolve_schema(schema: Any, openapi: Mapping[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    if isinstance(schema, list):
        return [_resolve_schema(item, openapi, seen) for item in schema]
    if not isinstance(schema, Mapping):
        return copy.deepcopy(schema)
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in seen or not ref.startswith("#/components/schemas/"):
            raise ValueError(f"不支持的 OpenAPI schema 引用: {ref}")
        name = ref.rsplit("/", 1)[-1]
        target = openapi.get("components", {}).get("schemas", {}).get(name)
        if not isinstance(target, Mapping):
            raise ValueError(f"OpenAPI schema 引用不存在: {ref}")
        merged = _resolve_schema(target, openapi, seen | {ref})
        if not isinstance(merged, dict):
            return merged
        for key, value in schema.items():
            if key != "$ref":
                merged[key] = _resolve_schema(value, openapi, seen)
        return merged
    return {str(key): _resolve_schema(value, openapi, seen) for key, value in schema.items()}


def operation_contract(spec: AdminCapabilitySpec, openapi: Mapping[str, Any]) -> OperationContract:
    operation = openapi.get("paths", {}).get(spec.path, {}).get(spec.method.lower())
    if not isinstance(operation, Mapping):
        raise ValueError(f"能力 {spec.code} 绑定的 API 不存在: {spec.method} {spec.path}")

    properties: dict[str, Any] = {}
    required: list[str] = []
    path_names: list[str] = []
    query_names: list[str] = []
    for raw in operation.get("parameters", []):
        parameter = _resolve_schema(raw, openapi)
        if not isinstance(parameter, Mapping):
            continue
        location = str(parameter.get("in") or "")
        if location not in {"path", "query"}:
            continue
        name = str(parameter.get("name") or "")
        if not name or name in properties:
            raise ValueError(f"能力 {spec.code} 参数名冲突: {name}")
        properties[name] = _resolve_schema(parameter.get("schema", {}), openapi)
        (path_names if location == "path" else query_names).append(name)
        if bool(parameter.get("required")):
            required.append(name)

    body_names: list[str] = []
    request_body = operation.get("requestBody")
    if isinstance(request_body, Mapping):
        content = request_body.get("content", {}).get("application/json", {})
        body_schema = _resolve_schema(content.get("schema", {}), openapi)
        if not isinstance(body_schema, Mapping) or body_schema.get("type") != "object":
            raise ValueError(f"能力 {spec.code} 只支持 JSON object 请求体")
        body_properties = body_schema.get("properties", {})
        if not isinstance(body_properties, Mapping):
            raise ValueError(f"能力 {spec.code} 请求体属性契约无效")
        for name, value in body_properties.items():
            if name in properties:
                raise ValueError(f"能力 {spec.code} 查询/请求体参数名冲突: {name}")
            properties[str(name)] = copy.deepcopy(value)
            body_names.append(str(name))
        required.extend(str(name) for name in body_schema.get("required", []))

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(dict.fromkeys(required))
    return OperationContract(
        schema=schema,
        path_names=tuple(path_names),
        query_names=tuple(query_names),
        body_names=tuple(body_names),
    )


def describe_capabilities(
    openapi: Mapping[str, Any],
    *,
    page: str = "",
    query: str = "",
    specs: Iterable[AdminCapabilitySpec] = ADMIN_CAPABILITIES,
) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    rows: list[dict[str, Any]] = []
    for spec in specs:
        if page and spec.page != page:
            continue
        haystack = f"{spec.code} {spec.page} {spec.description}".lower()
        if needle and needle not in haystack:
            continue
        contract = operation_contract(spec, openapi)
        rows.append(
            {
                "capability": spec.code,
                "page": spec.page,
                "description": spec.description,
                "risk": spec.risk,
                "permission": spec.permission or "admin",
                "parameters": contract.schema,
            }
        )
    return rows


def discovery_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "admin_describe_capabilities",
        "description": "查询管理页面的可用能力与精确参数契约；执行管理动作前先调用本工具",
        "parameters": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "enum": list(ADMIN_PAGE_ROUTES), "description": "管理页路由"},
                "query": {"type": "string", "description": "按能力编码、页面或中文描述过滤"},
            },
            "additionalProperties": False,
        },
    }


def execution_tool_schema(specs: Iterable[AdminCapabilitySpec] = ADMIN_CAPABILITIES) -> dict[str, Any]:
    return {
        "type": "function",
        "name": "admin_execute_capability",
        "description": (
            "执行已注册的管理页面真实 API 能力；必须先用 admin_describe_capabilities "
            "获得参数契约，写操作会暂停等待批准"
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
