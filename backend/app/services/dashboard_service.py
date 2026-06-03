"""
仪表盘服务模块: 聚合统计数据
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User


def _scope_filter(user: User, model):
    """返回 scope 过滤条件

    Args:
        user: 当前用户
        model: ORM 模型类

    Returns:
        过滤条件列表, admin 返回空列表
    """
    if user.role != "admin":
        return [model.user_id == user.id]
    return []


def _valid_task_ids(db: Session, user: User):
    """非删除审查任务的 id 子查询(按用户 scope)。

    问题/风险/类型统计据此排除已删除任务遗留的问题,避免删除报告后
    仪表盘问题数仍被旧问题虚高。
    """
    query = select(ReviewTask.id).where(ReviewTask.status != "deleted")
    if user.role != "admin":
        query = query.where(ReviewTask.user_id == user.id)
    return query


def get_summary(db: Session, user: User) -> dict:
    """获取仪表盘汇总数据

    Args:
        db: 数据库会话
        user: 当前用户

    Returns:
        dict: 含project_count/file_count/review_count等汇总数据
    """
    scope = _scope_filter(user, Project)

    project_q = db.query(func.count(Project.id)).filter(
        Project.status != "deleted", *scope)
    project_count = project_q.scalar() or 0

    file_q = db.query(func.count(CodeFile.id)).join(
        Project, Project.id == CodeFile.project_id).filter(
        CodeFile.status == "active", Project.status != "deleted")
    if user.role != "admin":
        file_q = file_q.filter(Project.user_id == user.id)
    file_count = file_q.scalar() or 0

    review_scope = _scope_filter(user, ReviewTask)

    task_q = db.query(func.count(ReviewTask.id)).filter(
        ReviewTask.status == "success", *review_scope)
    review_count = task_q.scalar() or 0

    valid_ids = _valid_task_ids(db, user)
    total_issues = db.query(func.count(ReviewIssue.id)).filter(
        ReviewIssue.task_id.in_(valid_ids)).scalar() or 0
    severe_issues = db.query(func.count(ReviewIssue.id)).filter(
        ReviewIssue.task_id.in_(valid_ids), ReviewIssue.severity == "严重").scalar() or 0

    avg_q = db.query(func.avg(ReviewTask.score)).filter(
        ReviewTask.status == "success", *review_scope)
    avg_score = round(avg_q.scalar() or 0, 1)

    recent_q = db.query(ReviewTask).filter(
        ReviewTask.status == "success", *review_scope).order_by(
        ReviewTask.create_time.desc()).limit(5)
    recent_tasks = [{"id": t.id, "score": t.score, "create_time": t.create_time.isoformat() if t.create_time else None}
                    for t in recent_q.all()]

    return {
        "project_count": project_count,
        "file_count": file_count,
        "review_count": review_count,
        "total_issues": total_issues,
        "severe_issues": severe_issues,
        "avg_score": avg_score,
        "recent_tasks": recent_tasks,
    }


def get_risk_distribution(db: Session, user: User, days: int = 30) -> list[dict]:
    """获取风险等级分布(近N天)

    Args:
        db: 数据库会话
        user: 当前用户
        days: 统计天数

    Returns:
        list[dict]: [{severity: str, count: int}, ...]
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    base_q = db.query(ReviewIssue.severity, func.count(ReviewIssue.id)).filter(
        ReviewIssue.create_time >= cutoff,
        ReviewIssue.task_id.in_(_valid_task_ids(db, user)))
    rows = base_q.group_by(ReviewIssue.severity).all()
    result = {"严重": 0, "高": 0, "中": 0, "低": 0}
    for sev, cnt in rows:
        result[sev] = cnt
    return [{"severity": k, "count": v} for k, v in result.items()]


def get_issue_type_statistics(db: Session, user: User, days: int = 30) -> list[dict]:
    """获取问题类型分布(近N天)

    Args:
        db: 数据库会话
        user: 当前用户
        days: 统计天数

    Returns:
        list[dict]: [{issue_type: str, count: int}, ...]
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    base_q = db.query(ReviewIssue.issue_type, func.count(ReviewIssue.id)).filter(
        ReviewIssue.create_time >= cutoff,
        ReviewIssue.task_id.in_(_valid_task_ids(db, user)))
    rows = base_q.group_by(ReviewIssue.issue_type).all()
    return [{"issue_type": t, "count": c} for t, c in rows]


def get_score_trend(db: Session, user: User, limit: int = 10) -> list[dict]:
    """获取评分趋势(最近N次审查)

    Args:
        db: 数据库会话
        user: 当前用户
        limit: 返回最近N条

    Returns:
        list[dict]: [{task_id, score, create_time}, ...]
    """
    review_scope = _scope_filter(user, ReviewTask)

    base_q = db.query(ReviewTask).filter(
        ReviewTask.status == "success", *review_scope).order_by(
        ReviewTask.create_time.desc()).limit(limit)
    rows = base_q.all()
    return [{"task_id": r.id, "score": r.score, "create_time": r.create_time.isoformat() if r.create_time else None}
            for r in reversed(rows)]


def get_review_frequency(db: Session, user: User, days: int = 30) -> list[dict]:
    """获取审查频次趋势(近N天按日统计)

    Args:
        db: 数据库会话
        user: 当前用户
        days: 统计天数

    Returns:
        list[dict]: [{date: str, count: int}, ...]
    """
    from sqlalchemy import Date, cast

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    review_scope = _scope_filter(user, ReviewTask)

    base_q = db.query(
        cast(ReviewTask.create_time, Date).label("date"),
        func.count(ReviewTask.id)
    ).filter(ReviewTask.create_time >= cutoff, ReviewTask.status != "deleted",
             *review_scope).group_by("date")
    rows = base_q.all()
    data_map = {str(r[0]): r[1] for r in rows}
    result = []
    for i in range(days):
        d = (cutoff + timedelta(days=i)).strftime("%Y-%m-%d")
        result.append({"date": d, "count": data_map.get(d, 0)})
    return result
