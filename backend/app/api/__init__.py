"""
API路由聚合模块
"""
from fastapi import APIRouter

from app.api.v1 import (
    admin_overview,
    agent_governance,
    agents,
    ai_chat,
    ai_logs,
    ai_prompt,
    api_config,
    audit,
    auth,
    code_files,
    dashboard,
    evolution,
    feedback,
    forum,
    issues,
    knowledge,
    llm_config,
    maintenance,
    project_members,
    projects,
    rbac,
    reports,
    review,
    rules,
    security,
    user_profile,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["鉴权"])
api_router.include_router(users.router, prefix="/users", tags=["用户管理"])
api_router.include_router(projects.router, prefix="/projects", tags=["项目"])
# v2.4: 项目成员管理(挂在 /projects/{project_id}/members 下)
api_router.include_router(project_members.router, prefix="/projects/{project_id}/members", tags=["项目成员"])
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
api_router.include_router(api_config.router, tags=["API配置"])
api_router.include_router(maintenance.router, prefix="/maintenance", tags=["维修工单"])
api_router.include_router(feedback.router, prefix="/feedback", tags=["用户反馈"])
api_router.include_router(forum.router, prefix="/forum", tags=["开发者论坛"])
api_router.include_router(user_profile.router, prefix="/me", tags=["用户画像"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["个人知识库"])
api_router.include_router(llm_config.router, prefix="/admin/llm", tags=["大模型配置"])
api_router.include_router(agent_governance.router, prefix="/admin", tags=["Agent治理"])
api_router.include_router(admin_overview.router, prefix="/admin", tags=["管理员总览"])
# RBAC 权限管理(角色/权限/菜单/数据范围,全部要求管理员身份)
api_router.include_router(rbac.router, prefix="/rbac", tags=["RBAC权限管理"])
