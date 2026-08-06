"""
报告服务模块
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai.scoring import compute_score
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.pagination import Pagination
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User


def list_reports(db: Session, user: User, project_id: int = None,
                 start_date: str = "", end_date: str = "",
                 page: int = 1, page_size: int = 20) -> dict:
    """查询报告列表

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID过滤
        start_date: 开始日期
        end_date: 结束日期
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    q = db.query(ReviewTask).filter(ReviewTask.status == "success")
    if user.role != "admin":
        q = q.filter(ReviewTask.user_id == user.id)
    if project_id:
        q = q.filter(ReviewTask.project_id == project_id)
    if start_date:
        q = q.filter(ReviewTask.create_time >= start_date)
    if end_date:
        q = q.filter(ReviewTask.create_time <= end_date + " 23:59:59")

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = q.order_by(ReviewTask.create_time.desc()).offset(pagination.offset).limit(pagination.page_size).all()

    items = []
    for row in rows:
        project = db.get(Project, row.project_id)
        items.append({
            "id": row.id, "task_id": row.id, "task_name": row.task_name,
            "project_name": project.project_name if project else "",
            "total_issues": row.total_issues, "score": row.score,
            "create_time": row.create_time.isoformat() if row.create_time else None,
        })
    return pagination.to_dict(items)


def get_report_detail(db: Session, user: User, task_id: int) -> dict:
    """获取报告详情

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Returns:
        dict: 报告完整数据
    """
    task = db.get(ReviewTask, task_id)
    if not task or task.status != "success":
        raise NotFoundError("报告不存在", code=40400)
    if task.user_id != user.id and user.role != "admin":
        raise NotFoundError("报告不存在", code=40400)

    project = db.get(Project, task.project_id)

    severity_rows = db.query(
        ReviewIssue.severity, func.count(ReviewIssue.id)
    ).filter(ReviewIssue.task_id == task_id).group_by(ReviewIssue.severity).all()

    type_rows = db.query(
        ReviewIssue.issue_type, func.count(ReviewIssue.id)
    ).filter(ReviewIssue.task_id == task_id).group_by(ReviewIssue.issue_type).all()
    fixed_count = db.query(func.count(ReviewIssue.id)).filter(
        ReviewIssue.task_id == task_id,
        ReviewIssue.status == "fixed",
    ).scalar() or 0

    from app.services.review_service import _task_agent_release_summaries

    return {
        "project": {"id": project.id, "project_name": project.project_name,
                     "language": project.language} if project else {},
        "task": {"id": task.id, "name": task.task_name, "review_type": task.review_type,
                 "total_files": task.total_files,
                 "duration_ms": task.duration_ms,
                 "score": task.score, "status": task.status,
                 "create_time": task.create_time.isoformat() if task.create_time else None,
                 "agent_releases": _task_agent_release_summaries(db, task_id)},
        "stats": {
            "total_files": task.total_files, "total_issues": task.total_issues,
            "score": task.score,
            "severe": task.severe_issues or 0,
            "high": task.high_issues or 0,
            "medium": task.medium_issues or 0,
            "low": task.low_issues or 0,
            "fixed": fixed_count,
            "severity": {s: c for s, c in severity_rows},
            "severity_breakdown": {s: c for s, c in severity_rows},
            "by_type": {t: c for t, c in type_rows},
        },
        "summary": task.summary,
        "files": _build_file_summaries(db, task_id),
        "rules_snapshot": task.rules_snapshot or [],
    }


def _build_file_summaries(db: Session, task_id: int) -> list[dict]:
    """汇总报告涉及的文件及其问题统计。

    Args:
        db: 数据库会话。
        task_id: 审查任务 ID。

    Returns:
        list[dict]: 文件摘要列表。旧任务没有关联记录时按历史问题回退。
    """
    issue_rows = db.query(
        ReviewIssue.file_id,
        ReviewIssue.file_name,
        ReviewIssue.severity,
        func.count(ReviewIssue.id),
    ).filter(
        ReviewIssue.task_id == task_id,
    ).group_by(
        ReviewIssue.file_id,
        ReviewIssue.file_name,
        ReviewIssue.severity,
    ).all()

    counts: dict[tuple[str, object], dict] = {}
    for file_id, file_name, severity, count in issue_rows:
        key = ("id", file_id) if file_id is not None else ("name", file_name or "未命名文件")
        item = counts.setdefault(key, {
            "file_id": file_id,
            "file_name": file_name or "未命名文件",
            "severity": {"严重": 0, "高": 0, "中": 0, "低": 0},
        })
        item["severity"][severity] = item["severity"].get(severity, 0) + count

    linked_files = db.query(CodeFile).join(
        ReviewTaskFile,
        ReviewTaskFile.file_id == CodeFile.id,
    ).filter(
        ReviewTaskFile.task_id == task_id,
    ).order_by(
        ReviewTaskFile.id.asc(),
    ).all()

    summaries = []
    emitted_keys: set[tuple[str, object]] = set()
    for code_file in linked_files:
        key = ("id", code_file.id)
        emitted_keys.add(key)
        summaries.append(_file_summary(code_file, counts.get(key)))

    fallback_ids = [key[1] for key in counts if key not in emitted_keys and key[0] == "id"]
    fallback_files = {
        row.id: row for row in db.query(CodeFile).filter(CodeFile.id.in_(fallback_ids)).all()
    } if fallback_ids else {}
    for key, item in counts.items():
        if key in emitted_keys:
            continue
        summaries.append(_file_summary(fallback_files.get(key[1]), item))
    return summaries


def _file_summary(code_file: CodeFile | None, counts: dict | None) -> dict:
    """构造单个报告文件摘要。

    Args:
        code_file: 代码文件记录，旧数据缺失时可为空。
        counts: 按严重度聚合的问题统计。

    Returns:
        dict: 可直接返回给报告详情页的文件摘要。
    """
    item = counts or {
        "file_id": code_file.id if code_file else None,
        "file_name": code_file.file_name if code_file else "未命名文件",
        "severity": {"严重": 0, "高": 0, "中": 0, "低": 0},
    }
    severity = item["severity"]
    return {
        "file_id": code_file.id if code_file else item["file_id"],
        "file_name": code_file.file_name if code_file else item["file_name"],
        "language": code_file.language if code_file else "",
        "issue_count": sum(severity.values()),
        "severe_count": severity.get("严重", 0),
        "score": compute_score(severity),
    }


def delete_report(db: Session, user: User, task_id: int) -> None:
    """删除报告(软删除对应的审查任务)

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Raises:
        NotFoundError: 报告/任务不存在
        ForbiddenError: 无操作权限
    """
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("报告不存在", code=40400)
    if task.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无权限删除此报告", code=40300)
    task.status = "deleted"
    db.commit()
