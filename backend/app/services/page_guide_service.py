"""Agent 对话页面引导协议的服务端契约。

模型在回复里通过两种约定把用户带到对应页面、模拟真实页面操作:

1. 站内 markdown 链接 ``[标题](/路由)`` — 前端渲染为导航卡片,
   用户点击后 SPA 跳转;路由白名单与鉴权由前端同源守卫裁决。
2. 指令导航注释 ``<!--PRISM_NAVIGATE {"action":"navigate","route":"...","label":"..."}-->``
   — 当用户明确说"带我去/打开某页"或某操作完成后目标页面唯一时,
   回复末尾附加一条,前端解析为用户自行点击的导航按钮。

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
    "/reviews": ("审查任务", "查看与发起审查任务"),
    "/reviews/start": ("发起审查", "按项目与文件发起新审查"),
    "/issues": ("问题追踪", "跨任务检索并闭环问题"),
    "/reports": ("审查报告", "查看与导出审查报告"),
    "/audit": ("操作审计", "审查员查看有权限范围内的系统操作记录"),
    "/security": ("安全与审查规则", "查看安全态势、权威规则目录并管理审查规则"),
    "/agents": ("Agent 工作台", "查看 Agent 画像与运行态势"),
    "/agent-studio": ("Agent 工坊", "创建自定义 Agent 与 Skill"),
    "/sandboxes": ("代码沙箱", "创建测试沙箱与持续部署环境"),
    "/pentests": ("授权渗透测试", "查看与发起授权渗透委托、远程网站测试"),
    "/forum": ("开发者论坛", "发帖交流、回复讨论"),
    "/forum/new": ("发布新帖", "撰写并发布论坛帖子"),
    "/knowledge": ("个人知识库", "沉淀与检索个人知识"),
    "/support": ("支持中心", "统一提交支持工单和意见反馈并跟踪处理"),
    "/profile": ("个人中心", "维护个人资料与画像"),
    "/profile/personalization": ("个性化画像", "查看画像学习与偏好"),
    "/profile/password": ("修改密码", "修改登录密码"),
    "/profile/api-config": ("API 配置", "维护个人模型 API 配置"),
}

ADMIN_PAGE_LABELS: dict[str, tuple[str, str]] = {
    "/admin/governance": ("Agent 治理中心", "Agent、审批、发布、知识与能力治理"),
    "/admin/operations": ("运行与审计中心", "系统总览、策略、任务、监控、审计与运行记录"),
    "/admin/access": ("用户与权限中心", "用户、角色与权限管理"),
    "/admin/platform": ("平台配置中心", "模型、嵌入、内测码、MCP 与运行节点配置"),
    "/admin/security-center": ("小菱安全中心", "查看服务器安全监控、告警证据、巡检状态并调整监控灵敏度"),
    "/report/templates": ("报告模板管理", "维护报告模板"),
    "/support": ("支持中心", "处理用户反馈、支持工单并跟踪回复状态"),
    "/forum": ("开发者论坛", "查看、管理和置顶社区讨论"),
    "/admin/billing-refunds": ("计费与退款", "查看订单、退款申请及支付状态"),
    "/admin/billing-statements": ("渠道账单对账", "核对渠道账单与本地订单记录"),
    "/admin/billing-reservations": ("模型账本对账", "查看模型预占、用量证据与核对状态"),
    "/admin/billing-reviews": ("账本待复核", "查看待双人复核的模型与工具账单"),
    "/admin/billing-rate-reviews": ("费率待复核", "核对模型费率变更和来源状态"),
    "/admin/billing-refund-reviews": ("退款待审核", "查看退款审批申请和处理状态"),
    "/admin/billing-pricing": ("模型费率目录", "查看模型成本、计价版本和生效状态"),
    "/admin/enterprise-asset-publications": (
        "企业 Agent 全局审批", "查看企业资产提交给平台的全局发布申请"
    ),
    "/usage": ("Token 用量", "查看当前账号或当前授权范围的用量记录"),
    "/billing": ("充值与订阅", "查看可购买商品、订单和订阅权益"),
    "/enterprise": ("企业空间", "管理当前企业成员、小组、项目、沟通和企业资产"),
    "/my-agents": ("我的 Agent", "管理本人可见的 Agent 与发布申请"),
    "/remote-pentests": ("远程网站评估", "查看评估申请、审批状态和执行结果"),
}

USER_NAVIGATION_ONLY_LABELS: dict[str, tuple[str, str]] = {
    "/usage": ("Token 用量", "查看个人或已授权企业范围的 Token 用量"),
    "/billing": ("充值与订阅", "查看商品、订单和订阅权益"),
    "/enterprise": ("企业空间", "进入企业成员、小组、项目、沟通和资产页面"),
    "/my-agents": ("我的 Agent", "管理本人创建或获准使用的 Agent"),
    "/remote-pentests": ("远程网站评估", "查看评估申请及本人获准访问的结果"),
    "/agent-assets": ("共享 Agent 与 Skill", "浏览经过审核且对当前账号可见的 Agent 与 Skill"),
}

ADMIN_NAVIGATION_ONLY_ROUTES = (
    "/admin/billing-refunds",
    "/admin/billing-statements",
    "/admin/billing-reservations",
    "/admin/billing-reviews",
    "/admin/billing-rate-reviews",
    "/admin/billing-refund-reviews",
    "/admin/billing-pricing",
    "/admin/enterprise-asset-publications",
)
USER_NAVIGATION_ONLY_ROUTES = tuple(USER_NAVIGATION_ONLY_LABELS)
USER_GUIDE_ROUTES = tuple(dict.fromkeys((
    *USER_PAGE_ROUTES, *USER_NAVIGATION_ONLY_ROUTES,
    "/reviews/start", "/forum/new", "/profile/personalization", "/profile/password",
)))
ADMIN_SHELL_SHORTCUT_ROUTES = tuple(dict.fromkeys((
    *ADMIN_NAVIGATION_ONLY_ROUTES,
    *USER_NAVIGATION_ONLY_ROUTES,
)))
ADMIN_GUIDE_ROUTES = tuple(dict.fromkeys((
    *ADMIN_PAGE_ROUTES,
    *ADMIN_NAVIGATION_ONLY_ROUTES,
    *USER_GUIDE_ROUTES,
)))

_ENTERPRISE_SECTIONS = {
    "chat": "聊天",
    "forum": "企业论坛",
    "announcement": "企业公告",
    "knowledge": "企业知识库",
    "assets": "Agent 与 Skill",
    "remote-tests": "远测申请与审批",
    "assistant-approvals": "小菱审批",
    "management": "组织管理",
    "usage": "企业用量",
}

_COMMON_SECTIONS = {
    "/support": {"maintenance": "支持工单", "feedback": "意见反馈"},
}

_ADMIN_SECTIONS: dict[str, dict[str, str]] = {
    "/admin/platform": {
        "beta-codes": "内测码",
        "report-templates": "报告模板",
        "llm": "大模型",
        "embedding": "RAG 嵌入",
        "mcp-workers": "MCP 与沙箱节点",
    },
    "/admin/governance": {
        "agents": "Agent 目录",
        "studio": "创建与测试",
        "approvals": "审批中心",
        "evolution": "自进化",
        "knowledge": "知识与记忆",
        "skills": "Skill",
    },
    "/admin/operations": {
        "overview": "运行总览",
        "policies": "策略",
        "tools": "工具权限",
        "jobs": "任务调度",
        "observability": "监控告警",
        "logs": "日志与审计",
        "pentest-evidence": "渗透执行核查",
        "rewards": "奖惩趋势",
        "rollback": "版本回退",
    },
    "/admin/access": {
        "users": "用户管理",
        "roles": "角色管理",
        "permissions": "权限点",
    },
}


def current_page_context_block(surface: str, *, path: str, section: str = "") -> str:
    """Build a server-mapped current-page hint without trusting client labels or permissions."""
    if surface not in {"user", "admin"} or not path:
        return ""
    labels = (
        {**USER_PAGE_LABELS, **USER_NAVIGATION_ONLY_LABELS, **ADMIN_PAGE_LABELS}
        if surface == "admin"
        else {**USER_PAGE_LABELS, **USER_NAVIGATION_ONLY_LABELS}
    )
    routes = ADMIN_GUIDE_ROUTES if surface == "admin" else USER_GUIDE_ROUTES
    matched = next((route for route in sorted(routes, key=len, reverse=True)
                    if path == route or path.startswith(f"{route}/")), None)
    if matched is None:
        return ""
    label, purpose = labels.get(matched, (matched, ""))
    if path == "/enterprise":
        section_label = _ENTERPRISE_SECTIONS.get(section)
    elif path in _COMMON_SECTIONS:
        section_label = _COMMON_SECTIONS[path].get(section)
    elif surface == "admin":
        section_label = _ADMIN_SECTIONS.get(path, {}).get(section)
    else:
        section_label = None
    current = f"{label} / {section_label}" if section_label else label
    return (
        "# 当前页面位置\n"
        f"页面：{current}（{path}）；用途：{purpose}。\n"
        "用户指向“当前页面”时，可把此处作为任务语境；先通过已登记工具核验页面数据和可用操作。"
        "页面清单只说明导航位置；没有匹配的已登记工具时，不要代替用户执行操作。"
        "此路由线索不代表资源授权、页面内容或操作结果；服务端权限检查和风险审批始终优先。"
    )

_GUIDE_PROTOCOL = """\
# 页面引导协议(帮用户"像真人一样使用页面")
- 平台所有功能都在左侧导航的页面里;你完成查询或操作后,必须告诉用户结果在
  哪个页面继续查看/操作,并用站内 markdown 链接给出入口,例如 [审查任务](/reviews)。
- 需要给用户提供自行前往的入口时,可在回复末尾单独一行附加导航指令
  (系统将渲染为用户可点击的"前往"按钮):
  <!--PRISM_NAVIGATE {"action":"navigate","route":"页面路由","label":"按钮文字"}-->
  一次回复最多一条导航指令;拿不准目标页面时只给链接,不要附加指令。
- 只能使用本提示词列出的页面路由,不得编造;路由必须以 / 开头。
- 页面路由只代表合法导航目标;页面上下文与菜单可见性都不会授予工具或业务操作权限。
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
        f"{_format_pages(USER_GUIDE_ROUTES, {**USER_PAGE_LABELS, **USER_NAVIGATION_ONLY_LABELS})}"
    )


def admin_guide_block() -> str:
    """管理侧 Agent 的页面引导说明(含合法管理页路由清单)。"""

    return (
        f"{_GUIDE_PROTOCOL}\n\n"
        f"# 你可引导的管理页面(路由 · 名称 · 用途)\n"
        f"{_format_pages(ADMIN_GUIDE_ROUTES, {**USER_PAGE_LABELS, **USER_NAVIGATION_ONLY_LABELS, **ADMIN_PAGE_LABELS})}"
    )
