"""
项目管理服务模块

v2.4(2026-06-25): 数据隔离改为基于 project_member 关系
    - list_projects: admin 全量 / 非 admin: owner ∪ member
    - get/update/delete_project: 通过 require_project_access 校验
    - create_project: 创建后自动写入 project_member(owner) 记录
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.core.pagination import Pagination
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.project import ProjectIn, ProjectUpdateIn
from app.utils.sanitize import sanitize_text
from app.services.project_member_service import (
    ensure_owner_member,
    get_visible_project_ids,
    require_project_access,
)


def list_projects(db: Session, user: User, keyword: str = "", language: str = "",
                  status: str = "active", page: int = 1, page_size: int = 20) -> dict:
    """查询当前用户可访问的项目列表(基于 project_member 关系)

    可见范围:
        - admin: 全部非删除项目
        - 非 admin: owner 项目 ∪ member 项目

    Args:
        db: 数据库会话
        user: 当前用户
        keyword: 项目名搜索关键字
        language: 语言过滤
        status: 状态过滤
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    visible_ids, _ = get_visible_project_ids(db, user)
    q = db.query(Project).filter(Project.id.in_(visible_ids))
    if keyword:
        q = q.filter(Project.project_name.contains(keyword))
    if language:
        q = q.filter(Project.language == language)
    if status:
        q = q.filter(Project.status == status)
    else:
        q = q.filter(Project.status != "deleted")
    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = q.order_by(Project.create_time.desc()).offset(pagination.offset).limit(pagination.page_size).all()

    # 批量聚合,避免 N+1: 一次查全部文件数 + 一次查全部最近成功审查
    proj_ids = [r.id for r in rows]
    file_counts: dict[int, int] = {}
    last_tasks: dict[int, ReviewTask] = {}
    if proj_ids:
        file_counts = dict(
            db.query(CodeFile.project_id, func.count(CodeFile.id))
            .filter(CodeFile.project_id.in_(proj_ids), CodeFile.status == "active")
            .group_by(CodeFile.project_id)
            .all()
        )
        succ_tasks = (
            db.query(ReviewTask)
            .filter(ReviewTask.project_id.in_(proj_ids), ReviewTask.status == "success")
            .order_by(ReviewTask.create_time.desc())
            .all()
        )
        for t in succ_tasks:
            # 已按时间倒序,首次出现即该项目最近一次成功审查
            if t.project_id not in last_tasks:
                last_tasks[t.project_id] = t

    items = []
    for row in rows:
        last_task = last_tasks.get(row.id)
        # v2.0 B2: 用最近一次成功审查的真实评分,前端不再 hash 派生
        items.append({
            "id": row.id, "project_name": row.project_name,
            "description": row.description, "language": row.language,
            "status": row.status, "file_count": file_counts.get(row.id, 0),
            "last_review_at": last_task.create_time if last_task else None,
            "score": last_task.score if last_task else None,
            "create_time": row.create_time,
        })
    return pagination.to_dict(items)


def create_project(db: Session, user: User, payload: ProjectIn) -> Project:
    """创建新项目并写入 project_member(owner) 记录

    Args:
        db: 数据库会话
        user: 当前用户
        payload: 项目创建请求体

    Returns:
        Project: 新创建的项目

    Raises:
        ConflictError: 项目名与已有项目重复
    """
    exists = db.query(Project.id).filter(
        Project.user_id == user.id, Project.project_name == payload.project_name,
        Project.status != "deleted").first()
    if exists:
        raise ConflictError("项目名重复", code=40901)
    project = Project(
        user_id=user.id,
        # 项目名/描述剥纯文本,防存储型 XSS
        project_name=sanitize_text(payload.project_name),
        description=sanitize_text(payload.description),
        language=payload.language,
        status="active",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    # 同步写入 project_member(owner) 记录,确保后续数据隔离能查到该项目
    ensure_owner_member(db, project.id, user.id)
    return project


def get_project(db: Session, user: User, project_id: int) -> dict:
    """获取项目详情含最近审查任务

    通过 require_project_access 校验访问权限:
        - admin: 全部项目可读
        - owner: 自己的项目可读
        - reviewer: 被加入为成员的项目可读
        - 其他: 返回 404(防枚举)

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID

    Returns:
        dict: 项目详情

    Raises:
        NotFoundError: 项目不存在或无访问权限
    """
    require_project_access(db, project_id, user, need_write=False)
    project = db.get(Project, project_id)

    file_count = db.query(CodeFile.id).filter(
        CodeFile.project_id == project_id, CodeFile.status == "active").count()
    recent_tasks = db.query(ReviewTask).filter(
        ReviewTask.project_id == project_id,
        ReviewTask.status != "deleted",
    ).order_by(ReviewTask.create_time.desc()).limit(5).all()

    return {
        "id": project.id,
        "project_name": project.project_name,
        "description": project.description,
        "language": project.language,
        "status": project.status,
        "file_count": file_count,
        "create_time": project.create_time,
        "update_time": project.update_time,
        "recent_tasks": [
            {"id": t.id, "score": t.score, "total_issues": t.total_issues,
             "status": t.status, "create_time": t.create_time}
            for t in recent_tasks
        ],
    }


def update_project(db: Session, user: User, project_id: int, payload: ProjectUpdateIn) -> Project:
    """更新项目信息(需 owner/admin 权限)

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        payload: 更新请求体

    Returns:
        Project: 更新后的项目

    Raises:
        NotFoundError: 项目不存在或无访问权限
        ForbiddenError: 仅 owner/admin 可写
    """
    require_project_access(db, project_id, user, need_write=True)
    project = db.get(Project, project_id)
    if payload.project_name is not None:
        project.project_name = sanitize_text(payload.project_name)
    if payload.description is not None:
        project.description = sanitize_text(payload.description)
    if payload.language is not None:
        project.language = payload.language
    if payload.status is not None:
        project.status = payload.status
    db.commit()
    db.refresh(project)
    return project


def delete_project(db: Session, user: User, project_id: int) -> None:
    """软删除项目(需 owner/admin 权限)

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID

    Raises:
        NotFoundError: 项目不存在或无访问权限
        ForbiddenError: 仅 owner/admin 可删除
    """
    require_project_access(db, project_id, user, need_write=True)
    project = db.get(Project, project_id)
    project.status = "deleted"
    db.commit()
