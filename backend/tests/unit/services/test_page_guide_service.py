"""页面引导协议与系统提示词契约测试。"""

from __future__ import annotations

from app.main import app
from app.services.admin_capability_registry import ADMIN_PAGE_ROUTES
from app.services.agent_responses_service import _instructions
from app.services.page_guide_service import (
    ADMIN_PAGE_LABELS,
    USER_PAGE_LABELS,
    admin_guide_block,
    user_guide_block,
)
from app.services.user_capability_registry import USER_PAGE_ROUTES


def test_guide_blocks_cover_all_registered_pages() -> None:
    """每个已注册页面都必须有中文名称与用途,防止引导到无名页面。"""
    for route in USER_PAGE_ROUTES:
        assert route in USER_PAGE_LABELS, route
        label, purpose = USER_PAGE_LABELS[route]
        assert label and purpose, route
    for route in ADMIN_PAGE_ROUTES:
        assert route in ADMIN_PAGE_LABELS, route
        label, purpose = ADMIN_PAGE_LABELS[route]
        assert label and purpose, route


def test_instructions_include_guide_protocol_and_recall() -> None:
    """主指令保留导航协议，页面清单改为 recall_knowledge 检索，不再固定注入。"""
    user = _instructions("user")
    admin = _instructions("admin")

    for text in (user, admin):
        assert "PRISM_NAVIGATE" in text
        assert "站内 markdown 链接" in text
        assert "不得编造" in text
        assert "recall_knowledge" in text

    # 页面清单不再逐条塞进固定指令，避免每轮 token 膨胀
    assert "/dashboard" not in user
    assert "/admin/users" not in user
    assert "/admin/overview" not in admin
    assert "/agent-studio" not in admin

    # 身份与角色化行为约定
    assert "棱镜小助" in user
    assert "批量处理与批量分析" in admin


def test_guide_routes_exist_in_frontend_route_table() -> None:
    """引导协议给出的路由必须与前端 router 同源(手工对账表,防漂移)。"""
    # 与 frontend/src/router/index.ts 静态路径对账;含动态段与重定向的不在清单内
    frontend_static_user_routes = {
        "/dashboard", "/projects", "/reviews", "/reviews/start", "/rules",
        "/security", "/reports", "/report/templates", "/code", "/issues",
        "/agents", "/sandboxes", "/agent-studio", "/forum", "/forum/new",
        "/knowledge", "/support/maintenance", "/support/feedback", "/profile",
        "/profile/personalization", "/profile/password", "/profile/api-config",
    }
    frontend_static_admin_routes = {
        "/admin/overview", "/admin/agents", "/admin/approvals",
        "/admin/agent-releases", "/admin/beta-codes", "/admin/policies",
        "/admin/tools", "/admin/knowledge", "/admin/jobs",
        "/admin/observability", "/admin/rewards", "/admin/rollback",
        "/admin/users", "/admin/rbac/roles", "/admin/rbac/permissions",
        "/admin/rbac/users", "/admin/ai-logs", "/admin/report-templates",
        "/admin/audit", "/admin/evolution", "/admin/skills",
        "/admin/embedding", "/admin/mcp-workers", "/admin/llm",
    }
    user_block = user_guide_block()
    for route in frontend_static_user_routes:
        assert route in user_block, f"用户引导缺少前端已有路由: {route}"
    admin_block = admin_guide_block()
    for route in frontend_static_admin_routes:
        assert route in admin_block, f"管理引导缺少前端已有路由: {route}"


def test_openapi_still_valid_for_all_capabilities() -> None:
    """冒烟:app 加载后能力注册表契约依旧可解析(防止引导改动破坏启动)。"""
    assert app.openapi()["paths"]
