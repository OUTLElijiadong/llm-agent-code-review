"""报告与仪表盘按来源统计；仅使用合成 SQLite 数据，不启动扫描。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.core.permission_codes import PermissionCode
from app.models.pentest import PentestEngagement, PentestFinding
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.report import ReportDetailOut, ReportListItem
from app.schemas.security import SecurityDashboardSummaryOut
from app.services import (
    dashboard_service,
    pentest_service,
    rbac_service,
    report_service,
    review_service,
    security_service,
)


@pytest.fixture
def source_owner(db):
    owner = User(username="source-owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    # 本组验证已授权来源的数据一致性；权限缺失/撤权另由真实 HTTP 矩阵覆盖。
    role = Role(name="来源报告读者", code="user", status="active", is_builtin=1)
    permissions = [
        Permission(code="report:view", name="报告查看", module="report", type="api"),
        Permission(code=PermissionCode.ISSUE_VIEW, name="问题查看", module="issue", type="api"),
        Permission(code="security:view", name="安全报告查看", module="security", type="api"),
        Permission(code="pentest:view", name="渗透报告查看", module="pentest", type="api"),
    ]
    db.add_all([role, *permissions])
    db.flush()
    db.add(UserRole(user_id=owner.id, role_id=role.id))
    db.add_all([RolePermission(role_id=role.id, permission_id=permission.id) for permission in permissions])
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
        coverage=(
            {
                "coverage_status": "complete",
                "score_available": True,
                "tested": ["synthetic fixture scope"],
                "unscanned": [],
            }
            if source in {"sandbox_test", "pentest"} else None
        ),
        create_time=created or datetime.now(timezone.utc),
    )
    db.add(task)
    db.flush()
    return task


def _snapshot(db, task, **content):
    if task.review_type in {"sandbox_test", "pentest"}:
        content.setdefault("report_md", "## 合成来源报告\n\n测试夹具中的有效来源快照。")
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


def _pentest(db, task, *, created=None, include_findings=True):
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
    findings = [
        ("高", "注入", "confirmed"),
        ("严重", "鉴权", "candidate"),
        ("提示", "配置", "inconclusive"),
        ("严重", "已排除", "refuted"),
        ("高", "已排除", "refuted"),
    ] if include_findings else []
    for position, (severity, kind, status) in enumerate(findings):
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


def test_pentest_list_hides_unreadable_report_reference_from_authorizer(db, source_owner, source_project):
    """授权审查员可见委托卡片，但不得获得发起人报告入口。"""
    task = _task(db, source_owner, source_project, "pentest")
    engagement = _pentest(db, task, include_findings=False)
    duplicate_link = PentestEngagement(
        public_id="pt-duplicate-report-link",
        user_id=source_owner.id,
        project_id=source_project.id,
        target_type="web",
        status="completed",
        report_task_id=task.id,
    )
    db.add(duplicate_link)
    valid_task = _task(db, source_owner, source_project, "pentest")
    valid_engagement = _pentest(db, valid_task, include_findings=False)
    valid_snapshot = db.query(ReviewReport).filter_by(task_id=valid_task.id).one()
    # 渗透报告由领域发现生成，不要求快照重复保存 Markdown 正文。
    valid_snapshot.content_json = {**valid_snapshot.content_json, "report_md": " \n"}
    incomplete_task = _task(db, source_owner, source_project, "pentest")
    incomplete_engagement = _pentest(db, incomplete_task, include_findings=False)
    incomplete_snapshot = db.query(ReviewReport).filter_by(task_id=incomplete_task.id).one()
    incomplete_snapshot.content_json = {
        **incomplete_snapshot.content_json,
        "engagement_public_id": "wrong-engagement-link",
    }

    reviewer = User(username="pentest-reviewer", password="x", role="reviewer", status=1)
    reviewer_role = Role(code="reviewer", name="渗透授权审查员", status="active", is_builtin=1)
    permission_codes = (
        PermissionCode.PENTEST_VIEW,
        PermissionCode.PENTEST_AUTHORIZE,
        PermissionCode.REPORT_VIEW,
    )
    permissions = []
    for code in permission_codes:
        permission = db.query(Permission).filter_by(code=code).one_or_none()
        if permission is None:
            permission = Permission(code=code, name=code, module="pentest", type="api")
            db.add(permission)
            db.flush()
        permissions.append(permission)
    db.add_all([reviewer, reviewer_role])
    db.flush()
    db.add(UserRole(user_id=reviewer.id, role_id=reviewer_role.id))
    db.add(ProjectMember(project_id=source_project.id, user_id=reviewer.id))
    db.add_all([
        RolePermission(role_id=reviewer_role.id, permission_id=permission.id)
        for permission in permissions
    ])

    restricted_reader = User(username="pentest-reader-restricted", password="x", role="reviewer", status=1)
    db.add(restricted_reader)
    db.flush()
    db.add_all([
        UserRole(user_id=restricted_reader.id, role_id=reviewer_role.id),
        ProjectMember(project_id=source_project.id, user_id=restricted_reader.id),
    ])

    admin = User(username="pentest-report-admin", password="x", role="admin", status=1)
    db.add(admin)
    db.flush()
    admin_role = db.query(Role).filter_by(code="admin").one_or_none()
    if admin_role is None:
        admin_role = Role(code="admin", name="系统管理员", status="active", is_builtin=1)
        db.add(admin_role)
        db.flush()
    db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    db.commit()

    reviewer_result = pentest_service.list_engagements(db, reviewer, scope="all")
    reviewer_row = next(item for item in reviewer_result["items"] if item["public_id"] == engagement.public_id)
    owner_result = pentest_service.list_engagements(db, source_owner, scope="own")
    owner_row = next(item for item in owner_result["items"] if item["public_id"] == valid_engagement.public_id)
    duplicate_owner_row = next(
        item for item in owner_result["items"] if item["public_id"] == engagement.public_id
    )
    incomplete_owner_row = next(
        item for item in owner_result["items"] if item["public_id"] == incomplete_engagement.public_id
    )

    assert reviewer_row["can_view_report"] is False
    assert reviewer_row["report_task_id"] is None
    assert owner_row["can_view_report"] is True
    assert owner_row["report_task_id"] == valid_task.id
    assert duplicate_owner_row["can_view_report"] is False
    assert duplicate_owner_row["report_task_id"] is None
    assert incomplete_owner_row["can_view_report"] is False
    assert incomplete_owner_row["report_task_id"] is None
    report_view = next(permission for permission in permissions if permission.code == PermissionCode.REPORT_VIEW)
    pentest_view = next(permission for permission in permissions if permission.code == PermissionCode.PENTEST_VIEW)
    db.delete(db.query(RolePermission).filter_by(
        role_id=reviewer_role.id,
        permission_id=report_view.id,
    ).one())
    db.flush()
    assert rbac_service.check_permission(db, restricted_reader.id, PermissionCode.PENTEST_VIEW) is True
    assert rbac_service.check_permission(db, restricted_reader.id, PermissionCode.REPORT_VIEW) is False
    assert report_service.readable_pentest_engagement_ids(
        db, restricted_reader, [int(valid_engagement.id)]
    ) == set()

    db.add(RolePermission(role_id=reviewer_role.id, permission_id=report_view.id))
    db.delete(db.query(RolePermission).filter_by(
        role_id=reviewer_role.id,
        permission_id=pentest_view.id,
    ).one())
    db.flush()
    assert rbac_service.check_permission(db, restricted_reader.id, PermissionCode.REPORT_VIEW) is True
    assert rbac_service.check_permission(db, restricted_reader.id, PermissionCode.PENTEST_VIEW) is False
    assert report_service.readable_pentest_engagement_ids(
        db, restricted_reader, [int(valid_engagement.id)]
    ) == set()
    assert report_service.readable_pentest_engagement_ids(db, admin, [int(valid_engagement.id)]) == {
        int(valid_engagement.id)
    }


def _listed(db, owner, task):
    item = next(
        item for item in report_service.list_reports(db, owner, include_history=True)["items"]
        if item["task_id"] == task.id
    )
    assert item["can_view_report"] is True
    return item


@pytest.mark.parametrize("source", ["standard", "discuss"])
def test_review_sources_keep_final_issue_scoring(db, source_owner, source_project, source):
    task = _task(db, source_owner, source_project, source, count=99, score=89)
    task.coverage = {"coverage_status": "complete", "score_available": True}
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
    if source == "pentest":
        _pentest(db, task)
        expected_count = 5
    else:
        _snapshot(db, task, report_md="领域报告历史快照")
        expected_count = count
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    listed = _listed(db, source_owner, task)

    assert detail["task"]["total_issues"] == detail["stats"]["total_issues"] == listed["total_issues"] == expected_count
    expected_score = None if source == "sandbox_test" else score
    assert detail["task"]["score"] == listed["score"] == expected_score
    expected_score_source = "unavailable" if source == "sandbox_test" else source
    assert detail["stats"]["score_breakdown"]["score_source"] == expected_score_source
    assert "weights" not in detail["stats"]["score_breakdown"]
    assert detail["files"] == []
    assert detail["source"]["type"] == listed["source"]["type"] == source
    assert ReportDetailOut(**detail).model_dump()["source"] == detail["source"]
    assert ReportListItem(**listed).model_dump()["source"] == listed["source"]
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == expected_count
    assert task.total_issues == count
    assert db.query(ReviewIssue).filter_by(task_id=task.id).count() == 0


def test_sandbox_has_unclassified_findings_not_invented_severity(db, source_owner, source_project):
    task = _task(db, source_owner, source_project, "sandbox_test", count=3, score=100)
    _snapshot(db, task, public_id="sbx-source", report_md="## 问题清单\n- A\n- B\n- C\n")
    db.commit()

    detail = report_service.get_report_detail(db, source_owner, task.id)
    risk = {item["severity"]: item["count"] for item in dashboard_service.get_risk_distribution(db, source_owner)}

    assert detail["stats"]["severity"] == {"未分级": 3}
    assert detail["stats"]["risk_level"] == "未评估"
    assert detail["stats"]["severe"] == detail["stats"]["low"] == 0
    assert detail["source"]["detail_api"] == "/api/sandboxes/sbx-source"
    assert risk == {"严重": 0, "高": 0, "中": 0, "低": 0, "未分级": 3}
    assert dashboard_service.get_issue_type_statistics(db, source_owner) == [{"issue_type": "沙箱测试", "count": 3}]


@pytest.mark.parametrize("source", ["task", "report"])
@pytest.mark.parametrize("mode", ["whitebox", "blackbox", "combined"])
def test_sandbox_report_list_preserves_recorded_test_mode(db, source_owner, source_project, source, mode):
    task = _task(db, source_owner, source_project, "sandbox_test", count=1, score=75)
    _snapshot(db, task, public_id="sbx-mode", report_md="## 测试结果\n\n已记录模式。")
    if source == "task":
        task.coverage = {**task.coverage, "requested_mode": mode}
    else:
        snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=task.user_id).one()
        snapshot.content_json = {**snapshot.content_json, "coverage": {"requested_mode": mode}}
    db.commit()

    listed = _listed(db, source_owner, task)

    assert listed["source"]["test_mode"] == mode


def test_sandbox_report_list_keeps_missing_mode_explicitly_unknown(db, source_owner, source_project):
    task = _task(db, source_owner, source_project, "sandbox_test", count=1, score=75)
    _snapshot(db, task, public_id="sbx-mode-unknown", report_md="## 测试结果\n\n历史记录未保存模式。")
    db.commit()

    listed = _listed(db, source_owner, task)

    assert "test_mode" not in listed["source"]


@pytest.mark.parametrize("invalid_mode", ["not-a-mode", {"mode": "blackbox"}, ["blackbox"], 7])
def test_sandbox_report_list_ignores_invalid_recorded_mode_and_uses_next_valid_source(
    db, source_owner, source_project, invalid_mode,
):
    task = _task(db, source_owner, source_project, "sandbox_test", count=1, score=75)
    _snapshot(db, task, public_id="sbx-mode-invalid", report_md="## 测试结果\n\n模式来源存在异常。")
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=task.user_id).one()
    snapshot.content_json = {**snapshot.content_json, "coverage": {"requested_mode": invalid_mode}}
    task.coverage = {**task.coverage, "test_mode": "blackbox"}
    db.commit()

    listed = _listed(db, source_owner, task)

    assert listed["source"]["test_mode"] == "blackbox"


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
            task.coverage = {"coverage_status": "complete", "score_available": True}
            _issues(db, task)
        elif source == "pentest":
            _pentest(db, task)
        else:
            _snapshot(db, task)
        tasks.append(task)
    db.commit()

    listed = report_service.list_reports(db, source_owner, include_history=True)["items"]
    details = [report_service.get_report_detail(db, source_owner, task.id) for task in tasks]
    summary = dashboard_service.get_summary(db, source_owner)

    assert sum(item["total_issues"] for item in listed) == sum(item["stats"]["total_issues"] for item in details) == 12
    assert summary["total_issues"] == 12
    assert summary["review_count"] == 4
    # 讨论和领域测试保留各自评分，不混入明确代码审查类型的均分。
    assert summary["code_review_count"] == 1
    assert summary["avg_score"] == 89
    assert {item["score"] for item in summary["recent_tasks"]} == {84, 89, None}
    assert {item["task_id"]: item["score"] for item in dashboard_service.get_score_trend(db, source_owner)} == {
        tasks[0].id: 89, tasks[1].id: 89, tasks[3].id: 84,
    }
    assert sum(item["count"] for item in dashboard_service.get_risk_distribution(db, source_owner)) == 10
    assert sum(item["count"] for item in dashboard_service.get_issue_type_statistics(db, source_owner)) == 10


def test_score_trend_omits_successful_tasks_without_a_valid_score(db, source_owner, source_project):
    """越界的历史评分不能让评分趋势接口返回 500 或伪造异常分数。"""
    scored = _task(db, source_owner, source_project, "standard", count=0, score=87)
    scored.coverage = {"coverage_status": "complete", "score_available": True}
    unscored = _task(db, source_owner, source_project, "sandbox_test", count=4, score=101)
    _snapshot(db, unscored, public_id="sbx-unscored", report_md="## 问题清单\n- A\n")
    db.commit()

    trend = dashboard_service.get_score_trend(db, source_owner, limit=10)

    assert [row["task_id"] for row in trend] == [scored.id]
    assert trend[0]["score"] == 87


def test_dashboard_score_average_omits_unverified_historical_scores(db, source_owner, source_project):
    """缺少覆盖证明的旧分数不能混入工作台均分与有效样本数。"""
    task = _task(db, source_owner, source_project, "standard", count=0, score=87)
    db.commit()

    summary = dashboard_service.get_summary(db, source_owner)

    assert summary["avg_score"] == 0
    assert summary["code_review_count"] == 0
    # 最近任务仍展示按历史问题集复算的分数；只是不混入完整覆盖均分。
    assert next(item for item in summary["recent_tasks"] if item["id"] == task.id)["score"] == 87


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
@pytest.mark.parametrize("score", [0, 100])
def test_empty_source_keeps_explicit_score(db, source_owner, source_project, source, score):
    task = _task(db, source_owner, source_project, source, score=score)
    if source in {"standard", "discuss"}:
        task.coverage = {"coverage_status": "complete", "score_available": True}
    elif source == "sandbox_test":
        _snapshot(db, task)
    elif source == "pentest":
        _pentest(db, task, include_findings=False)
    db.commit()
    detail = report_service.get_report_detail(db, source_owner, task.id)

    assert detail["stats"]["total_issues"] == _listed(db, source_owner, task)["total_issues"] == 0
    expected_score = None if source == "sandbox_test" else score
    assert detail["stats"]["score"] == _listed(db, source_owner, task)["score"] == expected_score
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 0


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
def test_failed_sources_preserve_availability_and_success_counts(db, source_owner, source_project, source):
    task = _task(db, source_owner, source_project, source, count=2, score=0, status="failed")
    if source in {"standard", "discuss"}:
        _issues(db, task)
    elif source == "sandbox_test":
        _snapshot(db, task)
    elif source == "pentest":
        _pentest(db, task)
    db.commit()

    expected_count = 5 if source == "pentest" else 2
    assert report_service.get_report_detail(db, source_owner, task.id)["stats"]["total_issues"] == expected_count
    assert _listed(db, source_owner, task)["status"] == "failed"
    # 失败但已有审查结果的报告按已审范围进入默认列表，不再只能在历史中找到。
    assert report_service.list_reports(db, source_owner)["total"] == 1
    assert report_service.list_reports(db, source_owner, include_history=True)["total"] == 1
    summary = dashboard_service.get_summary(db, source_owner)
    assert summary["review_count"] == 0
    assert summary["avg_score"] == 0
    assert summary["recent_tasks"] == []
    assert summary["total_issues"] == expected_count


@pytest.mark.parametrize("source", ["standard", "discuss", "sandbox_test", "pentest"])
def test_task_owner_and_project_member_scopes_are_not_expanded(db, source_owner, source_project, admin_user, source):
    task = _task(db, source_owner, source_project, source, count=2)
    if source in {"standard", "discuss"}:
        _issues(db, task)
    elif source == "sandbox_test":
        _snapshot(db, task)
    elif source == "pentest":
        _pentest(db, task)
    member = User(username="source-member", password="x", role="user", status=1)
    outsider = User(username="source-outsider", password="x", role="user", status=1)
    db.add_all([member, outsider])
    db.flush()
    db.add(ProjectMember(project_id=source_project.id, user_id=member.id))
    db.commit()

    for reader in [source_owner, admin_user]:
        expected_count = 5 if source == "pentest" else 2
        assert report_service.get_report_detail(db, reader, task.id)["stats"]["total_issues"] == expected_count
    for reader in [member, outsider]:
        with pytest.raises(NotFoundError):
            report_service.get_report_detail(db, reader, task.id)
        assert report_service.list_reports(db, reader)["total"] == 0
    # 项目成员可见普通审查指标，但私域报告仍要求发起人/管理员及 report:view。
    assert dashboard_service.get_summary(db, member)["total_issues"] == (
        0 if source in {"sandbox_test", "pentest"} else 2
    )
    assert dashboard_service.get_summary(db, outsider)["total_issues"] == 0


def test_dashboard_hides_foreign_report_cards_but_keeps_project_member_score(db, source_owner, source_project):
    owner_task = _task(db, source_owner, source_project, "standard", score=88)
    owner_task.coverage = {"coverage_status": "complete", "score_available": True}
    _issues(db, owner_task)
    member = User(username="dashboard-private-member", password="x", role="user", status=1)
    db.add(member)
    db.flush()
    db.add(ProjectMember(project_id=source_project.id, user_id=member.id))
    db.commit()

    summary = dashboard_service.get_summary(db, member)
    assert summary["recent_tasks"] == []
    trend = dashboard_service.get_score_trend(db, member)
    assert [(row["task_id"], row["score"]) for row in trend] == [(owner_task.id, 88)]


def test_dashboard_hides_pentest_card_for_account_without_pentest_permission(db, source_owner, source_project):
    reader = User(username="dashboard-no-pentest", password="x", role="user", status=1)
    db.add(reader)
    db.flush()
    role = Role(name="仅报告读取", code="report-reader-only", status="active", is_builtin=0)
    db.add(role)
    db.flush()
    permission = db.query(Permission).filter_by(code=PermissionCode.REPORT_VIEW).one_or_none()
    if permission is None:
        permission = Permission(code=PermissionCode.REPORT_VIEW, name="报告查看", module="report", type="api")
        db.add(permission)
        db.flush()
    db.add_all([
        UserRole(user_id=reader.id, role_id=role.id),
        RolePermission(role_id=role.id, permission_id=permission.id),
        ProjectMember(project_id=source_project.id, user_id=reader.id),
    ])
    task = _task(db, reader, source_project, "pentest", score=84)
    _pentest(db, task)
    db.commit()

    summary = dashboard_service.get_summary(db, reader)
    assert summary["recent_tasks"] == []
    assert dashboard_service.get_score_trend(db, reader) == []
    reports = report_service.list_reports(db, reader)
    assert reports["total"] == 0
    assert reports["items"] == []


def test_report_list_fails_closed_without_report_permission_and_hides_sensitive_fields(db):
    reader = User(username="report-no-permission", password="x", role="reviewer", status=1)
    role = Role(name="无报告权限审查员", code="reviewer", status="active", is_builtin=0)
    db.add_all([reader, role])
    db.flush()
    db.add(UserRole(user_id=reader.id, role_id=role.id))
    project = Project(user_id=reader.id, project_name="无权限报告", status="active")
    db.add(project)
    db.flush()
    task = _task(db, reader, project, "sandbox_test", count=1, score=80, status="failed")
    task.error_message = "敏感执行错误"
    task.coverage = {"tested": ["敏感目标"], "unscanned": ["敏感未扫描范围"]}
    _snapshot(db, task, public_id="private-sandbox-id", report_md="私有报告正文")
    db.commit()

    result = report_service.list_reports(db, reader)

    # 权限在 count 与分页前应用；无权任务不形成卡片，也不泄露 source/error/coverage。
    assert result["total"] == 0
    assert result["items"] == []
    assert "敏感执行错误" not in repr(result)
    assert "敏感目标" not in repr(result)
    assert "private-sandbox-id" not in repr(result)


@pytest.mark.parametrize("coverage_status,expected_score", [("complete", 84), ("partial", None), ("unknown", None)])
@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_domain_score_visibility_is_consistent_across_report_and_dashboard(
    db, source_owner, source_project, source, coverage_status, expected_score,
):
    if source == "sandbox_test":
        expected_score = None
    task = _task(db, source_owner, source_project, source, score=84)
    task.coverage = (
        {
            "coverage_status": coverage_status,
            "score_available": coverage_status == "complete",
            "tested": ["known scope"],
            "unscanned": [],
        }
        if coverage_status != "unknown" else {}
    )
    if source == "pentest":
        _pentest(db, task)
    else:
        _snapshot(db, task, public_id=f"sbx-{task.id}", report_md="## 范围\n合成覆盖数据")
    db.commit()

    listed = _listed(db, source_owner, task)
    detail = report_service.get_report_detail(db, source_owner, task.id)
    summary = dashboard_service.get_summary(db, source_owner)
    trend = dashboard_service.get_score_trend(db, source_owner)
    recent = next(item for item in summary["recent_tasks"] if item["id"] == task.id)

    assert listed["score"] == detail["task"]["score"] == detail["stats"]["score"] == expected_score
    assert recent["score"] == expected_score
    assert ([item["task_id"] for item in trend] == [task.id]) == (expected_score is not None)
    if expected_score is not None:
        assert trend[0]["score"] == expected_score


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

    assert report_service.list_reports(db, source_owner)["items"] == []
    with pytest.raises(ConflictError):
        report_service.get_report_detail(db, source_owner, task.id)
    # 仪表盘保留任务自身的聚合计数，但不泄露外部委托的发现明细。
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
    prior = _task(db, source_owner, source_project, "standard", score=90)
    prior.coverage = {"coverage_status": "complete", "score_available": True}
    zero = _task(db, source_owner, source_project, "standard", score=0)
    zero.coverage = {"coverage_status": "complete", "score_available": True}
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


@pytest.mark.parametrize("passed,expected_status", [(True, "success"), (False, "failed")])
def test_real_sandbox_publisher_report_and_dashboard_close_the_count_loop(
    db, source_owner, source_project, passed, expected_status,
):
    from app.services.sandbox_service import _publish_sandbox_report

    published = _publish_sandbox_report(
        db,
        SimpleNamespace(public_id="sbx-published", owner_id=source_owner.id, project_id=source_project.id),
        {
            "passed": passed,
            "agent_tests": {"generated": 2, "passed_count": 1},
            "coverage": {"coverage_status": "complete", "tested": ["合成测试范围"], "unscanned": []},
        },
        "## 总体结论\n合成结果\n## 问题清单\n- A\n- B\n- C\n",
    )
    task = db.get(ReviewTask, published["report_task_id"])
    detail = report_service.get_report_detail(db, source_owner, task.id)

    assert detail["stats"]["total_issues"] == _listed(db, source_owner, task)["total_issues"] == 3
    # 本用例未提供动态用例生成和执行回执，不能从 passed 字段推导评分。
    assert detail["stats"]["score"] is _listed(db, source_owner, task)["score"] is None
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 3
    assert task.status == expected_status


def test_real_pentest_publisher_and_domain_detail_agree(db, source_owner, source_project):
    from app.services.pentest_service import TARGET_TYPE_LABELS, _publish_pentest_report, get_engagement_detail

    task = _task(db, source_owner, source_project, "pentest", count=999, score=84)
    engagement = _pentest(db, task)
    task.task_name = f"授权渗透测试 · {TARGET_TYPE_LABELS[engagement.target_type]} · {engagement.public_id}"
    db.flush()
    findings = db.query(PentestFinding).filter_by(engagement_id=engagement.id).all()
    published = _publish_pentest_report(
        db, engagement, findings, "合成渗透报告", {"严重": 1, "高": 1, "提示": 1}, {"高": 1}, 84, {}, {}, "高风险",
        coverage={"coverage_status": "complete", "tested": ["合成授权目标"], "unscanned": []},
    )
    domain = get_engagement_detail(db, source_owner, engagement.public_id)
    detail = report_service.get_report_detail(db, source_owner, published.id)

    assert published.id == task.id
    assert len(domain["findings"]) == detail["stats"]["total_issues"] == 5
    assert detail["stats"]["refuted"] == 2
    assert dashboard_service.get_summary(db, source_owner)["total_issues"] == 5
    engagement.report_task_id = None
    db.commit()
    assert report_service.list_reports(db, source_owner)["items"] == []
    with pytest.raises(ConflictError):
        report_service.get_report_detail(db, source_owner, published.id)


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
    report_reader = Role(name="普通用户", code="user", status="active", is_builtin=1)
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
        task.coverage = {"coverage_status": "complete", "score_available": True}
        _issues(db, task)
    elif source == "pentest":
        _pentest(db, task)
    else:
        _snapshot(db, task)
    db.commit()

    response = client.get("/api/reports", params={"include_history": source == "sandbox_test"})
    assert response.status_code == 200
    listed = response.json()["data"]["items"][0]
    assert listed["can_view_report"] is True
    detail_response = client.get(f"/api/reports/{task.id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()["data"]
    dashboard_response = client.get("/api/dashboard/summary")
    assert dashboard_response.status_code == 200
    assert listed["total_issues"] == detail["stats"]["total_issues"] == count
    assert listed["source"]["type"] == detail["source"]["type"] == source
    if source == "sandbox_test":
        assert isinstance(detail["source"].get("domain_report_md"), str)
        assert "domain_report_md" not in listed["source"]
    elif source == "pentest":
        assert isinstance(detail["source"].get("domain_report_md"), str)
        assert listed["source"].get("public_id") == detail["source"].get("public_id")
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


def test_skipped_ai_review_status_and_reason_are_preserved_in_list_and_detail(
    source_api_client,
):
    client, db, owner, _other, project, _current = source_api_client
    task = _task(db, owner, project, "sandbox_test")
    skip_reason = "精确 HTTP 白名单任务按策略不启动多 Agent AI 评审。"
    _snapshot(
        db,
        task,
        ai_review={"status": "skipped", "failure_reason": None, "skip_reason": skip_reason},
    )
    db.commit()

    listed = client.get("/api/reports", params={"include_history": True}).json()["data"]["items"][0]
    detail = client.get(f"/api/reports/{task.id}").json()["data"]
    expected = {
        "status": "skipped",
        "failure_reason": None,
        "skip_reason": skip_reason,
    }
    assert listed["source"]["ai_review"] == expected
    assert detail["source"]["ai_review"] == expected


@pytest.mark.parametrize("bad_content", [None, [], "invalid", {"source": "other", "public_id": "foreign"}])
def test_invalid_domain_snapshot_does_not_override_task_facts(db, source_owner, source_project, bad_content):
    task = _task(db, source_owner, source_project, "sandbox_test", count=3, score=0)
    snapshot = _snapshot(db, task)
    snapshot.content_json = bad_content
    db.commit()

    assert report_service.list_reports(db, source_owner)["items"] == []
    with pytest.raises(ConflictError):
        report_service.get_report_detail(db, source_owner, task.id)


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
