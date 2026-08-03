"""
项目成员业务服务

提供项目成员关系管理 + 通用可见项目过滤能力。
所有需要按项目成员关系做数据隔离的 service 都应调用本模块的 get_visible_project_ids。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User


def get_visible_project_ids(db: Session, user: Optional[User]) -> tuple[list[int], str]:
    """返回当前用户可见的项目 ID 列表 + 范围标识。

    可见范围定义:
        - admin: 全部非删除项目,scope='global'
        - 非admin: owner 项目(Project.user_id==user.id) ∪ member 项目(project_member.user_id==user.id),scope='self'

    Args:
        db: 数据库会话
        user: 当前用户(None 视为管理员视角,返回全局)

    Returns:
        tuple[list[int], str]: (项目ID列表, scope)
            - scope='global': 管理员视角,返回全部项目
            - scope='self': 普通用户视角,返回 owner ∪ member 项目
    """
    if user is None or user.role in {"admin", "super_admin"}:
        rows = db.query(Project.id).filter(Project.status != "deleted").all()
        return [r[0] for r in rows], "global"

    # owner 项目
    owner_rows = (
        db.query(Project.id)
        .filter(Project.user_id == user.id, Project.status != "deleted")
        .all()
    )
    owner_ids = [r[0] for r in owner_rows]

    # member 项目
    member_rows = (
        db.query(ProjectMember.project_id)
        .filter(ProjectMember.user_id == user.id)
        .all()
    )
    member_ids = [r[0] for r in member_rows]

    # union 去重
    visible_ids = list(set(owner_ids) | set(member_ids))
    return visible_ids, "self"


def is_project_member(
    db: Session, project_id: int, user: User
) -> tuple[bool, str]:
    """判断用户对项目的访问角色。

    Args:
        db: 数据库会话
        project_id: 项目ID
        user: 当前用户

    Returns:
        tuple[bool, str]: (是否可访问, 角色)
            - (True, "admin"): 管理员
            - (True, "owner"): 项目拥有者
            - (True, "reviewer"): 项目成员(审查员)
            - (False, ""): 无访问权限
    """
    if user.role in {"admin", "super_admin"}:
        return True, "admin"

    # owner 检查
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        return False, ""
    if project.user_id == user.id:
        return True, "owner"

    # member 检查
    member = (
        db.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
        .first()
    )
    if member:
        return True, member.role_in_project

    return False, ""


def require_project_access(
    db: Session, project_id: int, user: User, need_write: bool = False
) -> str:
    """校验用户对项目的访问权限,失败抛异常。

    审计策略:
        - 写权限访问(need_write=True): 无论成功/失败都记录审计日志
        - 读权限访问: 不记录(避免日志量过大)
        - 权限拒绝(ForbiddenError): 记录失败审计

    Args:
        db: 数据库会话
        project_id: 项目ID
        user: 当前用户
        need_write: 是否需要写权限(True 时仅 owner/admin 通过)

    Returns:
        str: 用户角色("admin"/"owner"/"reviewer")

    Raises:
        NotFoundError: 项目不存在
        ForbiddenError: 无访问权限或写权限不足
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)

    can_access, role = is_project_member(db, project_id, user)
    if not can_access:
        # 无访问权限:对写请求记录失败审计(读请求不记录,避免日志膨胀)
        if need_write:
            _audit_project_access(
                db, user, project_id,
                mode="write", role="none", status="failed",
                detail=f"无项目访问权限被拒绝 project_id={project_id}",
            )
        raise NotFoundError("项目不存在", code=40400)

    if need_write and role == "reviewer":
        # 写权限不足:记录失败审计
        _audit_project_access(
            db, user, project_id,
            mode="write", role=role, status="failed",
            detail=f"审查员写权限被拒绝 project_id={project_id}",
        )
        raise ForbiddenError("需要项目拥有者权限", code=40300)

    # 写权限校验通过:记录成功审计
    if need_write:
        _audit_project_access(
            db, user, project_id,
            mode="write", role=role, status="success",
            detail=f"写权限校验通过 project_id={project_id}",
        )

    return role


def _audit_project_access(
    db: Session,
    user: User,
    project_id: int,
    *,
    mode: str,
    role: str,
    status: str,
    detail: str,
) -> None:
    """记录项目访问审计日志(内部函数,失败不影响主业务)。

    提交策略:
        - status="failed": 立即 commit,因为外层即将 raise 异常触发 rollback,
          若不立即提交审计日志会被回滚丢失
        - status="success": commit=False,由外层事务统一提交,避免破坏业务事务原子性

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        mode: 访问模式("read"/"write")
        role: 用户角色("admin"/"owner"/"reviewer"/"none")
        status: 审计状态("success"/"failed")
        detail: 审计说明文本
    """
    try:
        from app.services import audit_service
        # 失败审计必须立即提交,否则会被外层异常 rollback 丢失
        commit_now = (status == "failed")
        audit_service.log(
            db,
            user,
            action="project_access",
            target_type="project",
            target_id=str(project_id),
            detail=detail,
            status=status,
            commit=commit_now,
        )
    except Exception:
        # 审计失败不影响主业务流程
        pass


def add_member(
    db: Session,
    project_id: int,
    user_id: int,
    role: str = "reviewer",
    operator: Optional[User] = None,
) -> ProjectMember:
    """加入项目成员。

    Args:
        db: 数据库会话
        project_id: 项目ID
        user_id: 被加入的用户ID
        role: 项目内角色(owner/reviewer),默认 reviewer
        operator: 操作者(用于权限校验),None 时跳过权限校验

    Returns:
        ProjectMember: 新建的成员关系记录

    Raises:
        NotFoundError: 项目或用户不存在
        ForbiddenError: 操作者无权限
        BadRequestError: 已是项目成员
    """
    if operator is not None:
        require_project_access(db, project_id, operator, need_write=True)

    # 校验项目存在
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)

    # 校验用户存在
    target_user = db.get(User, user_id)
    if not target_user or target_user.status != 1:
        raise NotFoundError("用户不存在或已禁用", code=40400)

    # 校验未重复加入
    existing = (
        db.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    if existing:
        raise BadRequestError("该用户已是项目成员", code=40000)

    member = ProjectMember(
        project_id=project_id,
        user_id=user_id,
        role_in_project=role,
    )
    db.add(member)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise BadRequestError("该用户已是项目成员", code=40000)
    db.commit()
    db.refresh(member)
    return member


def remove_member(
    db: Session,
    project_id: int,
    user_id: int,
    operator: Optional[User] = None,
) -> bool:
    """移除项目成员。

    Args:
        db: 数据库会话
        project_id: 项目ID
        user_id: 被移除的用户ID
        operator: 操作者(用于权限校验),None 时跳过权限校验

    Returns:
        bool: 是否成功移除

    Raises:
        NotFoundError: 项目不存在
        ForbiddenError: 操作者无权限
        BadRequestError: 不能移除项目 owner
    """
    if operator is not None:
        require_project_access(db, project_id, operator, need_write=True)

    # 不允许移除 owner
    member = (
        db.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    if not member:
        return False
    if member.role_in_project == "owner":
        raise BadRequestError("不能移除项目拥有者", code=40000)

    db.delete(member)
    db.commit()
    return True


def update_member_role(
    db: Session,
    project_id: int,
    user_id: int,
    new_role: str,
    operator: Optional[User] = None,
) -> ProjectMember:
    """更新成员角色。

    Args:
        db: 数据库会话
        project_id: 项目ID
        user_id: 被修改的用户ID
        new_role: 新角色(owner/reviewer)
        operator: 操作者(用于权限校验),None 时跳过权限校验

    Returns:
        ProjectMember: 更新后的成员关系记录

    Raises:
        NotFoundError: 项目或成员不存在
        ForbiddenError: 操作者无权限
        BadRequestError: 不能降级 owner 为 reviewer(需先转移 owner)
    """
    if operator is not None:
        require_project_access(db, project_id, operator, need_write=True)

    member = (
        db.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    if not member:
        raise NotFoundError("成员不存在", code=40400)

    # 不允许把 owner 降级为 reviewer(会导致项目无 owner)
    if member.role_in_project == "owner" and new_role != "owner":
        raise BadRequestError("不能降级项目拥有者,请先转移 owner 角色", code=40000)

    member.role_in_project = new_role
    db.commit()
    db.refresh(member)
    return member


def list_members(db: Session, project_id: int) -> list[dict]:
    """列出项目成员(含用户基本信息)。

    Args:
        db: 数据库会话
        project_id: 项目ID

    Returns:
        list[dict]: 成员列表,每项含 id/user_id/username/nickname/role_in_project/create_time
    """
    rows = (
        db.query(
            ProjectMember.id,
            ProjectMember.user_id,
            User.username,
            User.nickname,
            ProjectMember.role_in_project,
            ProjectMember.create_time,
            # R8 修复:补齐 update_time,对齐 MemberOut schema
            ProjectMember.update_time,
        )
        .join(User, User.id == ProjectMember.user_id)
        .filter(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.create_time)
        .all()
    )
    return [
        {
            "id": r[0],
            "user_id": r[1],
            "username": r[2],
            "nickname": r[3],
            "role_in_project": r[4],
            "create_time": r[5],
            # R8 修复:补齐 update_time
            "update_time": r[6],
        }
        for r in rows
    ]


def ensure_owner_member(db: Session, project_id: int, user_id: int) -> None:
    """确保项目的 owner 成员记录存在(创建项目时调用)。

    Args:
        db: 数据库会话
        project_id: 项目ID
        user_id: 拥有者用户ID
    """
    existing = (
        db.query(ProjectMember)
        .filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        .first()
    )
    if existing:
        if existing.role_in_project != "owner":
            existing.role_in_project = "owner"
            db.commit()
        return
    member = ProjectMember(
        project_id=project_id,
        user_id=user_id,
        role_in_project="owner",
    )
    db.add(member)
    db.commit()
