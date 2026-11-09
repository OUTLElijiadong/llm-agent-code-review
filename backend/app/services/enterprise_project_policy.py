"""企业项目权限计算与个人项目隔离策略。"""
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.project import Project
from app.services.enterprise_org_service import require_enterprise_execution, require_enterprise_member

_ACTIONS = {
    "viewer": frozenset({"view"}),
    "reviewer": frozenset({"view", "execute"}),
    "editor": frozenset({"view", "execute", "write"}),
    "leader": frozenset({"view", "execute", "write", "manage"}),
    "admin": frozenset({"view", "execute", "write", "manage"}),
}


def require_enterprise_project_access(db: Session, enterprise_id: int, project_id: int,
                                      user_id: int, action: str, *,
                                      require_billing_execution: bool = True,
                                      lock: bool = False, nowait: bool = False) -> str:
    """检查企业成员、责任组及显式项目授权；任何异常状态默认拒绝。"""
    if action not in {"view", "execute", "write", "manage"}:
        raise ValidationError("未知的项目操作")
    binding_query = db.query(EnterpriseProject).filter_by(
        enterprise_id=enterprise_id, project_id=project_id, status="active"
    )
    binding = (binding_query.populate_existing().with_for_update(nowait=nowait) if lock else binding_query).first()
    if not binding:
        raise NotFoundError("企业项目不存在")
    project_query = db.query(Project).filter_by(id=project_id)
    project = (project_query.populate_existing().with_for_update(nowait=nowait) if lock else project_query).first()
    if lock:
        # Existing organization and project mutations lock the company. NOWAIT
        # prevents a source write that already holds its project/actor locks from
        # waiting in the opposite order and forming a deadlock.
        company = (db.query(Enterprise).filter_by(id=enterprise_id).populate_existing()
                   .with_for_update(read=True, nowait=nowait).first())
        if company is None or company.status not in {"active", "suspended"}:
            raise NotFoundError("企业不存在")
    group_query = db.query(EnterpriseGroup).filter_by(
        id=binding.group_id, enterprise_id=enterprise_id, status="active"
    )
    group = (group_query.populate_existing().with_for_update(read=True, nowait=nowait)
             if lock else group_query).first()
    if not project or project.status in {"deleted", "quarantined"} or not group:
        raise NotFoundError("企业项目不存在")
    member = require_enterprise_member(
        db,
        enterprise_id,
        user_id,
        require_billing_access=require_billing_execution,
        lock=lock,
        nowait=nowait,
    )
    role = ""
    group_member = None
    if member.role in {"owner", "admin"}:
        role = "admin"
    else:
        group_member_query = db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=enterprise_id, group_id=binding.group_id, user_id=user_id, status="active"
        )
        group_member = (group_member_query.populate_existing().with_for_update(nowait=nowait)
                        if lock else group_member_query).first()
        if group_member and group_member.role == "leader":
            role = "leader"
        else:
            grant_query = db.query(EnterpriseProjectGrant).filter_by(
                enterprise_id=enterprise_id, project_id=project_id, user_id=user_id, status="active"
            )
            grant = (grant_query.populate_existing().with_for_update(nowait=nowait) if lock else grant_query).first()
            if grant and grant.role in {"viewer", "reviewer", "editor"}:
                role = grant.role
            elif group_member:
                # Membership in the project's assigned group gives review/use
                # access. Writes and project administration remain separate.
                role = "reviewer"
    if action not in _ACTIONS.get(role, frozenset()):
        raise ForbiddenError("没有该企业项目操作权限")
    if action == "execute" and require_billing_execution:
        require_enterprise_execution(db, enterprise_id)
    return role
