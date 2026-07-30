"""管理员 AI 代管后台工具。

让管理员的聊天 Agent 能帮管理员管理后台:
- 只读类(查用户/角色/治理概览/告警/服务器状态)直接执行;
- 写操作(设角色/删用户/启停 Agent)一律创建审批事项,经管理员在审批中心
  人工「通过」后才执行,绝不让 AI 直接改库。

所有工具仅管理员可用(is_admin_user 校验),非管理员调用返回权限错误。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult
from app.models.user import User
from app.services import agent_governance_service, approval_service, observability_service
from app.services import rbac_service, user_service
from app.services.system_service import system_status


def _is_admin(db: Session, user: Optional[User]) -> bool:
    if user is None:
        return False
    return rbac_service.is_admin_user(db, user.id)


def _deny() -> AgentResult:
    return AgentResult(success=False, error="仅管理员可使用后台代管工具")


# ──────────────────────────── 只读工具(直接执行)────────────────────────────

def admin_list_users(db: Session, user: Optional[User], keyword: str = "",
                     role: str = "", page: int = 1, page_size: int = 20,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询用户列表(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    data = user_service.list_users(db, keyword=keyword, role=role, page=page, page_size=page_size)
    items = [
        {
            "id": u.id, "username": u.username, "nickname": u.nickname,
            "role": u.role, "status": u.status, "last_login_ip": getattr(u, "last_login_ip", None),
        }
        for u in data.get("items", [])
    ]
    return AgentResult(success=True, data={"total": data.get("total", 0), "users": items})


def admin_list_roles(db: Session, user: Optional[User],
                     ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询角色列表(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    roles = rbac_service.list_roles(db)
    return AgentResult(success=True, data=[
        {"code": r.code, "name": r.name, "description": r.description} for r in roles
    ])


def admin_governance_overview(db: Session, user: Optional[User],
                              ctx: Optional[AgentContext] = None) -> AgentResult:
    """Agent 治理概览(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    return AgentResult(success=True, data=observability_service.overview(db))


def admin_list_agents(db: Session, user: Optional[User],
                      ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询 Agent 配置档案(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    profiles = agent_governance_service.list_profiles(db)
    return AgentResult(success=True, data=[
        {"agent_code": p.code, "name": p.name, "status": p.status, "is_enabled": p.is_enabled}
        for p in profiles
    ])


def admin_list_approvals(db: Session, user: Optional[User], status: str = "pending",
                         ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询审批事项(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    items = approval_service.list_items(db, status=status)
    return AgentResult(success=True, data=[
        {"id": i.id, "title": i.title, "action": i.action, "status": i.status, "risk_level": i.risk_level}
        for i in items
    ])


def admin_system_status(db: Session, user: Optional[User],
                        ctx: Optional[AgentContext] = None) -> AgentResult:
    """服务器运行状态(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    return AgentResult(success=True, data=system_status())


# ──────────────────────── 写操作(强制管理员审批)────────────────────────────

def _create_approval(db: Session, user: User, *, title: str, action: str,
                     resource: str, reason: str, request: dict) -> AgentResult:
    """把敏感写操作转为审批事项,待管理员人工通过后执行。"""
    item = approval_service.create_or_auto_decide(
        db,
        title=title,
        action=action,
        resource=resource,
        risk_level="high",
        decision="deny",  # deny → 不会 auto_approved,必进 pending
        reason=reason,
        agent_code="chat_assistant",
        request=request,
        actor=user,
    )
    return AgentResult(success=True, data={
        "pending_approval": True,
        "approval_id": item.id,
        "message": f"已提交审批(#{item.id}),需你在「审批中心」人工通过后才会执行:{title}",
    })


def admin_set_user_role(db: Session, user: Optional[User], user_id: int, role: str,
                        ctx: Optional[AgentContext] = None) -> AgentResult:
    """申请修改用户角色(敏感,需审批)。"""
    if not _is_admin(db, user):
        return _deny()
    if role not in ("admin", "user", "reviewer"):
        return AgentResult(success=False, error=f"非法角色: {role}")
    target = db.get(User, user_id)
    if not target:
        return AgentResult(success=False, error=f"用户 {user_id} 不存在")
    return _create_approval(
        db, user,
        title=f"将用户「{target.username}」角色改为 {role}",
        action="user.set_role", resource=f"user:{user_id}",
        reason=f"管理员 AI 助手代管理员申请调整用户 {target.username} 的角色为 {role}",
        request={"user_id": user_id, "role": role},
    )


def admin_delete_user(db: Session, user: Optional[User], user_id: int,
                      ctx: Optional[AgentContext] = None) -> AgentResult:
    """申请删除用户(高危,需审批)。"""
    if not _is_admin(db, user):
        return _deny()
    target = db.get(User, user_id)
    if not target:
        return AgentResult(success=False, error=f"用户 {user_id} 不存在")
    return _create_approval(
        db, user,
        title=f"删除用户「{target.username}」",
        action="user.delete", resource=f"user:{user_id}",
        reason=f"管理员 AI 助手代管理员申请删除用户 {target.username}(软删,数据保留)",
        request={"user_id": user_id},
    )


def admin_toggle_agent(db: Session, user: Optional[User], agent_code: str, enable: bool,
                       ctx: Optional[AgentContext] = None) -> AgentResult:
    """申请启停某个 Agent(敏感,需审批)。"""
    if not _is_admin(db, user):
        return _deny()
    action_text = "启用" if enable else "停用"
    return _create_approval(
        db, user,
        title=f"{action_text} Agent「{agent_code}」",
        action="agent.toggle", resource=f"agent:{agent_code}",
        reason=f"管理员 AI 助手代管理员申请{action_text} Agent {agent_code}",
        request={"agent_code": agent_code, "enable": enable},
    )
