"""仪表盘私域统计出口的真实 HTTP/RBAC 回归，仅使用本地合成 SQLite 数据。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import dashboard, reports, security
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import dashboard_service, report_service, review_service


@pytest.fixture
def private_dashboard():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    users = {
        name: User(username=f"dashboard-scope-{name}", password="local-fixture", role=role, status=1)
        for name, role in (("owner", "user"), ("author", "reviewer"), ("member", "reviewer"),
                           ("outsider", "reviewer"), ("admin", "admin"))
    }
    roles = {code: Role(name=code, code=code, status="active", is_builtin=1)
             for code in ("user", "reviewer", "admin")}
    db.add_all([*users.values(), *roles.values()])
    db.flush()
    report_grant = None
    for code in ("review:view", "security:view", "report:view", "report:export:json"):
        permission = Permission(code=code, name=code, module=code.split(":")[0], type="api")
        db.add(permission)
        db.flush()
        for role_code in ("user", "reviewer"):
            if role_code == "user" and code.startswith("report:"):
                continue
            grant = RolePermission(role_id=roles[role_code].id, permission_id=permission.id)
            db.add(grant)
            if role_code == "reviewer" and code == "report:view":
                report_grant = grant
    db.add_all(UserRole(user_id=user.id, role_id=roles[user.role].id) for user in users.values())
    project = Project(user_id=users["owner"].id, project_name="本地私域摘要项目", status="active")
    db.add(project)
    db.flush()
    memberships = {}
    for name in ("author", "member"):
        membership = ProjectMember(project_id=project.id, user_id=users[name].id, role_in_project="reviewer")
        db.add(membership)
        memberships[name] = membership
    ordinary = ReviewTask(user_id=users["owner"].id, project_id=project.id,
                          task_name="普通代码审查", review_type="standard", status="success", score=88,
                          create_time=datetime.now(timezone.utc) - timedelta(seconds=1))
    db.add(ordinary)
    db.flush()
    db.add(ReviewIssue(task_id=ordinary.id, severity="中", issue_type="普通代码问题",
                       title="本地合成问题", description="本地合成证据"))
    db.commit()
    app = FastAPI()
    register_handlers(app)
    app.include_router(dashboard.router, prefix="/api/dashboard")
    app.include_router(security.router, prefix="/api/security")
    app.include_router(reports.router, prefix="/api/reports")
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            yield {"client": client, "db": db, "users": users, "project": project,
                   "memberships": memberships, "ordinary": ordinary, "report_grant": report_grant}
    finally:
        db.close()
        engine.dispose()


def _domain(scope, source, *, actor="author", status="success"):
    task = ReviewTask(
        user_id=scope["users"][actor].id, project_id=scope["project"].id,
        task_name="可见的私域任务元信息", review_type=source, status=status,
        score=14, total_issues=3, severe_issues=3, summary="PRIVATE_REPORT_BODY",
    )
    scope["db"].add(task)
    scope["db"].flush()
    scope["db"].add(ReviewReport(
        task_id=task.id, user_id=task.user_id, score=14,
        create_time=datetime.now(timezone.utc),
        content_json={"source": source, "severity_counts": {"严重": 3},
                      "report_md": "## 问题清单\n### [严重] 本地甲\n### [严重] 本地乙\n### [严重] 本地丙"},
    ))
    scope["db"].commit()
    return task


def _get(scope, actor, path):
    user = scope["users"][actor]
    token = create_access_token(user.id, user.role, user.token_version or 0)
    response = scope["client"].get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _assert_only_ordinary_metrics(scope, actor, domain_task, outlet="all"):
    if outlet in {"all", "summary"}:
        summary = _get(scope, actor, "/api/dashboard/summary")
        assert summary["review_count"] == 2  # 任务元信息仍沿用项目成员可见范围。
        assert summary["total_issues"] == 1
        assert summary["severe_issues"] == 0
        private_row = next(item for item in summary["recent_tasks"] if item["id"] == domain_task.id)
        assert private_row["score"] is None
        assert private_row["coverage"] is None
        assert next(item for item in summary["recent_tasks"] if item["id"] == scope["ordinary"].id)["score"] == 88
    if outlet in {"all", "risk"}:
        risk = _get(scope, actor, "/api/dashboard/risk-distribution?days=0")
        assert {item["severity"]: item["count"] for item in risk} == {"严重": 0, "高": 0, "中": 1, "低": 0}
    if outlet in {"all", "types"}:
        assert _get(scope, actor, "/api/dashboard/issue-type-statistics?days=0") == [
            {"issue_type": "普通代码问题", "count": 1},
        ]
    if outlet in {"all", "trend"}:
        trend = _get(scope, actor, "/api/dashboard/score-trend")
        assert [(item["task_id"], item["score"]) for item in trend] == [(scope["ordinary"].id, 88)]
    if outlet in {"all", "security"}:
        safety = _get(scope, actor, "/api/security/dashboard-summary")
        assert safety["avg_risk_score"] == 88
        assert safety["source_summaries"] == [{"source": "standard", "total_issues": 1,
                                                "severity": {"中": 1}, "confirmed": 0, "refuted": 0}]


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
@pytest.mark.parametrize("outlet", ["summary", "risk", "types", "trend", "security"])
def test_project_member_with_report_permission_cannot_read_another_authors_private_metrics(
    private_dashboard, source, outlet,
):
    task = _domain(private_dashboard, source)
    _assert_only_ordinary_metrics(private_dashboard, "member", task, outlet)


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
@pytest.mark.parametrize("outlet", ["summary", "risk", "types", "trend", "security"])
def test_private_author_without_report_permission_cannot_use_dashboard_as_report_bypass(
    private_dashboard, source, outlet,
):
    task = _domain(private_dashboard, source, actor="owner")
    _assert_only_ordinary_metrics(private_dashboard, "owner", task, outlet)


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
@pytest.mark.parametrize("actor", ["author", "admin"])
def test_private_author_and_admin_keep_authorized_metrics(private_dashboard, source, actor):
    task = _domain(private_dashboard, source)
    if source == "sandbox_test":
        task.coverage = {"verification_status": "partial", "reason": "AI 动态补充未执行"}
        private_dashboard["db"].commit()
    summary = _get(private_dashboard, actor, "/api/dashboard/summary")
    assert summary["total_issues"] == 4
    assert summary["severe_issues"] == 3
    recent_task = next(item for item in summary["recent_tasks"] if item["id"] == task.id)
    assert recent_task["score"] == 14
    if source == "sandbox_test":
        assert recent_task["coverage"]["verification_status"] == "partial"
    assert {item["task_id"] for item in _get(private_dashboard, actor, "/api/dashboard/score-trend")} == {
        task.id, private_dashboard["ordinary"].id,
    }
    safety = _get(private_dashboard, actor, "/api/security/dashboard-summary")
    assert safety["avg_risk_score"] == 14
    assert sum(item["total_issues"] for item in safety["source_summaries"]) == 4


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_report_permission_revocation_cannot_reuse_warm_dashboard_cache(private_dashboard, monkeypatch, source):
    task = _domain(private_dashboard, source)
    monkeypatch.setattr(dashboard_service.settings, "dashboard_stats_cache_seconds", 60)
    assert _get(private_dashboard, "author", "/api/dashboard/summary")["total_issues"] == 4
    private_dashboard["db"].delete(private_dashboard["report_grant"])
    private_dashboard["db"].commit()  # 模拟其它进程撤权，不主动清空本进程缓存。
    _assert_only_ordinary_metrics(private_dashboard, "author", task)


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_author_membership_revocation_removes_all_project_metrics(private_dashboard, monkeypatch, source):
    _domain(private_dashboard, source)
    monkeypatch.setattr(dashboard_service.settings, "dashboard_stats_cache_seconds", 60)
    assert _get(private_dashboard, "author", "/api/dashboard/summary")["total_issues"] == 4
    private_dashboard["db"].delete(private_dashboard["memberships"]["author"])
    private_dashboard["db"].commit()
    summary = _get(private_dashboard, "author", "/api/dashboard/summary")
    assert summary["project_count"] == summary["total_issues"] == 0
    assert summary["recent_tasks"] == []
    assert _get(private_dashboard, "author", "/api/dashboard/score-trend") == []
    assert _get(private_dashboard, "author", "/api/security/dashboard-summary")["source_summaries"] == []


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
@pytest.mark.parametrize("status", ["success", "failed", "pending", "cancelled", "deleted"])
@pytest.mark.parametrize("actor", ["owner", "author", "member", "admin"])
def test_aggregate_sql_scope_matches_task_metric_policy(private_dashboard, source, status, actor):
    """聚合 SQL 不能与任务/项目出口的既有确定性授权规则分叉。"""
    task = _domain(private_dashboard, source, status=status)
    db = private_dashboard["db"]
    user = private_dashboard["users"][actor]
    allowed = db.query(ReviewTask.id).filter(
        ReviewTask.id == task.id, report_service.task_metrics_access_filter(db, user),
    ).count() == 1
    assert allowed is review_service.can_view_task_metrics(db, user, task)


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_private_scores_are_filtered_before_trend_limit(private_dashboard, source):
    _domain(private_dashboard, source)
    trend = _get(private_dashboard, "member", "/api/dashboard/score-trend?limit=1")
    assert [(item["task_id"], item["score"]) for item in trend] == [(private_dashboard["ordinary"].id, 88)]


@pytest.mark.parametrize("source,new_status", [("sandbox_test", "cancelled"), ("pentest", "failed")])
def test_unavailable_domain_result_cannot_reuse_warm_dashboard_cache(
    private_dashboard, monkeypatch, source, new_status,
):
    task = _domain(private_dashboard, source)
    monkeypatch.setattr(dashboard_service.settings, "dashboard_stats_cache_seconds", 60)
    assert _get(private_dashboard, "author", "/api/dashboard/summary")["total_issues"] == 4
    task.status = new_status
    private_dashboard["db"].commit()
    summary = _get(private_dashboard, "author", "/api/dashboard/summary")
    assert summary["total_issues"] == 1
    assert summary["severe_issues"] == 0
    assert _get(private_dashboard, "author", "/api/security/dashboard-summary")["source_summaries"] == [
        {"source": "standard", "total_issues": 1, "severity": {"中": 1}, "confirmed": 0, "refuted": 0},
    ]
