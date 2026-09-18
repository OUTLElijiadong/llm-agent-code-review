"""
仪表盘服务模块: 聚合统计数据

v2.4(2026-06-25): 数据隔离改为基于 project_member 关系
    - admin 视角: 全平台聚合(scope='global')
    - 非 admin 视角: owner ∪ member 项目聚合(scope='self')
    - _scope_filter / _valid_task_ids 统一改用 get_visible_project_ids
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import false as sa_false
from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.core.config import settings
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.project_member_service import get_visible_project_ids
from app.services.report_service import load_task_issue_stats

# ── 仪表盘聚合缓存 ──────────────────────────────────────────────────────────
# 工作台一次加载并发请求 summary/risk-distribution/issue-type-statistics 三个接口,
# 它们各自重复执行同一份逐任务问题统计(含沙箱报告 markdown 正则解析)。这里按
# (user_id, bucket, 当前可见项目集合) 缓存统计值；权限范围不缓存。
# TTL 内共享统计，同 key 并发请求只允许一个线程计算。
_CACHE_LIMIT = 256
_cache_lock = threading.Lock()
_key_locks: dict[tuple, threading.Lock] = {}
_stats_cache: dict[tuple, tuple[float, object]] = {}


def _cache_ttl() -> float:
    return float(settings.dashboard_stats_cache_seconds)


def _cached_compute(store: dict, key: tuple, compute):
    ttl = _cache_ttl()
    if ttl <= 0:
        return compute()
    with _cache_lock:
        hit = store.get(key)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        with _cache_lock:
            hit = store.get(key)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]
        value = compute()
        with _cache_lock:
            store[key] = (time.monotonic(), value)
            if len(store) > _CACHE_LIMIT:
                for stale in sorted(store.items(), key=lambda kv: kv[1][0])[: len(store) - _CACHE_LIMIT]:
                    store.pop(stale[0], None)
                    _key_locks.pop(stale[0], None)
        return value


def invalidate_dashboard_stats(user_id: int | None = None) -> None:
    """清除仪表盘聚合缓存;审查/报告/成员关系变化后调用可立即可见。"""
    with _cache_lock:
        if user_id is None:
            _stats_cache.clear()
            _key_locks.clear()
            return
        for store in (_stats_cache,):
            for key in [k for k in store if k[0] == user_id]:
                store.pop(key, None)
                _key_locks.pop(key, None)


def _visible_project_ids(db: Session, user: User) -> list[int]:
    """返回当前用户可见的项目 ID 列表(基于 project_member 关系)

    Args:
        db: 数据库会话
        user: 当前用户

    Returns:
        list[int]: 可见项目 ID 列表(admin 为全部非删除项目;非 admin 为 owner ∪ member)
    """

    # 权限范围每次读取数据库；不能由本进程 TTL 延迟其它 worker 的成员撤回。
    visible_ids, _ = get_visible_project_ids(db, user)
    return (
        [
            row[0]
            for row in db.query(Project.id)
            .filter(
                Project.id.in_(visible_ids),
                Project.status != "deleted",
            )
            .all()
        ]
        if visible_ids
        else []
    )


def _valid_task_ids(db: Session, user: User):
    """非删除审查任务的 id 子查询(限定在用户可见项目范围内)。

    问题/风险/类型统计据此排除已删除任务遗留的问题,避免删除报告后
    仪表盘问题数仍被旧问题虚高。

    Args:
        db: 数据库会话
        user: 当前用户

    Returns:
        sqlalchemy.sql.selectable.Select: 任务 ID 子查询
    """
    visible_ids = _visible_project_ids(db, user)
    return select(ReviewTask.id).where(
        ReviewTask.status != "deleted",
        ReviewTask.project_id.in_(visible_ids),
    )


# 问题统计只需要这些列;避免把 summary/rules_snapshot/score_breakdown 等
# 大列整行拉进内存(全平台 admin 视角下曾把所有任务的大字段反复加载三遍)。
_issue_stats_task_columns = load_only(
    ReviewTask.id,
    ReviewTask.user_id,
    ReviewTask.project_id,
    ReviewTask.review_type,
    ReviewTask.create_time,
    ReviewTask.end_time,
    ReviewTask.total_issues,
    ReviewTask.severe_issues,
    ReviewTask.high_issues,
    ReviewTask.medium_issues,
    ReviewTask.low_issues,
)


def _issue_stats(
    db: Session,
    user: User,
    *,
    since: datetime | None = None,
    window_days: int | None = None,
) -> list[dict]:
    """所有图表复用报告来源事实，保持非删除任务及项目成员可见范围。

    window_days 用于缓存归桶(窗口口径按天,不按秒级时间戳,否则 TTL 缓存
    形同虚设);since 仍作为实际过滤截止时刻。
    """

    visible_ids = _visible_project_ids(db, user)

    def _compute() -> list[dict]:
        tasks = (
            db.query(ReviewTask)
            .options(_issue_stats_task_columns)
            .filter(ReviewTask.status != "deleted", ReviewTask.project_id.in_(visible_ids))
            .all()
        )
        return list(load_task_issue_stats(db, tasks, since=since).values())

    if window_days is not None:
        bucket = f"days:{int(window_days)}"
    else:
        bucket = "all"
    return _cached_compute(_stats_cache, (user.id, bucket, tuple(sorted(visible_ids))), _compute)


def get_summary(db: Session, user: User) -> dict:
    """获取仪表盘汇总数据(基于 project_member 关系)

    Args:
        db: 数据库会话
        user: 当前用户

    Returns:
        dict: 含project_count/file_count/review_count等汇总数据
    """
    visible_ids = _visible_project_ids(db, user)

    project_count = (
        db.query(func.count(Project.id)).filter(Project.status != "deleted", Project.id.in_(visible_ids)).scalar() or 0
    )

    file_count = (
        db.query(func.count(CodeFile.id))
        .join(Project, Project.id == CodeFile.project_id)
        .filter(
            CodeFile.status == "active",
            Project.status != "deleted",
            Project.id.in_(visible_ids),
        )
        .scalar()
        or 0
    )

    archive_file_count = int(
        db.query(func.sum(ProjectSourceArchive.file_count))
        .filter(
            ProjectSourceArchive.project_id.in_(visible_ids),
            ProjectSourceArchive.storage_status == "active",
        )
        .scalar()
        or 0
    )

    review_count = (
        db.query(func.count(ReviewTask.id))
        .filter(
            ReviewTask.status == "success",
            ReviewTask.project_id.in_(visible_ids),
        )
        .scalar()
        or 0
    )

    issue_stats = _issue_stats(db, user)
    total_issues = sum(item["total_issues"] for item in issue_stats)
    severe_issues = sum(item["severity"].get("严重", 0) for item in issue_stats)

    # Test pass rates must not be aggregated as code quality scores.
    score_value, code_review_count = (
        db.query(func.avg(ReviewTask.score), func.count(ReviewTask.id))
        .filter(
            ReviewTask.status == "success",
            ReviewTask.project_id.in_(visible_ids),
            ReviewTask.review_type.in_(("quick", "standard", "security", "performance", "full")),
            ReviewTask.score.is_not(None),
            ReviewTask.score.between(0, 100),
        )
        .one()
    )
    avg_score = round(float(score_value or 0), 1)

    recent_q = (
        db.query(ReviewTask, Project.project_name)
        .join(Project, Project.id == ReviewTask.project_id)
        .filter(
            ReviewTask.status == "success",
            ReviewTask.project_id.in_(visible_ids),
            Project.status != "deleted",
        )
        .order_by(ReviewTask.create_time.desc())
        .limit(5)
    )
    recent_tasks = [
        {
            "id": task.id,
            "task_name": task.task_name or f"审查任务 #{task.id}",
            "project_id": task.project_id,
            "project_name": project_name,
            "status": task.status,
            "review_type": task.review_type,
            "score": task.score if task.score is not None and 0 <= task.score <= 100 else None,
            "create_time": task.create_time.isoformat() if task.create_time else None,
        }
        for task, project_name in recent_q.all()
    ]

    return {
        "project_count": project_count,
        "file_count": file_count,
        "archive_file_count": archive_file_count,
        "review_count": review_count,
        "total_issues": total_issues,
        "severe_issues": severe_issues,
        "avg_score": avg_score,
        "code_review_count": int(code_review_count or 0),
        "recent_tasks": recent_tasks,
    }


def get_risk_distribution(db: Session, user: User, days: int = 30) -> list[dict]:
    """获取风险等级分布(近N天,基于 project_member 关系)

    Args:
        db: 数据库会话
        user: 当前用户
        days: 统计天数;0 表示累计全部

    Returns:
        list[dict]: [{severity: str, count: int}, ...]
    """
    cutoff = None if days <= 0 else datetime.now(timezone.utc) - timedelta(days=days)
    result = {"严重": 0, "高": 0, "中": 0, "低": 0}
    for item in _issue_stats(db, user, since=cutoff, window_days=max(days, 0)):
        for severity, count in item["severity"].items():
            result[severity] = result.get(severity, 0) + count
    return [{"severity": k, "count": v} for k, v in result.items()]


def get_issue_type_statistics(db: Session, user: User, days: int = 30) -> list[dict]:
    """获取问题类型分布(近N天,基于 project_member 关系)

    Args:
        db: 数据库会话
        user: 当前用户
        days: 统计天数;0 表示累计全部

    Returns:
        list[dict]: [{issue_type: str, count: int}, ...]
    """
    cutoff = None if days <= 0 else datetime.now(timezone.utc) - timedelta(days=days)
    result: dict[str, int] = {}
    for item in _issue_stats(db, user, since=cutoff, window_days=max(days, 0)):
        for kind, count in item["by_type"].items():
            result[kind] = result.get(kind, 0) + count
    return [{"issue_type": kind, "count": count} for kind, count in result.items()]


def get_score_trend(db: Session, user: User, limit: int = 10) -> list[dict]:
    """获取评分趋势(最近N次审查,基于 project_member 关系)

    Args:
        db: 数据库会话
        user: 当前用户
        limit: 返回最近N条

    Returns:
        list[dict]: [{task_id, score, create_time}, ...]
    """
    visible_ids = _visible_project_ids(db, user)
    base_q = (
        db.query(ReviewTask)
        .filter(
            ReviewTask.status == "success",
            ReviewTask.project_id.in_(visible_ids),
            # 领域任务/历史任务可能没有质量评分；过滤掉无效分数，避免
            # ScoreTrendItem 校验 500，也不在图表中伪造 0 分。
            ReviewTask.score.is_not(None),
            ReviewTask.score.between(0, 100),
        )
        .order_by(ReviewTask.create_time.desc())
        .limit(limit)
    )
    rows = base_q.all()
    return [
        {"task_id": r.id, "score": r.score, "create_time": r.create_time.isoformat() if r.create_time else None}
        for r in reversed(rows)
    ]


def get_review_frequency(db: Session, user: User, days: int = 30) -> list[dict]:
    """获取审查频次趋势(近N天按日统计,基于 project_member 关系)

    Args:
        db: 数据库会话
        user: 当前用户
        days: 统计天数

    Returns:
        list[dict]: [{date: str, count: int}, ...]
    """
    # 数据库统一按 UTC 自然日存储；包含今天，累计只返回实际有记录的日期。
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    visible_ids = _visible_project_ids(db, user)
    day_column = func.date(ReviewTask.create_time)
    base_q = db.query(day_column.label("date"), func.count(ReviewTask.id)).filter(
        ReviewTask.status != "deleted",
        ReviewTask.project_id.in_(visible_ids),
        ReviewTask.create_time < today + timedelta(days=1),
    )
    start = today - timedelta(days=max(days - 1, 0))
    if days > 0:
        base_q = base_q.filter(ReviewTask.create_time >= start)
    rows = base_q.group_by(day_column).order_by(day_column).all()
    data_map = {str(row[0]): int(row[1]) for row in rows}
    if days <= 0:
        return [{"date": date, "count": count} for date, count in data_map.items()]
    return [
        {
            "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
            "count": data_map.get((start + timedelta(days=i)).strftime("%Y-%m-%d"), 0),
        }
        for i in range(days)
    ]


def get_running(db: Session, user: User) -> dict:
    """首页「后台进行中」:可见项目中排队/运行中的审查 + 本人进行中的 Agent 运行。

    轻量查询(索引列),供前端 5s 轮询;为空时前端隐藏整个面板(渐进披露)。
    """
    from app.models.agent_response_run import AgentResponseRun
    from app.services.rbac_service import check_permission, is_admin_user

    visible_ids = _visible_project_ids(db, user) if check_permission(db, user.id, "review:view") else []
    review_rows = (
        (
            db.query(ReviewTask, Project.project_name)
            .options(
                load_only(
                    ReviewTask.id,
                    ReviewTask.task_name,
                    ReviewTask.project_id,
                    ReviewTask.review_type,
                    ReviewTask.status,
                    ReviewTask.processed_files,
                    ReviewTask.total_files,
                    ReviewTask.create_time,
                )
            )
            .join(Project, Project.id == ReviewTask.project_id)
            .filter(
                ReviewTask.status.in_(("pending", "running")),
                Project.status != "deleted",
                ReviewTask.project_id.in_(visible_ids) if visible_ids else sa_false(),
            )
            .order_by(ReviewTask.create_time.desc())
            .limit(10)
            .all()
        )
        if visible_ids
        else []
    )
    reviews = [
        {
            "id": task.id,
            "task_name": task.task_name or f"审查任务 #{task.id}",
            "project_id": task.project_id,
            "project_name": project_name,
            "review_type": task.review_type or "standard",
            "status": task.status,
            "processed_files": int(task.processed_files or 0),
            "total_files": int(task.total_files or 0),
            "create_time": task.create_time.isoformat() if task.create_time else None,
        }
        for task, project_name in review_rows
    ]

    # 管理面与成员面使用不同的助手身份。工作台只展示当前登录账号
    # 在当前身份面上的运行记录，避免历史 admin 运行记录混入成员侧小菱列表。
    current_surface = "admin" if is_admin_user(db, int(user.id)) else "user"
    agent_rows = (
        (
            db.query(AgentResponseRun)
            .options(
                load_only(
                    AgentResponseRun.run_id,
                    AgentResponseRun.surface,
                    AgentResponseRun.session_key,
                    AgentResponseRun.status,
                    AgentResponseRun.update_time,
                )
            )
            .filter(
                AgentResponseRun.user_id == user.id,
                AgentResponseRun.surface == current_surface,
                AgentResponseRun.status.in_(
                    (
                        "running",
                        "approving",
                        "rejecting",
                        "answering",
                        "waiting_approval",
                        "waiting_input",
                    )
                ),
            )
            .order_by(AgentResponseRun.update_time.desc())
            .limit(5)
            .all()
        )
        if check_permission(db, user.id, "agent:chat")
        else []
    )
    agents = [
        {
            "run_id": row.run_id,
            "surface": row.surface,
            "session_key": row.session_key,
            "status": row.status,
            "update_time": row.update_time.isoformat() if row.update_time else None,
        }
        for row in agent_rows
    ]
    return {"reviews": reviews, "agents": agents}
