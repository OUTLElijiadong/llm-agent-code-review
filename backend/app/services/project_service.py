"""
项目管理服务模块
"""
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.pagination import Pagination
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.project import ProjectIn, ProjectUpdateIn


def list_projects(db: Session, user: User, keyword: str = "", language: str = "",
                  status: str = "active", page: int = 1, page_size: int = 20) -> dict:
    """查询当前用户可访问的项目列表

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
    q = db.query(Project)
    if user.role != "admin":
        q = q.filter(Project.user_id == user.id)
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

    items = []
    for row in rows:
        file_count = db.query(CodeFile.id).filter(
            CodeFile.project_id == row.id, CodeFile.status == "active").count()
        last_task = db.query(ReviewTask).filter(
            ReviewTask.project_id == row.id, ReviewTask.status == "success"
        ).order_by(ReviewTask.create_time.desc()).first()
        # v2.0 B2: 用最近一次成功审查的真实评分,前端不再 hash 派生
        score = last_task.score if last_task else None
        items.append({
            "id": row.id, "project_name": row.project_name,
            "description": row.description, "language": row.language,
            "status": row.status, "file_count": file_count,
            "last_review_at": last_task.create_time if last_task else None,
            "score": score,
            "create_time": row.create_time,
        })
    return pagination.to_dict(items)


def create_project(db: Session, user: User, payload: ProjectIn) -> Project:
    """创建新项目

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
        project_name=payload.project_name,
        description=payload.description,
        language=payload.language,
        status="active",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def get_project(db: Session, user: User, project_id: int) -> dict:
    """获取项目详情含最近审查任务

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID

    Returns:
        dict: 项目详情

    Raises:
        NotFoundError: 项目不存在
        ForbiddenError: 无访问权限
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)

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
    """更新项目信息

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
        payload: 更新请求体

    Returns:
        Project: 更新后的项目
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)
    if payload.project_name is not None:
        project.project_name = payload.project_name
    if payload.description is not None:
        project.description = payload.description
    if payload.language is not None:
        project.language = payload.language
    if payload.status is not None:
        project.status = payload.status
    db.commit()
    db.refresh(project)
    return project


def delete_project(db: Session, user: User, project_id: int) -> None:
    """软删除项目

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID
    """
    project = db.get(Project, project_id)
    if not project or project.status == "deleted":
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)
    project.status = "deleted"
    db.commit()
