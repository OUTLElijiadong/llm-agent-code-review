"""
API路由聚合模块
"""
from fastapi import APIRouter

from app.api.v1 import (
    agents,
    ai_chat,
    ai_logs,
    ai_prompt,
    audit,
    auth,
    code_files,
    dashboard,
    evolution,
    issues,
    projects,
    reports,
    review,
    rules,
    security,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["鉴权"])
api_router.include_router(users.router, prefix="/users", tags=["用户管理"])
api_router.include_router(projects.router, prefix="/projects", tags=["项目"])
api_router.include_router(code_files.router, prefix="/code-files", tags=["代码文件"])
api_router.include_router(rules.router, prefix="/rules", tags=["规则"])
api_router.include_router(review.router, prefix="/review", tags=["审查"])
api_router.include_router(issues.router, prefix="/issues", tags=["问题"])
api_router.include_router(reports.router, prefix="/reports", tags=["报告"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["仪表盘"])
api_router.include_router(ai_logs.router, prefix="/ai-logs", tags=["AI日志"])
api_router.include_router(ai_chat.router, prefix="/ai", tags=["AI助手"])
api_router.include_router(ai_prompt.router, prefix="/ai-prompt", tags=["AI提示词"])
api_router.include_router(security.router, prefix="/security", tags=["安全审计"])
api_router.include_router(agents.router, prefix="/agents", tags=["Agent中心"])
api_router.include_router(evolution.router, prefix="/evolution", tags=["Agent自进化"])
api_router.include_router(audit.router, prefix="/admin/audit", tags=["操作审计"])
