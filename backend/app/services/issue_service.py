"""
审查问题服务模块
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.pagination import Pagination
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User


def get_issue(db: Session, user: User, issue_id: int) -> ReviewIssue:
    """获取问题详情

    Args:
        db: 数据库会话
        user: 当前用户
        issue_id: 问题ID

    Returns:
        ReviewIssue: 问题ORM对象

    Raises:
        NotFoundError: 问题不存在或无访问权限(为防枚举,越权一律按不存在处理)
    """
    issue = db.get(ReviewIssue, issue_id)
    if not issue:
        raise NotFoundError("问题不存在", code=40400)
    # 归属校验: 通过 issue → task 反查所有者
    if user.role != "admin":
        task = db.get(ReviewTask, issue.task_id)
        if task is None or task.user_id != user.id:
            raise NotFoundError("问题不存在", code=40400)
    return issue


def update_status(db: Session, user: User, issue_id: int, status: str) -> None:
    """更新单个问题的处理状态

    Args:
        db: 数据库会话
        user: 操作用户
        issue_id: 问题ID
        status: 新状态 (unfixed/fixed/ignored/pending_review)
    """
    issue = db.get(ReviewIssue, issue_id)
    if not issue:
        raise NotFoundError("问题不存在", code=40400)
    task = db.get(ReviewTask, issue.task_id)
    # 孤儿问题(task 缺失)对非管理员一律拒绝,避免绕过归属校验
    if user.role != "admin" and (task is None or task.user_id != user.id):
        raise ForbiddenError("无操作权限", code=40300)
    issue.status = status
    issue.handled_by = user.id
    issue.handled_at = datetime.now(timezone.utc)
    db.commit()


def list_issues(
    db: Session,
    user: User,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    severity: str = "",
    issue_type: str = "",
    status: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """跨任务/项目分页查询问题

    Args:
        db: 数据库会话
        user: 当前用户 (非管理员仅能看到自己项目下的问题)
        project_id: 限定项目
        task_id: 限定审查任务
        severity: 严重程度过滤
        issue_type: 问题类型过滤
        status: 状态过滤
        keyword: 标题或描述模糊匹配
        page: 页码
        page_size: 每页条数

    Returns:
        dict: 分页响应,items 中每条携带 project_id / project_name / task_name 冗余字段
    """
    q = (
        db.query(ReviewIssue, ReviewTask, Project)
        .join(ReviewTask, ReviewTask.id == ReviewIssue.task_id)
        .join(Project, Project.id == ReviewTask.project_id)
    )

    if user.role != "admin":
        q = q.filter(ReviewTask.user_id == user.id)
    if project_id is not None:
        q = q.filter(ReviewTask.project_id == project_id)
    if task_id is not None:
        q = q.filter(ReviewIssue.task_id == task_id)
    if severity:
        q = q.filter(ReviewIssue.severity == severity)
    if issue_type:
        q = q.filter(ReviewIssue.issue_type == issue_type)
    if status:
        if status != "all":
            q = q.filter(ReviewIssue.status == status)
    else:
        q = q.filter(ReviewIssue.status.in_(["unfixed", "pending_review"]))
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(and_(ReviewIssue.title.like(like) | ReviewIssue.description.like(like)))

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = (
        q.order_by(ReviewIssue.create_time.desc())
        .offset(pagination.offset)
        .limit(pagination.page_size)
        .all()
    )

    items = []
    for issue, task, project in rows:
        items.append(
            {
                "id": issue.id,
                "task_id": issue.task_id,
                "task_name": task.task_name,
                "project_id": project.id,
                "project_name": project.project_name,
                "file_id": issue.file_id,
                "file_name": issue.file_name,
                "line_number": issue.line_number,
                "end_line": issue.end_line,
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "title": issue.title,
                "description": issue.description,
                "suggestion": issue.suggestion,
                "fixed_code": issue.fixed_code,
                "status": issue.status,
                "create_time": issue.create_time,
            }
        )
    return pagination.to_dict(items)


def batch_update_status(db: Session, user: User, ids: list[int], status: str) -> None:
    """批量更新问题状态

    Args:
        db: 数据库会话
        user: 操作用户
        ids: 问题ID列表
        status: 新状态
    """
    for issue_id in ids:
        update_status(db, user, issue_id, status)
