"""SecuritySentinel 业务服务 (v2.1.1)

把"安全态势聚合"从 Agent 中独立出来 — Agent 只负责"扫",
service 负责"聚",前者依赖 LLM 链路,后者纯 SQL 查询。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User

_SEVERITY_KEYS = ("严重", "高", "中", "低")


def _project_ids_for_user(db: Session, user: Optional[User]) -> tuple[list[int], str]:
    """返回当前用户可见的项目 ID 列表 + 范围标识"""
    if user is None or user.role == "admin":
        rows = (
            db.query(Project.id)
            .filter(Project.status != "deleted")
            .all()
        )
        return [r[0] for r in rows], "global"
    rows = (
        db.query(Project.id)
        .filter(Project.user_id == user.id, Project.status != "deleted")
        .all()
    )
    return [r[0] for r in rows], "self"


def _infer_owasp_from_issue(issue: ReviewIssue,
                            inferrer) -> str:
    """从 ReviewIssue 推断 OWASP 编号

    优先级:title/description 中的关键词推断;失败返回空字符串。
    """
    owasp, _ = inferrer(issue.title or "", issue.description or "")
    return owasp


def get_dashboard_summary(db: Session, user: Optional[User],
                          days: int = 30) -> dict:
    """工作台安全态势汇总

    Args:
        db: 数据库会话
        user: 当前用户(None 表示管理员视角)
        days: 统计窗口

    Returns:
        dict: 符合 SecurityDashboardSummaryOut Schema
    """
    days = max(1, min(365, days))
    project_ids, scope = _project_ids_for_user(db, user)

    # 空项目快速路径
    if not project_ids:
        return {
            "user_scope": scope,
            "project_count": 0,
            "scanned_project_count": 0,
            "avg_risk_score": None,
            "severe_issues_total": 0,
            "high_issues_total": 0,
            "medium_issues_total": 0,
            "low_issues_total": 0,
            "owasp_hotspots": [],
            "top_risky_projects": [],
            "trend": _empty_trend(days),
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # 项目下所有任务(排除已删除任务,避免删除报告后安全统计虚高)
    task_rows = (
        db.query(ReviewTask.id, ReviewTask.project_id, ReviewTask.score)
        .filter(ReviewTask.project_id.in_(project_ids),
                ReviewTask.status != "deleted")
        .all()
    )
    task_ids_by_project: dict[int, list[int]] = defaultdict(list)
    score_by_project: dict[int, int] = {}
    for task_id, pid, score in task_rows:
        task_ids_by_project[pid].append(task_id)
        # 取每个项目最近一次有效评分(任务按 id desc 近似时间序)
        if score is not None and score > 0 and (
            pid not in score_by_project or task_id > _last_task_id(task_rows, pid)
        ):
            # 简化策略: 直接覆盖,因为任务 id 单调递增
            if score_by_project.get(pid, -1) < score or pid not in score_by_project:
                score_by_project[pid] = score

    all_task_ids = [tid for ids in task_ids_by_project.values() for tid in ids]
    if not all_task_ids:
        return {
            "user_scope": scope,
            "project_count": len(project_ids),
            "scanned_project_count": 0,
            "avg_risk_score": None,
            "severe_issues_total": 0,
            "high_issues_total": 0,
            "medium_issues_total": 0,
            "low_issues_total": 0,
            "owasp_hotspots": [],
            "top_risky_projects": [],
            "trend": _empty_trend(days),
        }

    # 安全类问题(窗口期内)
    issues = (
        db.query(ReviewIssue)
        .filter(
            ReviewIssue.task_id.in_(all_task_ids),
            ReviewIssue.issue_type == "安全漏洞",
            ReviewIssue.create_time >= cutoff,
        )
        .all()
    )

    # 严重度统计
    sev_counts = Counter(i.severity for i in issues)

    # OWASP 热点: 复用 SecuritySentinelAgent._infer_owasp_cwe
    agent = SecuritySentinelAgent()
    owasp_counter: Counter[str] = Counter()
    issues_by_project: dict[int, list[ReviewIssue]] = defaultdict(list)
    task_to_project: dict[int, int] = {}
    for tid, pid, _ in task_rows:
        task_to_project[tid] = pid
    for i in issues:
        owasp = _infer_owasp_from_issue(i, agent._infer_owasp_cwe)
        if owasp:
            owasp_counter[owasp] += 1
        pid = task_to_project.get(i.task_id)
        if pid is not None:
            issues_by_project[pid].append(i)

    # 项目名映射
    project_rows = (
        db.query(Project.id, Project.project_name)
        .filter(Project.id.in_(project_ids))
        .all()
    )
    name_by_pid = {pid: name for pid, name in project_rows}

    # 风险评分: 平均 + 高风险项目排序
    scored_pids = [pid for pid in score_by_project if score_by_project[pid] > 0]
    avg_score: Optional[int] = (
        round(sum(score_by_project[pid] for pid in scored_pids) / len(scored_pids))
        if scored_pids else None
    )

    risky_projects: list[dict] = []
    for pid, issues_in_p in issues_by_project.items():
        sev_local = Counter(i.severity for i in issues_in_p)
        risky_projects.append({
            "project_id": pid,
            "project_name": name_by_pid.get(pid, f"项目 #{pid}"),
            "risk_score": score_by_project.get(pid),
            "severe_issues": sev_local.get("严重", 0),
            "high_issues": sev_local.get("高", 0),
        })
    # 严重数倒序;严重数相同的取评分低的;最多 5 条
    risky_projects.sort(key=lambda x: (
        -x["severe_issues"], -x["high_issues"],
        x["risk_score"] if x["risk_score"] is not None else 100,
    ))
    risky_projects = risky_projects[:5]

    # 趋势曲线: 按天聚合 (按 issue.create_time)
    trend = _build_trend(issues, days)

    return {
        "user_scope": scope,
        "project_count": len(project_ids),
        "scanned_project_count": len(issues_by_project),
        "avg_risk_score": avg_score,
        "severe_issues_total": sev_counts.get("严重", 0),
        "high_issues_total": sev_counts.get("高", 0),
        "medium_issues_total": sev_counts.get("中", 0),
        "low_issues_total": sev_counts.get("低", 0),
        "owasp_hotspots": [
            {"owasp": k, "count": v}
            for k, v in owasp_counter.most_common(5)
        ],
        "top_risky_projects": risky_projects,
        "trend": trend,
    }


def _last_task_id(task_rows, project_id: int) -> int:
    """辅助: 找出该项目最大 task_id (近似 最新)"""
    max_id = -1
    for tid, pid, _ in task_rows:
        if pid == project_id and tid > max_id:
            max_id = tid
    return max_id


def _build_trend(issues, days: int) -> list[dict]:
    """按天聚合 严重/高 数量,返回 days 长度数组"""
    today = datetime.now(timezone.utc).date()
    bucket: dict[str, dict[str, int]] = {}
    # 初始化所有天
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        bucket[d.isoformat()] = {"severe": 0, "high": 0}
    for issue in issues:
        if issue.create_time is None:
            continue
        ctime = issue.create_time
        if ctime.tzinfo is None:
            ctime = ctime.replace(tzinfo=timezone.utc)
        key = ctime.date().isoformat()
        if key not in bucket:
            continue
        if issue.severity == "严重":
            bucket[key]["severe"] += 1
        elif issue.severity == "高":
            bucket[key]["high"] += 1
    return [
        {"date": d, "severe": v["severe"], "high": v["high"]}
        for d, v in sorted(bucket.items())
    ]


def _empty_trend(days: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    return [
        {
            "date": (today - timedelta(days=days - 1 - i)).isoformat(),
            "severe": 0,
            "high": 0,
        }
        for i in range(days)
    ]
