"""Agent 对话页面引导协议的服务端契约。

模型在回复里通过两种约定把用户带到对应页面、模拟真实页面操作:

1. 站内 markdown 链接 ``[标题](/路由)`` — 前端渲染为导航卡片,
   用户点击后 SPA 跳转;路由白名单与鉴权由前端同源守卫裁决。
2. 指令导航注释 ``<!--PRISM_NAVIGATE {"action":"navigate","route":"...","label":"..."}-->``
   — 当用户明确说"带我去/打开某页"或某操作完成后目标页面唯一时,
   回复末尾附加一条,前端解析为导航按钮并在完成时自动跳转。

本模块给出两份系统提示词共用的引导说明与合法路由清单,
路由清单与前端 ``router/index.ts`` 保持同步,避免模型编造路由。
"""

from __future__ import annotations

from app.services.admin_capability_registry import ADMIN_PAGE_ROUTES
from app.services.user_capability_registry import USER_PAGE_ROUTES

# 页面路由 → (中文名称, 一句话用途),供系统提示词告诉模型每个入口的语义。
USER_PAGE_LABELS: dict[str, tuple[str, str]] = {
    "/dashboard": ("工作台", "查看个人审查汇总、风险分布与趋势"),
    "/projects": ("项目管理", "新建/导入项目、查看项目列表"),
    "/code": ("代码中心", "浏览项目代码文件并在线编辑"),
    "/reviews": ("审查记录", "查看与发起审查任务"),
    "/reviews/start": ("发起审查", "按项目与文件发起新审查"),
    "/issues": ("问题追踪", "跨任务检索并闭环问题"),
    "/reports": ("审查报告", "查看与导出审查报告"),
    "/report/templates": ("报告模板管理", "维护报告模板"),
    "/rules": ("审查规则", "管理自定义审查规则"),
    "/security": ("安全中心", "安全审计清单、发现与扫描"),
    "/agents": ("Agent 中心", "查看 Agent 画像与运行态势"),
    "/agent-studio": ("Agent 工坊", "创建自定义 Agent 与 Skill"),
    "/sandboxes": ("代码沙箱", "创建测试沙箱与持续部署环境"),
    "/forum": ("开发者论坛", "发帖交流、回复讨论"),
    "/forum/new": ("发布新帖", "撰写并发布论坛帖子"),
    "/knowledge": ("个人知识库", "沉淀与检索个人知识"),
    "/support/maintenance": ("申请维修", "提交与跟进维修工单"),
    "/support/feedback": ("意见反馈", "提交与查看产品反馈"),
    "/profile": ("个人中心", "维护个人资料与画像"),
    "/profile/personalization": ("个性化画像", "查看画像学习与偏好"),
    "/profile/password": ("修改密码", "修改登录密码"),
    "/profile/api-config": ("API 配置", "维护个人模型 API 配置"),
}

ADMIN_PAGE_LABELS: dict[str, tuple[str, str]] = {
    "/admin/overview": ("总览大屏", "系统状态、安全态势与 Agent 活跃"),
    "/admin/agents": ("Agent 管理", "Agent 治理档案与配置"),
    "/admin/approvals": ("审批中心", "处理待审批事项"),
    "/admin/agent-releases": ("Agent 发布审批", "审批自定义 Agent 发布"),
    "/admin/beta-codes": ("内测码管理", "生成与撤销内测码"),
    "/admin/policies": ("策略中心", "治理策略与决策记录"),
    "/admin/tools": ("工具权限", "Agent 工具调用与权限"),
    "/admin/knowledge": ("知识与记忆", "Agent 记忆与知识治理"),
    "/admin/jobs": ("任务调度", "Agent 调度任务管理"),
    "/admin/observability": ("监控告警", "Agent 可观测与告警"),
    "/admin/rewards": ("奖惩趋势", "Agent 奖惩事件"),
    "/admin/rollback": ("回滚中心", "治理制品版本回滚"),
    "/admin/users": ("用户管理", "用户查询、启停与重置密码"),
    "/admin/rbac/roles": ("角色管理", "RBAC 角色与权限分配"),
    "/admin/rbac/permissions": ("权限点列表", "权限点与菜单树"),
    "/admin/rbac/users": ("用户角色分配", "用户 RBAC 角色绑定"),
    "/admin/ai-logs": ("Agent 调用日志", "AI 调用记录检索"),
    "/admin/report-templates": ("报告模板管理", "报告模板维护"),
    "/admin/audit": ("系统操作审计", "操作审计日志"),
    "/admin/evolution": ("Agent 自进化", "反馈信号与进化提案"),
    "/admin/skills": ("Skill 管理", "Skill 清单与调用记录"),
    "/admin/embedding": ("RAG 嵌入配置", "嵌入模型配置"),
    "/admin/mcp-workers": ("MCP 与沙箱节点", "MCP Server 与沙箱 Worker"),
    "/admin/llm": ("大模型配置", "全局 LLM 配置"),
}

_GUIDE_PROTOCOL = """\
# 页面引导协议(帮用户"像真人一样使用页面")
- 平台所有功能都在左侧导航的页面里;你完成查询或操作后,必须告诉用户结果在
  哪个页面继续查看/操作,并用站内 markdown 链接给出入口,例如 [审查记录](/reviews)。
- 用户明确说"带我去/打开/跳转到某页",或某操作完成后最合适的下一步是唯一页面时,
  在回复末尾单独一行附加一条导航指令(系统会渲染成"前往"按钮并自动跳转):
  <!--PRISM_NAVIGATE {"action":"navigate","route":"页面路由","label":"按钮文字"}-->
  一次回复最多一条导航指令;拿不准目标页面时只给链接,不要附加指令。
- 只能使用本提示词列出的页面路由,不得编造;路由必须以 / 开头。
- 涉及具体对象时引导到详情页路径(如 /reviews/123、/projects/45),
  前提是该对象 ID 来自本次工具返回的真实数据。
"""


def _format_pages(routes: tuple[str, ...], labels: dict[str, tuple[str, str]]) -> str:
    lines = []
    for route in routes:
        label, purpose = labels.get(route, (route, ""))
        lines.append(f"- {route} {label}: {purpose}" if purpose else f"- {route} {label}")
    return "\n".join(lines)


def user_guide_block() -> str:
    """用户侧 Agent 的页面引导说明(含合法页面路由清单)。"""

    return (
        f"{_GUIDE_PROTOCOL}\n\n"
        f"# 你可引导的普通用户页面(路由 · 名称 · 用途)\n"
        f"{_format_pages(USER_PAGE_ROUTES + ('/reviews/start', '/forum/new', '/profile/personalization', '/profile/password', '/sandboxes'), USER_PAGE_LABELS)}"
    )


def admin_guide_block() -> str:
    """管理侧 Agent 的页面引导说明(含合法管理页路由清单)。"""

    return (
        f"{_GUIDE_PROTOCOL}\n\n"
        f"# 你可引导的管理页面(路由 · 名称 · 用途)\n"
        f"{_format_pages(ADMIN_PAGE_ROUTES, ADMIN_PAGE_LABELS)}"
    )
