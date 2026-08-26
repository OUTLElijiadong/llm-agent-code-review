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
from app.models.project_member import ProjectMember
from app.models.project_source_archive import ProjectSourceArchive
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.project import ProjectIn, ProjectUpdateIn
from app.services import project_source_revision_service
from app.services.project_member_service import (
    ensure_owner_member,
    get_visible_project_ids,
    require_project_access,
)
from app.utils.sanitize import sanitize_text


def _agent_run_stats(db: Session, project_ids: list[int]) -> dict[int, tuple]:
    """按项目统计 Agent 运转次数(工具调用日志)与最近运转时间。"""
    from app.models.agent_governance import ToolCallLog

    if not project_ids:
        return {}
    rows = (
        db.query(ToolCallLog.project_id, func.count(ToolCallLog.id), func.max(ToolCallLog.create_time))
        .filter(ToolCallLog.project_id.in_(project_ids))
        .group_by(ToolCallLog.project_id)
        .all()
    )
    return {project_id: (count, last_at) for project_id, count, last_at in rows}


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
    source_archives: dict[int, ProjectSourceArchive] = {}
    last_tasks: dict[int, ReviewTask] = {}
    member_roles: dict[int, str] = {}
    agent_runs: dict[int, tuple] = {}
    if proj_ids:
        agent_runs = _agent_run_stats(db, proj_ids)
        file_counts = dict(
            db.query(CodeFile.project_id, func.count(CodeFile.id))
            .filter(CodeFile.project_id.in_(proj_ids), CodeFile.status == "active")
            .group_by(CodeFile.project_id)
            .all()
        )
        source_archives = {
            row.project_id: row
            for row in db.query(ProjectSourceArchive).filter(
                ProjectSourceArchive.project_id.in_(proj_ids),
                ProjectSourceArchive.storage_status == "active",
            ).all()
        }
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
        member_roles = dict(
            db.query(ProjectMember.project_id, ProjectMember.role_in_project)
            .filter(
                ProjectMember.project_id.in_(proj_ids),
                ProjectMember.user_id == user.id,
            )
            .all()
        )

    items = []
    for row in rows:
        last_task = last_tasks.get(row.id)
        source_archive = source_archives.get(row.id)
        can_write = (
            user.role in {"admin", "super_admin"}
            or row.user_id == user.id
            or member_roles.get(row.id) == "owner"
        )
        # v2.0 B2: 用最近一次成功审查的真实评分,前端不再 hash 派生
        run_stats = agent_runs.get(row.id)
        items.append({
            "id": row.id, "project_name": row.project_name,
            "description": row.description, "language": row.language,
            "status": row.status,
            "agent_run_count": run_stats[0] if run_stats else 0,
            "last_agent_run_at": run_stats[1] if run_stats else None,
            "file_count": file_counts.get(row.id, 0) or (source_archive.file_count if source_archive else 0),
            "source_mode": "audit_archive" if source_archive else "files",
            "source_malware_status": source_archive.malware_status if source_archive else None,
            "can_update": can_write,
            "can_delete": can_write,
            "last_review_at": last_task.create_time if last_task else None,
            "score": last_task.score if last_task else None,
            "create_time": row.create_time,
        })
    return pagination.to_dict(items)


def create_project(
    db: Session,
    user: User,
    payload: ProjectIn,
    *,
    initial_status: str = "active",
    commit: bool = True,
) -> Project:
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
    if initial_status not in {"active", "importing"}:
        raise ValueError("项目初始状态只允许 active 或 importing")
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
        status=initial_status,
    )
    db.add(project)
    if commit:
        db.commit()
        db.refresh(project)
        # 同步写入 project_member(owner) 记录,确保后续数据隔离能查到该项目
        ensure_owner_member(db, project.id, user.id)
    else:
        db.flush()
        db.add(ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role_in_project="owner",
        ))
        db.flush()
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
    project_role = require_project_access(db, project_id, user, need_write=False)
    project = db.get(Project, project_id)
    agent_runs = _agent_run_stats(db, [project_id]).get(project_id)

    file_count = db.query(CodeFile.id).filter(
        CodeFile.project_id == project_id, CodeFile.status == "active").count()
    source_archive = db.query(ProjectSourceArchive).filter(
        ProjectSourceArchive.project_id == project_id,
        ProjectSourceArchive.storage_status == "active",
    ).first()
    if file_count == 0 and source_archive is not None:
        file_count = source_archive.file_count
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
        "source_mode": "audit_archive" if source_archive else "files",
        "source_archive": (
            {
                "original_filename": source_archive.original_filename,
                "archive_sha256": source_archive.archive_sha256,
                "compressed_size": source_archive.compressed_size,
                "expanded_size": source_archive.expanded_size,
                "file_count": source_archive.file_count,
                "max_member_size": source_archive.max_member_size,
                "max_compression_ratio": source_archive.max_compression_ratio,
                "storage_status": source_archive.storage_status,
                "malware_status": source_archive.malware_status,
                "audit_status": source_archive.audit_status,
                "quarantined": True,
                "threat_count": source_archive.threat_count,
            }
            if source_archive else None
        ),
        "can_update": project_role in {"admin", "owner"},
        "can_delete": project_role in {"admin", "owner"},
        "agent_run_count": agent_runs[0] if agent_runs else 0,
        "last_agent_run_at": agent_runs[1] if agent_runs else None,
        "create_time": project.create_time,
        "update_time": project.update_time,
        "recent_tasks": [
            {"id": t.id, "score": t.score, "total_issues": t.total_issues,
             "status": t.status, "create_time": t.create_time}
            for t in recent_tasks
        ],
        "source_revisions": project_source_revision_service.list_revisions(db, user, project_id),
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
