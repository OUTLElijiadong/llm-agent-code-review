"""Security dashboard 聚合、隔离、评分与趋势的补充测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.services import security_service as module


def _project(db: Any, user_id: int, name: str) -> Project:
    """创建项目；参数为会话、用户 ID、名称，返回已持久化 Project。"""
    project = Project(user_id=user_id, project_name=name, status="active")
    db.add(project)
    db.commit()
    return project


def _task(db: Any, project_id: int, score: int, status: str = "success") -> ReviewTask:
    """创建审查任务；参数含项目、评分和状态，返回已持久化 ReviewTask。"""
    task = ReviewTask(
        user_id=1,
        project_id=project_id,
        review_type="standard",
        status=status,
        score=score,
    )
    db.add(task)
    db.commit()
    return task


def _issue(
    db: Any,
    task_id: int,
    severity: str,
    *,
    title: str = "Injection",
    issue_type: str = "安全漏洞",
    create_time: Any = None,
) -> ReviewIssue:
    """创建审查问题；参数描述归属、严重度和时间，返回已持久化 ReviewIssue。"""
    issue = ReviewIssue(
        task_id=task_id,
        issue_type=issue_type,
        severity=severity,
        title=title,
        description=f"{title} detail",
        create_time=create_time or datetime.now(timezone.utc),
    )
    db.add(issue)
    db.commit()
    return issue


def test_project_ids_for_user_delegates_visibility_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """项目范围 helper 应原样委托统一成员可见性服务。"""
    db = object()
    user = SimpleNamespace(id=7, role="member")
    resolver = MagicMock(return_value=([1, 2], "self"))
    monkeypatch.setattr(module, "get_visible_project_ids", resolver)

    assert module._project_ids_for_user(db, user) == ([1, 2], "self")
    resolver.assert_called_once_with(db, user)


def test_infer_owasp_normalizes_missing_text_fields() -> None:
    """OWASP 推断应把空标题和描述转换为空字符串再调用 inferrer。"""
    inferrer = MagicMock(return_value=("A03:2021-Injection", "CWE-89"))
    issue = SimpleNamespace(title=None, description=None)

    assert module._infer_owasp_from_issue(issue, inferrer) == "A03:2021-Injection"
    inferrer.assert_called_once_with("", "")


def test_dashboard_empty_scope_clamps_day_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """无可见项目时应走快速路径，并把统计天数限制在 1 到 365。"""
    monkeypatch.setattr(module, "_project_ids_for_user", MagicMock(return_value=([], "self")))

    minimum = module.get_dashboard_summary(object(), SimpleNamespace(id=7), days=0)
    maximum = module.get_dashboard_summary(object(), SimpleNamespace(id=7), days=999)

    assert minimum["user_scope"] == "self"
    assert minimum["project_count"] == 0
    assert minimum["avg_risk_score"] is None
    assert len(minimum["trend"]) == 1
    assert len(maximum["trend"]) == 365
    assert all(point["severe"] == point["high"] == 0 for point in maximum["trend"])


def test_dashboard_projects_without_review_tasks_return_empty_scan(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可见项目没有有效审查任务时应保留项目数并返回空安全态势。"""
    project = _project(db, 7, "empty-project")
    monkeypatch.setattr(
        module,
        "_project_ids_for_user",
        MagicMock(return_value=([project.id], "self")),
    )

    summary = module.get_dashboard_summary(db, SimpleNamespace(id=7), days=7)

    assert summary["project_count"] == 1
    assert summary["scanned_project_count"] == 0
    assert summary["owasp_hotspots"] == []
    assert summary["top_risky_projects"] == []
    assert len(summary["trend"]) == 7


def test_dashboard_aggregates_visible_security_data_and_latest_scores(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安全态势应排除无关数据、使用每项目最新评分并生成热点、排序和趋势。"""
    project_a = _project(db, 7, "alpha")
    project_b = _project(db, 8, "beta")
    hidden = _project(db, 9, "hidden")
    old_a = _task(db, project_a.id, 80)
    latest_a = _task(db, project_a.id, 40)
    task_b = _task(db, project_b.id, 60)
    deleted = _task(db, project_a.id, 5, status="deleted")
    hidden_task = _task(db, hidden.id, 1)
    now = datetime.now(timezone.utc)

    _issue(db, old_a.id, "中", title="XSS")
    _issue(db, latest_a.id, "严重", title="SQL injection")
    _issue(db, latest_a.id, "高", title="SQL injection")
    _issue(db, task_b.id, "高", title="XSS")
    _issue(db, task_b.id, "低", title="style", issue_type="代码规范")
    _issue(db, task_b.id, "严重", title="old", create_time=now - timedelta(days=40))
    _issue(db, deleted.id, "严重", title="deleted")
    _issue(db, hidden_task.id, "严重", title="hidden")

    def infer_owasp(title: str, description: str) -> tuple[str, str]:
        """按标题模拟 OWASP 推断；参数为标题和描述，返回 OWASP/CWE。"""
        assert description
        if "injection" in title.lower():
            return "A03:2021-Injection", "CWE-89"
        if "xss" in title.lower():
            return "A03:2021-Injection", "CWE-79"
        return "", ""

    sentinel = SimpleNamespace(_infer_owasp_cwe=MagicMock(side_effect=infer_owasp))
    monkeypatch.setattr(module, "_project_ids_for_user", MagicMock(return_value=([project_a.id, project_b.id], "self")))
    monkeypatch.setattr(module, "SecuritySentinelAgent", MagicMock(return_value=sentinel))

    summary = module.get_dashboard_summary(db, SimpleNamespace(id=7), days=30)

    assert summary["user_scope"] == "self"
    assert summary["project_count"] == 2
    assert summary["scanned_project_count"] == 2
    assert summary["avg_risk_score"] == 50
    assert summary["severe_issues_total"] == 1
    assert summary["high_issues_total"] == 2
    assert summary["medium_issues_total"] == 1
    assert summary["low_issues_total"] == 0
    assert summary["owasp_hotspots"] == [{"owasp": "A03:2021-Injection", "count": 4}]
    assert [item["project_name"] for item in summary["top_risky_projects"]] == ["alpha", "beta"]
    assert summary["top_risky_projects"][0]["risk_score"] == 40
    assert summary["trend"][-1]["severe"] == 1
    assert summary["trend"][-1]["high"] == 2


def test_last_task_id_and_trend_cover_boundaries() -> None:
    """任务 ID helper 与趋势构建应覆盖空值、朴素时间、窗口外和低严重度。"""
    today = datetime.now(timezone.utc)
    issues = [
        SimpleNamespace(create_time=None, severity="严重"),
        SimpleNamespace(create_time=today.replace(tzinfo=None), severity="严重"),
        SimpleNamespace(create_time=today, severity="高"),
        SimpleNamespace(create_time=today, severity="低"),
        SimpleNamespace(create_time=today - timedelta(days=9), severity="严重"),
    ]

    assert module._last_task_id([], 1) == -1
    assert module._last_task_id([(2, 1, 80), (5, 1, 60), (9, 2, 10)], 1) == 5
    trend = module._build_trend(issues, 3)
    assert len(trend) == 3
    assert trend[-1]["severe"] == 1
    assert trend[-1]["high"] == 1
