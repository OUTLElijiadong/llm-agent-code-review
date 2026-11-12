"""企业项目目录、归属和显式成员授权。"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_org import (
    Enterprise,
    EnterpriseAuditEvent,
    EnterpriseGroupMember,
)
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.project import Project
from app.services.enterprise_org_service import (
    require_enterprise_member,
    require_group_member,
)
from app.services.enterprise_project_policy import require_enterprise_project_access

_PROJECT_GRANT_ROLES = frozenset({"viewer", "reviewer", "editor"})
_HIDDEN_PROJECT_STATUSES = ("deleted", "quarantined")


def _lock_active_enterprise(db: Session, enterprise_id: int) -> Enterprise:
    company = (
        db.query(Enterprise)
        .filter_by(id=enterprise_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if company is None:
        raise NotFoundError("企业不存在")
    if company.status != "active" or company.billing_suspended:
        raise ForbiddenError("企业当前不能修改项目")
    from app.services.billing_checkout_service import enterprise_subscription_status

    if enterprise_subscription_status(db, enterprise_id) == "expired":
        raise ForbiddenError("企业订阅已过期，续费后才能管理项目")
    return company


def _active_binding(db: Session, enterprise_id: int, project_id: int) -> EnterpriseProject:
    row = db.query(EnterpriseProject).filter_by(
        enterprise_id=enterprise_id, project_id=project_id, status="active",
    ).populate_existing().with_for_update().first()
    if row is None:
        raise NotFoundError("企业项目不存在")
    return row


def _audit(
    db: Session,
    *,
    enterprise_id: int,
    actor_id: int,
    action: str,
    project_id: int,
    group_id: int,
    subject_user_id: int | None = None,
    role: str | None = None,
) -> None:
    detail = {"project_id": int(project_id)}
    if role is not None:
        detail["role"] = role
    db.add(EnterpriseAuditEvent(
        enterprise_id=enterprise_id,
        actor_id=actor_id,
        action=action,
        subject_user_id=subject_user_id,
        group_id=group_id,
        detail=detail,
    ))


def _serialize_project(db: Session, binding: EnterpriseProject, project: Project) -> dict:
    from app.models.enterprise_org import EnterpriseGroup

    group = db.query(EnterpriseGroup).filter_by(
        id=binding.group_id, enterprise_id=binding.enterprise_id,
    ).first()
    return {
        "project_id": int(project.id),
        "project_name": project.project_name,
        "description": project.description,
        "language": project.language,
        "status": project.status,
        "enterprise_id": int(binding.enterprise_id),
        "group_id": int(binding.group_id),
        "group_name": group.name if group else "",
        "binding_status": binding.status,
    }


def list_projects(db: Session, enterprise_id: int, actor_id: int, *, group_id: int | None = None) -> list[dict]:
    require_enterprise_member(db, enterprise_id, actor_id)
    if group_id is not None:
        require_group_member(db, enterprise_id, group_id, actor_id)
    query = (
        db.query(EnterpriseProject, Project)
        .join(Project, Project.id == EnterpriseProject.project_id)
        .filter(
            EnterpriseProject.enterprise_id == enterprise_id,
            EnterpriseProject.status == "active",
            Project.status.notin_(_HIDDEN_PROJECT_STATUSES),
        )
    )
    if group_id is not None:
        query = query.filter(EnterpriseProject.group_id == group_id)
    rows = query.order_by(Project.create_time.desc(), Project.id.desc()).all()
    result = []
    for binding, project in rows:
        try:
            role = require_enterprise_project_access(
                db, enterprise_id, int(project.id), actor_id, "view",
            )
        except (ForbiddenError, NotFoundError):
            continue
        item = _serialize_project(db, binding, project)
        item["role"] = role
        item["can_manage"] = role in {"admin", "leader"}
        result.append(item)
    return result


def bind_owned_project(
    db: Session,
    enterprise_id: int,
    project_id: int,
    *,
    group_id: int,
    actor_id: int,
) -> dict:
    """把操作者自己的个人项目纳入其企业小组，并写入审计记录。

    归属变更与初始项目授权使用同一事务；项目创建者必须是企业成员，
    以避免公司管理员在未获所有者同意时接管外部账号的个人项目。
    """
    _lock_active_enterprise(db, enterprise_id)
    require_enterprise_member(db, enterprise_id, actor_id, lock=True)
    require_group_member(db, enterprise_id, group_id, actor_id, lock=True)
    project = (
        db.query(Project)
        .filter_by(id=project_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if project is None or project.status in _HIDDEN_PROJECT_STATUSES:
        raise NotFoundError("项目不存在")
    if int(project.user_id) != actor_id:
        raise ForbiddenError("只能把自己创建的个人项目纳入企业")
    # Personal approvals serialize on the project row before locking their
    # engagement. Do not carry an active personal authorization into a tenant.
    from app.models.pentest import PentestEngagement

    active_pentest = (db.query(PentestEngagement.id)
                      .filter(PentestEngagement.project_id == project_id,
                              PentestEngagement.status.in_(("authorized", "running")))
                      .with_for_update().first())
    if active_pentest is not None:
        raise ConflictError("项目存在已授权或执行中的个人渗透测试，请先取消或结束后再绑定企业")
    # The durable execution path is not enabled for new runs yet, so every
    # previously started legacy engagement must remain unknown until reviewed.
    # Never let the feature flag decide whether project scope changes are safe.
    from app.services import pentest_execution_service

    evidence = pentest_execution_service.project_execution_evidence(db, project_id=project_id)
    if evidence != "clear":
        raise ConflictError("个人渗透执行仍在停止或缺少终止证据，请先核查后再绑定企业")
    existing = db.query(EnterpriseProject).filter_by(project_id=project_id).first()
    if existing is not None:
        raise ConflictError("项目已绑定企业，不能重复归属")

    binding = EnterpriseProject(
        enterprise_id=enterprise_id,
        project_id=project_id,
        group_id=group_id,
        status="active",
    )
    db.add(binding)
    # Project content changes scope at binding time. Retire any historic copies
    # from personal RAG in this same transaction before the enterprise grant is
    # committed, so a failed bind cannot leave a partial ownership transition.
    from app.services import agent_knowledge_service, knowledge_service

    knowledge_service.retire_enterprise_project_sources(db, project_id)
    agent_knowledge_service.retire_bound_project_sources(db, project_id)
    # Group members can review/run the project's jobs; the project creator
    # keeps explicit edit capability after their personal project is moved.
    grant = EnterpriseProjectGrant(
        enterprise_id=enterprise_id,
        project_id=project_id,
        user_id=actor_id,
        role="editor",
        status="active",
    )
    db.add(grant)
    _audit(
        db,
        enterprise_id=enterprise_id,
        actor_id=actor_id,
        action="enterprise_project_bound",
        project_id=project_id,
        group_id=group_id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("项目归属已被其他操作变更，请刷新后重试") from exc
    db.refresh(binding)
    return _serialize_project(db, binding, project)


def set_project_grant(
    db: Session,
    enterprise_id: int,
    project_id: int,
    user_id: int,
    *,
    actor_id: int,
    role: str,
) -> EnterpriseProjectGrant:
    if role not in _PROJECT_GRANT_ROLES:
        raise ValidationError("项目成员角色仅支持 viewer、reviewer 或 editor")
    _lock_active_enterprise(db, enterprise_id)
    require_enterprise_member(db, enterprise_id, actor_id, lock=True)
    binding = _active_binding(db, enterprise_id, project_id)
    actor_project_role = require_enterprise_project_access(
        db, enterprise_id, project_id, actor_id, "manage",
    )
    target = require_enterprise_member(db, enterprise_id, user_id, lock=True)
    if target.role in {"owner", "admin"}:
        raise ValidationError("企业所有者和管理员继承企业项目管理权限，无需单独授权")
    if actor_project_role == "leader":
        target_group_member = db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=enterprise_id,
            group_id=binding.group_id,
            user_id=user_id,
            status="active",
        ).with_for_update().first()
        if target_group_member is None or target_group_member.role != "member":
            raise ForbiddenError("小组长只能管理所属小组的普通成员项目权限")

    grant = db.query(EnterpriseProjectGrant).filter_by(
        enterprise_id=enterprise_id, project_id=project_id, user_id=user_id,
    ).populate_existing().with_for_update().first()
    if grant is None:
        grant = EnterpriseProjectGrant(
            enterprise_id=enterprise_id,
            project_id=project_id,
            user_id=user_id,
            role=role,
            status="active",
        )
        db.add(grant)
    else:
        grant.role = role
        grant.status = "active"
    _audit(
        db,
        enterprise_id=enterprise_id,
        actor_id=actor_id,
        action="enterprise_project_access_granted",
        project_id=project_id,
        group_id=binding.group_id,
        subject_user_id=user_id,
        role=role,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("项目授权已被其他操作变更，请刷新后重试") from exc
    db.refresh(grant)
    return grant


def revoke_project_grant(
    db: Session,
    enterprise_id: int,
    project_id: int,
    user_id: int,
    *,
    actor_id: int,
) -> None:
    _lock_active_enterprise(db, enterprise_id)
    require_enterprise_member(db, enterprise_id, actor_id, lock=True)
    binding = _active_binding(db, enterprise_id, project_id)
    actor_role = require_enterprise_project_access(db, enterprise_id, project_id, actor_id, "manage")
    target = require_enterprise_member(db, enterprise_id, user_id, lock=True)
    if target.role in {"owner", "admin"}:
        raise ValidationError("不能撤销企业所有者或管理员的继承权限")
    if actor_role == "leader":
        target_group_member = db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=enterprise_id,
            group_id=binding.group_id,
            user_id=user_id,
            status="active",
        ).with_for_update().first()
        if target_group_member is None or target_group_member.role != "member":
            raise ForbiddenError("小组长只能管理所属小组的普通成员项目权限")
    grant = db.query(EnterpriseProjectGrant).filter_by(
        enterprise_id=enterprise_id, project_id=project_id, user_id=user_id,
    ).populate_existing().with_for_update().first()
    if grant is None or grant.status != "active":
        raise NotFoundError("项目成员授权不存在")
    grant.status = "inactive"
    _audit(
        db,
        enterprise_id=enterprise_id,
        actor_id=actor_id,
        action="enterprise_project_access_revoked",
        project_id=project_id,
        group_id=binding.group_id,
        subject_user_id=user_id,
    )
    db.commit()
