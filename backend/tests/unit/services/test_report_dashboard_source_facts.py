"""报告与仪表盘按来源统计；仅使用合成 SQLite 数据，不启动扫描。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.exceptions import NotFoundError
from app.models.pentest import PentestEngagement, PentestFinding
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.report import ReportDetailOut, ReportListItem
from app.schemas.security import SecurityDashboardSummaryOut
from app.services import dashboard_service, report_service, review_service, security_service


@pytest.fixture
def source_owner(db):
    owner = User(username="source-owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    return owner


@pytest.fixture
def source_project(db, source_owner):
    project = Project(user_id=source_owner.id, project_name="来源统计", status="active")
    db.add(project)
    db.flush()
    return project


def _task(db, owner, project, source, *, count=0, score=0, status="success", created=None):
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name=f"来源-{source}",
        review_type=source,
        status=status,
        total_issues=count,
        score=score,
        score_version="pentest-v1" if source == "pentest" else None,
        create_time=created or datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()
    return task


def _snapshot(db, task, **content):
    snapshot = ReviewReport(
        task_id=task.id,
        user_id=task.user_id,
        score=task.score,
        create_time=task.create_time,
        content_json={"source": task.review_type, **content},
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _issues(db, task, *, created=None):
    for severity, kind, status in [("高", "安全漏洞", "fixed"), ("中", "潜在Bug", "unfixed")]:
        db.add(ReviewIssue(
            task_id=task.id,
            severity=severity,
            issue_type=kind,
            status=status,
            description="本地合成问题",
            create_time=created or task.create_time,
        ))
    db.flush()


def _pentest(db, task, *, created=None):
    engagement = PentestEngagement(
        public_id=f"pt-source-{task.id}",
        user_id=task.user_id,
        project_id=task.project_id,
        target_type="web",
        status="completed",
        report_task_id=task.id,
    )
    db.add(engagement)
    db.flush()
    _snapshot(db, task, engagement_public_id=engagement.public_id)
    for position, (severity, kind, status) in enumerate([
        ("高", "注入", "confirmed"),
        ("严重", "鉴权", "candidate"),
        ("提示", "配置", "inconclusive"),
        ("严重", "已排除", "refuted"),
        ("高", "已排除", "refuted"),
    ]):
        db.add(PentestFinding(
            engagement_id=engagement.id,
            finding_fingerprint=f"source-{task.id}-{position}",
            title=f"合成发现-{position}",
            severity=severity,
            category=kind,
            status=status,
            create_time=created or task.create_time,
        ))
    db.flush()
    return engagement


def _listed(db, owner, task):
    return next(item for item in report_service.list_reports(db, owner)["items"] if item["task_id"] == task.id)


@pytest.mark.parametrize("source", ["standard", "discuss"])
def test_review_sources_keep_final_issue_scoring(db, source_owner, source_project, source):
    task = _task(db, source_owner, source_project, source, count=99, score=89)
    _issues(db, task)
    db.commit()

    assert review_service._task_agent_release_summaries(db, task.id) == []
    detail = report_service.get_report_detail(db, source_owner, task.id)
    listed = _listed(db, source_owner, task)

    assert detail["stats"]["total_issues"] == listed["total_issues"] == 2
    assert detail["stats"]["score"] == listed["score"] == 89
    assert detail["stats"]["score_breakdown"]["score_source"] == "review_issues"
    assert detail["stats"]["severity"] == {"高": 1, "中": 1}
    assert detail["stats"]["by_type"] == {"安全漏洞": 1, "潜在Bug": 1}
    assert detail["stats"]["fixed"] == 1
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 2
    assert task.total_issues == 99


@pytest.mark.parametrize("source,count,score", [("sandbox_test", 3, 100), ("pentest", 2, 84)])
def test_domain_snapshot_count_is_not_replaced_by_empty_review_issues(
    db, source_owner, source_project, source, count, score,
):
    task = _task(db, source_owner, source_project, source, count=count, score=score)
    _snapshot(db, task, report_md="领域报告历史快照")
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    listed = _listed(db, source_owner, task)

    assert detail["task"]["total_issues"] == detail["stats"]["total_issues"] == listed["total_issues"] == count
    assert detail["task"]["score"] == listed["score"] == score
    assert detail["stats"]["score_breakdown"]["score_source"] == source
    assert "weights" not in detail["stats"]["score_breakdown"]
    assert detail["files"] == []
    assert detail["source"]["type"] == listed["source"]["type"] == source
    assert ReportDetailOut(**detail).model_dump()["source"] == detail["source"]
    assert ReportListItem(**listed).model_dump()["source"] == listed["source"]
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == count
    assert task.total_issues == count
    assert db.query(ReviewIssue).filter_by(task_id=task.id).count() == 0


def test_sandbox_has_unclassified_findings_not_invented_severity(db, source_owner, source_project):
    task = _task(db, source_owner, source_project, "sandbox_test", count=3, score=100)
    _snapshot(db, task, public_id="sbx-source", report_md="## 问题清单\n- A\n- B\n- C\n")
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    risk = {item["severity"]: item["count"] for item in dashboard_service.get_risk_distribution(db, source_owner)}

    assert detail["stats"]["severity"] == {"未分级": 3}
    assert detail["stats"]["risk_level"] is None
    assert detail["stats"]["severe"] == detail["stats"]["low"] == 0
    assert detail["source"]["detail_api"] == "/api/sandboxes/sbx-source"
    assert risk == {"严重": 0, "高": 0, "中": 0, "低": 0, "未分级": 3}
    assert dashboard_service.get_issue_type_statistics(db, source_owner) == [{"issue_type": "沙箱测试", "count": 3}]


def test_pentest_uses_domain_findings_but_preserves_confirmed_only_score(db, source_owner, source_project):
    task = _task(db, source_owner, source_project, "pentest", count=999, score=84)
    engagement = _pentest(db, task)
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    listed = _listed(db, source_owner, task)
    summary = dashboard_service.get_summary(db, source_owner)

    assert detail["stats"]["total_issues"] == listed["total_issues"] == summary["total_issues"] == 5
    assert detail["stats"]["confirmed"] == 1
    assert detail["stats"]["refuted"] == 2
    assert detail["stats"]["fixed"] == 0
    assert detail["stats"]["severity"] == {"严重": 1, "高": 1, "提示": 1}
    assert detail["stats"]["by_type"] == {"注入": 1, "鉴权": 1, "配置": 1}
    assert detail["stats"]["low"] == 1
    assert detail["stats"]["risk_level"] == "高风险"
    assert detail["task"]["score"] == detail["stats"]["score"] == listed["score"] == 84
    assert detail["task"]["score_version"] == "pentest-v1"
    assert detail["stats"]["score_breakdown"]["score_source"] == "pentest"
    assert summary["severe_issues"] == 1
    assert detail["source"]["detail_api"] == f"/api/pentest/engagements/{engagement.public_id}"
    assert task.total_issues == 999


def test_all_sources_agree_in_list_detail_and_dashboard(db, source_owner, source_project):
    tasks = []
    cases = [("standard", 99, 89), ("discuss", 99, 89), ("sandbox_test", 3, 100), ("pentest", 99, 84)]
    for source, count, score in cases:
        task = _task(db, source_owner, source_project, source, count=count, score=score)
        if source in {"standard", "discuss"}:
            _issues(db, task)
        elif source == "pentest":
            _pentest(db, task)
        else:
            _snapshot(db, task)
        tasks.append(task)
    db.commit()

    listed = report_service.list_reports(db, source_owner)["items"]
    details = [report_service.get_report_detail(db, source_owner, task.id) for task in tasks]
    summary = dashboard_service.get_summary(db, source_owner)

    assert sum(item["total_issues"] for item in listed) == sum(item["stats"]["total_issues"] for item in details) == 12
    assert summary["total_issues"] == 12
    assert summary["review_count"] == 4
    # 讨论和领域测试保留各自评分，不混入明确代码审查类型的均分。
    assert summary["code_review_count"] == 1
    assert summary["avg_score"] == 89
    assert {item["score"] for item in summary["recent_tasks"]} == {84, 89, 100}
    assert {item["task_id"]: item["score"] for item in dashboard_service.get_score_trend(db, source_owner)} == {
        task.id: task.score for task in tasks
    }
    assert sum(item["count"] for item in dashboard_service.get_risk_distribution(db, source_owner)) == 10
    assert sum(item["count"] for item in dashboard_service.get_issue_type_statistics(db, source_owner)) == 10


def test_score_trend_omits_successful_tasks_without_a_valid_score(db, source_owner, source_project):
    """越界的历史评分不能让评分趋势接口返回 500 或伪造异常分数。"""
    scored = _task(db, source_owner, source_project, "standard", count=0, score=87)
    unscored = _task(db, source_owner, source_project, "sandbox_test", count=4, score=101)
    _snapshot(db, unscored, public_id="sbx-unscored", report_md="## 问题清单\n- A\n")
    db.commit()

    trend = dashboard_service.get_score_trend(db, source_owner, limit=10)

    assert [row["task_id"] for row in trend] == [scored.id]
    assert trend[0]["score"] == 87


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
@pytest.mark.parametrize("score", [0, 100])
def test_empty_source_keeps_explicit_score(db, source_owner, source_project, source, score):
    task = _task(db, source_owner, source_project, source, score=score)
    db.commit()
    detail = report_service.get_report_detail(db, source_owner, task.id)

    assert detail["stats"]["total_issues"] == _listed(db, source_owner, task)["total_issues"] == 0
    assert detail["stats"]["score"] == _listed(db, source_owner, task)["score"] == score
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 0


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
def test_failed_sources_preserve_availability_and_success_counts(db, source_owner, source_project, source):
    task = _task(db, source_owner, source_project, source, count=2, score=0, status="failed")
    if source in {"standard", "discuss"}:
        _issues(db, task)
    db.commit()

    if source == "sandbox_test":
        assert report_service.get_report_detail(db, source_owner, task.id)["stats"]["total_issues"] == 2
        assert _listed(db, source_owner, task)["status"] == "failed"
    else:
        with pytest.raises(NotFoundError):
            report_service.get_report_detail(db, source_owner, task.id)
        assert report_service.list_reports(db, source_owner)["total"] == 0
    summary = dashboard_service.get_summary(db, source_owner)
    assert summary["review_count"] == 0
    assert summary["avg_score"] == 0
    assert summary["recent_tasks"] == []
    assert summary["total_issues"] == 2


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
def test_task_owner_and_project_member_scopes_are_not_expanded(db, source_owner, source_project, admin_user, source):
    task = _task(db, source_owner, source_project, source, count=2)
    if source in {"standard", "discuss"}:
        _issues(db, task)
    member = User(username="source-member", password="x", role="user", status=1)
    outsider = User(username="source-outsider", password="x", role="user", status=1)
    db.add_all([member, outsider])
    db.flush()
    db.add(ProjectMember(project_id=source_project.id, user_id=member.id))
    db.commit()

    for reader in [source_owner, admin_user]:
        assert report_service.get_report_detail(db, reader, task.id)["stats"]["total_issues"] == 2
    for reader in [member, outsider]:
        with pytest.raises(NotFoundError):
            report_service.get_report_detail(db, reader, task.id)
        assert report_service.list_reports(db, reader)["total"] == 0
    assert dashboard_service.get_summary(db, member)["total_issues"] == 2
    assert dashboard_service.get_summary(db, outsider)["total_issues"] == 0


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
def test_deleted_task_and_member_of_deleted_project_are_excluded(db, source_owner, source_project, source):
    task = _task(db, source_owner, source_project, source, count=2)
    if source in {"standard", "discuss"}:
        _issues(db, task)
    member = User(username="deleted-project-member", password="x", role="user", status=1)
    db.add(member)
    db.flush()
    db.add(ProjectMember(project_id=source_project.id, user_id=member.id))
    db.commit()

    task.status = "deleted"
    db.commit()
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 0
    assert report_service.list_reports(db, source_owner)["total"] == 0
    task.status = "success"
    source_project.status = "deleted"
    db.commit()
    summary = dashboard_service.get_summary(db, member)
    assert summary["review_count"] == summary["total_issues"] == 0
    assert all(item["count"] == 0 for item in dashboard_service.get_risk_distribution(db, member))
    assert dashboard_service.get_score_trend(db, member) == []


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
def test_distribution_window_uses_source_result_time(db, source_owner, source_project, source):
    old_time = datetime.now(timezone.utc) - timedelta(days=60)
    task = _task(db, source_owner, source_project, source, count=3, created=old_time)
    if source in {"standard", "discuss"}:
        _issues(db, task)
    elif source == "pentest":
        _pentest(db, task)
    else:
        _snapshot(db, task)
    db.commit()

    assert dashboard_service.get_summary(db, source_owner)["total_issues"] > 0
    assert all(item["count"] == 0 for item in dashboard_service.get_risk_distribution(db, source_owner, days=30))
    assert dashboard_service.get_issue_type_statistics(db, source_owner, days=30) == []
    assert sum(item["count"] for item in dashboard_service.get_risk_distribution(db, source_owner, days=90)) > 0


def test_pentest_foreign_link_does_not_leak_findings_or_source(db, source_owner, source_project):
    task = _task(db, source_owner, source_project, "pentest", count=2, score=84)
    engagement = _pentest(db, task)
    engagement.user_id = source_owner.id + 1000
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id).one()
    snapshot.user_id = engagement.user_id
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    assert detail["stats"]["total_issues"] == 2
    assert detail["source"].get("detail_api") is None
    assert "注入" not in detail["stats"]["by_type"]
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 2


def test_live_empty_pentest_does_not_fall_back_to_stale_task_count(db, source_owner, source_project):
    task = _task(db, source_owner, source_project, "pentest", count=999, score=84)
    engagement = _pentest(db, task)
    db.query(PentestFinding).filter_by(engagement_id=engagement.id).delete()
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    assert detail["stats"]["total_issues"] == _listed(db, source_owner, task)["total_issues"] == 0
    assert detail["stats"]["score"] == 84
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 0


def test_no_records_return_zero_counts_and_missing_report(db, source_owner):
    assert report_service.list_reports(db, source_owner)["items"] == []
    with pytest.raises(NotFoundError):
        report_service.get_report_detail(db, source_owner, 99999)
    summary = dashboard_service.get_summary(db, source_owner)
    assert summary["total_issues"] == summary["review_count"] == summary["severe_issues"] == 0
    assert dashboard_service.get_issue_type_statistics(db, source_owner) == []


def test_security_dashboard_keeps_latest_successful_zero_score(db, source_owner, source_project):
    _task(db, source_owner, source_project, "standard", score=90)
    zero = _task(db, source_owner, source_project, "standard", score=0)
    _issues(db, zero)
    _task(db, source_owner, source_project, "standard", score=99, status="failed")
    db.commit()

    summary = security_service.get_dashboard_summary(db, source_owner)

    assert summary["avg_risk_score"] == 0
    assert summary["top_risky_projects"][0]["risk_score"] == 0
    assert summary["score_scope"] == "latest_successful_task_per_project"


@pytest.mark.parametrize("status", ["pending", "running", "failed", "cancelled", "deleted"])
def test_security_dashboard_unfinished_or_failed_score_is_not_valid(db, source_owner, source_project, status):
    _task(db, source_owner, source_project, "standard", score=88, status=status)
    db.commit()

    summary = security_service.get_dashboard_summary(db, source_owner)
    assert summary["avg_risk_score"] is None


def test_security_dashboard_exposes_each_source_without_relabeling_security_subset(db, source_owner, source_project):
    for source in ["standard", "discuss", "security", "sandbox_test", "pentest"]:
        task = _task(db, source_owner, source_project, source, count=3, score=84)
        if source == "pentest":
            _pentest(db, task)
        elif source == "sandbox_test":
            _snapshot(db, task)
        else:
            _issues(db, task)
    db.commit()

    summary = security_service.get_dashboard_summary(db, source_owner)
    serialized = SecurityDashboardSummaryOut(**summary).model_dump()
    sources = {item["source"]: item for item in serialized["source_summaries"]}

    assert serialized["issue_scope"] == "review_issue_security"
    assert summary["high_issues_total"] == 3
    assert summary["medium_issues_total"] == 0
    assert sources["sandbox_test"]["total_issues"] == 3
    assert sources["sandbox_test"]["severity"] == {"未分级": 3}
    assert sources["pentest"]["total_issues"] == 5
    assert sources["pentest"]["refuted"] == 2
    assert sources["pentest"]["severity"] == {"严重": 1, "高": 1, "提示": 1}
    for source in ["standard", "discuss", "security"]:
        assert sources[source]["total_issues"] == 2
    all_findings = dashboard_service.get_summary(db, source_owner)["total_issues"]
    assert sum(item["total_issues"] for item in sources.values()) == all_findings


def test_security_dashboard_deleted_project_membership_cannot_restore_scores(db, source_owner, source_project):
    _task(db, source_owner, source_project, "standard", score=88)
    db.add(ProjectMember(project_id=source_project.id, user_id=source_owner.id))
    source_project.status = "deleted"
    db.commit()

    summary = security_service.get_dashboard_summary(db, source_owner)
    assert summary["project_count"] == 0
    assert summary["avg_risk_score"] is None
    assert summary["source_summaries"] == []


def test_security_source_summaries_preserve_result_window_and_visibility(db, source_owner, source_project):
    task = _task(
        db, source_owner, source_project, "sandbox_test", count=3,
        created=datetime.now(timezone.utc) - timedelta(days=60),
    )
    _snapshot(db, task)
    other = User(username="security-source-other", password="x", role="user", status=1)
    db.add(other)
    db.commit()

    recent = security_service.get_dashboard_summary(db, source_owner, days=30)
    old = security_service.get_dashboard_summary(db, source_owner, days=90)
    hidden = security_service.get_dashboard_summary(db, other, days=90)

    assert sum(item["total_issues"] for item in recent["source_summaries"]) == 0
    assert sum(item["total_issues"] for item in old["source_summaries"]) == 3
    assert hidden["source_summaries"] == []


@pytest.mark.parametrize("passed,score", [(True, 100), (False, 30)])
def test_real_sandbox_publisher_report_and_dashboard_close_the_count_loop(
    db, source_owner, source_project, passed, score,
):
    from app.services.sandbox_service import _publish_sandbox_report

    published = _publish_sandbox_report(
        db,
        SimpleNamespace(public_id="sbx-published", owner_id=source_owner.id, project_id=source_project.id),
        {"passed": passed, "agent_tests": {"generated": 2, "passed_count": 1}},
        "## 总体结论\n合成结果\n## 问题清单\n- A\n- B\n- C\n",
    )
    task = db.get(ReviewTask, published["report_task_id"])
    detail = report_service.get_report_detail(db, source_owner, task.id)

    assert detail["stats"]["total_issues"] == _listed(db, source_owner, task)["total_issues"] == 3
    assert detail["stats"]["score"] == _listed(db, source_owner, task)["score"] == score
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 3
    assert task.status == ("success" if passed else "failed")


def test_real_pentest_publisher_and_domain_detail_agree(db, source_owner, source_project):
    from app.services.pentest_service import TARGET_TYPE_LABELS, _publish_pentest_report, get_engagement_detail

    task = _task(db, source_owner, source_project, "pentest", count=999, score=84)
    engagement = _pentest(db, task)
    task.task_name = f"授权渗透测试 · {TARGET_TYPE_LABELS[engagement.target_type]} · {engagement.public_id}"
    db.flush()
    findings = db.query(PentestFinding).filter_by(engagement_id=engagement.id).all()
    published = _publish_pentest_report(
        db, engagement, findings, "合成渗透报告", {"严重": 1, "高": 1, "提示": 1}, {"高": 1}, 84, {}, {}, "高风险",
    )
    domain = get_engagement_detail(db, source_owner, engagement.public_id)
    detail = report_service.get_report_detail(db, source_owner, published.id)

    assert published.id == task.id
    assert len(domain["findings"]) == detail["stats"]["total_issues"] == 5
    assert detail["stats"]["refuted"] == 2
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 5
    engagement.report_task_id = None
    db.commit()
    historical = report_service.get_report_detail(db, source_owner, published.id)
    assert historical["stats"]["total_issues"] == 5
    assert historical["stats"]["severity"] == detail["stats"]["severity"]
    assert historical["stats"]["refuted"] == 2
    assert historical["stats"]["score"] == 84


@pytest.fixture
def source_api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api.v1 import dashboard as dashboard_api
    from app.api.v1 import reports as reports_api
    from app.api.v1 import security as security_api
    from app.core.database import Base, get_db
    from app.core.dependencies import get_current_user
    from app.core.error_handlers import register_handlers
    from app.models.rbac import Permission, Role, RolePermission, UserRole

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    owner = User(username="source-api-owner", password="x", role="super_admin", status=1)
    other = User(username="source-api-other", password="x", role="user", status=1)
    db.add_all([owner, other])
    db.flush()
    # 外部账号具有报告查看权限，确保后续 404 实际验证资源归属隔离。
    report_reader = Role(name="来源接口报告读者", code="source_report_reader", status="active")
    report_view = Permission(code="report:view", name="报告查看", module="report", type="api")
    db.add_all([report_reader, report_view])
    db.flush()
    db.add_all([
        RolePermission(role_id=report_reader.id, permission_id=report_view.id),
        UserRole(user_id=other.id, role_id=report_reader.id),
    ])
    project = Project(user_id=owner.id, project_name="来源接口", status="active")
    db.add(project)
    db.commit()
    application = FastAPI()
    register_handlers(application)
    application.include_router(reports_api.router, prefix="/api/reports")
    application.include_router(dashboard_api.router, prefix="/api/dashboard")
    application.include_router(security_api.router, prefix="/api/security")
    current = {"user": owner}

    def get_fixture_db():
        yield db

    application.dependency_overrides[get_db] = get_fixture_db
    application.dependency_overrides[get_current_user] = lambda: current["user"]
    try:
        with TestClient(application) as client:
            yield client, db, owner, other, project, current
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("source,count", [("standard", 2), ("discuss", 2), ("sandbox_test", 3), ("pentest", 5)])
def test_source_http_list_detail_dashboard_and_ownership(source_api_client, source, count):
    client, db, owner, other, project, current = source_api_client
    task = _task(db, owner, project, source, count=count, score=0)
    if source in {"standard", "discuss"}:
        _issues(db, task)
    elif source == "pentest":
        _pentest(db, task)
    else:
        _snapshot(db, task)
    db.commit()

    response = client.get("/api/reports")
    assert response.status_code == 200
    listed = response.json()["data"]["items"][0]
    detail_response = client.get(f"/api/reports/{task.id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()["data"]
    dashboard_response = client.get("/api/dashboard/summary")
    assert dashboard_response.status_code == 200
    assert listed["total_issues"] == detail["stats"]["total_issues"] == count
    assert listed["source"] == detail["source"]
    assert dashboard_response.json()["data"]["total_issues"] == count
    security_response = client.get("/api/security/dashboard-summary")
    assert security_response.status_code == 200
    safety = security_response.json()["data"]
    assert safety["avg_risk_score"] == 0
    assert safety["source_summaries"][0]["total_issues"] == count
    assert safety["issue_scope"] == "review_issue_security"

    current["user"] = other
    assert client.get(f"/api/reports/{task.id}").status_code == 404
    assert client.get("/api/reports").json()["data"]["items"] == []
    assert client.get("/api/dashboard/summary").json()["data"]["total_issues"] == 0
    assert client.get("/api/security/dashboard-summary").status_code == 403


@pytest.mark.parametrize("bad_content", [None, [], "invalid", {"source": "other", "public_id": "foreign"}])
def test_invalid_domain_snapshot_does_not_override_task_facts(db, source_owner, source_project, bad_content):
    task = _task(db, source_owner, source_project, "sandbox_test", count=3, score=0)
    snapshot = _snapshot(db, task)
    snapshot.content_json = bad_content
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    assert detail["stats"]["total_issues"] == 3
    assert detail["stats"]["score"] == 0
    assert detail["source"].get("public_id") is None


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_republished_domain_result_uses_completion_time(db, source_owner, source_project, source):
    task = _task(
        db, source_owner, source_project, source, count=3,
        created=datetime.now(timezone.utc) - timedelta(days=60),
    )
    _snapshot(db, task)
    task.end_time = datetime.now(timezone.utc)
    db.commit()

    risk = dashboard_service.get_risk_distribution(db, source_owner, days=30)
    safety = security_service.get_dashboard_summary(db, source_owner, days=30)

    assert sum(item["count"] for item in risk) == 3
    assert sum(item["total_issues"] for item in safety["source_summaries"]) == 3
