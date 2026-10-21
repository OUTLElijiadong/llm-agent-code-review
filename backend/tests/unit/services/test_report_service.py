"""报告服务回归测试。"""
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.models.code_file import CodeFile
from app.models.custom_agent import (
    ReviewTaskAgentRelease,  # noqa: F401 - registers the related report tables for metadata.create_all
)
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.services.report_service import (
    _sandbox_requested_mode,
    display_report_score,
    get_report_detail,
    list_reports,
)


@pytest.mark.parametrize(
    ("status", "coverage", "score", "expected"),
    [
        # 没有任何已审文件或问题：不出分，绝不用 0 占位。
        ("failed", None, 0, None),
        ("cancelled", None, 82, None),
        ("success", {"coverage_status": "partial", "score_available": True}, 82, None),
        # 已审部分文件：按已保存问题计数复算已审范围分（2 高 1 低 = 83）。
        ("partial", {"coverage_status": "partial", "completed_files": 1, "score_available": True}, 100, 83),
        ("failed", {"coverage_status": "failed", "completed_files": 2}, 0, 83),
        (
            "success",
            {"coverage_status": "complete", "verification_status": "failed", "completed_files": 1},
            82,
            83,
        ),
        ("success", {"coverage_status": "complete", "score_available": True}, 82, 82),
        # 无覆盖账本的历史成功任务按记录分展示（historical）。
        ("success", None, 100, 100),
    ],
)
def test_report_score_follows_reviewed_scope(status, coverage, score, expected):
    task = SimpleNamespace(
        status=status, coverage=coverage, review_type="standard", total_files=3, processed_files=0,
        total_issues=0, severe_issues=0, high_issues=2, medium_issues=0, low_issues=1, score_version=None,
    )

    assert display_report_score(task, score) == expected


def _make_project(db, admin_user):
    """创建报告测试项目。

    Args:
        db: 数据库会话。
        admin_user: 管理员测试用户。

    Returns:
        Project: 已持久化的项目。
    """
    project = Project(user_id=admin_user.id, project_name="报告项目", language="python")
    db.add(project)
    db.commit()
    return project


def _make_file(db, project, name):
    """创建报告测试代码文件。

    Args:
        db: 数据库会话。
        project: 所属测试项目。
        name: 文件名。

    Returns:
        CodeFile: 已持久化的代码文件。
    """
    code_file = CodeFile(
        project_id=project.id,
        file_name=name,
        file_path=name,
        language="python",
        content="print('qa')\n",
    )
    db.add(code_file)
    db.commit()
    return code_file


def test_report_detail_lists_linked_files_including_clean_file(db, admin_user):
    """报告应列出任务关联的全部文件，包括没有问题的文件。"""
    project = _make_project(db, admin_user)
    flagged = _make_file(db, project, "flagged.py")
    clean = _make_file(db, project, "clean.py")
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        status="success",
        total_files=2,
        processed_files=2,
        total_issues=1,
        severe_issues=1,
        score=85,
        rules_snapshot=[{"code": "security", "name": "安全漏洞"}],
    )
    db.add(task)
    db.commit()
    db.add_all([
        ReviewTaskFile(task_id=task.id, file_id=flagged.id),
        ReviewTaskFile(task_id=task.id, file_id=clean.id),
        ReviewIssue(
            task_id=task.id,
            file_id=flagged.id,
            file_name=flagged.file_name,
            issue_type="安全漏洞",
            severity="严重",
            title="风险",
            description="desc",
            status="fixed",
        ),
    ])
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)

    assert [row["file_name"] for row in detail["files"]] == ["flagged.py", "clean.py"]
    assert detail["files"][0]["issue_count"] == 1
    assert detail["files"][1]["issue_count"] == 0
    assert detail["stats"]["fixed"] == 1
    assert detail["stats"]["severity"] == {"严重": 1}


def test_report_detail_falls_back_to_issue_files_for_legacy_task(db, admin_user):
    """旧任务没有关联记录时仍应按历史问题展示文件。"""
    project = _make_project(db, admin_user)
    code_file = _make_file(db, project, "legacy.py")
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        status="success",
        total_files=1,
        processed_files=1,
        total_issues=1,
        high_issues=1,
        score=92,
    )
    db.add(task)
    db.commit()
    db.add(ReviewIssue(
        task_id=task.id,
        file_id=code_file.id,
        file_name=code_file.file_name,
        issue_type="潜在Bug",
        severity="高",
        title="旧数据问题",
        description="desc",
    ))
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)

    assert len(detail["files"]) == 1
    assert detail["files"][0]["file_name"] == "legacy.py"
    assert detail["files"][0]["language"] == "python"


def test_report_detail_uses_final_issue_set_for_all_score_facts(db, admin_user):
    """详情不得继续返回任务表中的旧问题数、旧评分或旧 breakdown。"""
    project = _make_project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="事实源任务",
        status="success",
        total_issues=99,
        severe_issues=0,
        high_issues=99,
        medium_issues=0,
        low_issues=0,
        score=5,
        coverage={"coverage_status": "complete", "score_available": True},
        score_version="legacy-v0",
        score_breakdown={"score": 5, "risk_level": "低风险"},
    )
    db.add(task)
    db.commit()
    db.add_all([
        ReviewIssue(
            task_id=task.id,
            issue_type="安全漏洞",
            severity="严重",
            title="严重问题",
            description="desc",
            status="fixed",
        ),
        ReviewIssue(
            task_id=task.id,
            issue_type="潜在Bug",
            severity="高",
            title="高风险问题",
            description="desc",
            status="unfixed",
        ),
    ])
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)
    listed = next(item for item in list_reports(db, admin_user)["items"] if item["task_id"] == task.id)

    assert detail["task"]["task_name"] == "事实源任务"
    assert detail["task"]["name"] == "事实源任务"
    assert detail["task"]["score"] == 77
    assert detail["stats"]["total_issues"] == 2
    assert detail["stats"]["score"] == 77
    assert detail["stats"]["severe"] == 1
    assert detail["stats"]["high"] == 1
    assert detail["stats"]["medium"] == 0
    assert detail["stats"]["low"] == 0
    assert detail["stats"]["fixed"] == 1
    assert detail["stats"]["score_version"] == "severity-deduction-v1"
    assert detail["stats"]["score_breakdown"]["weights"] == {
        "严重": 15,
        "高": 8,
        "中": 3,
        "低": 1,
    }
    assert detail["stats"]["score_breakdown"]["score_source"] == "review_issues"
    assert detail["stats"]["risk_level"] == "中风险"
    assert listed["total_issues"] == detail["stats"]["total_issues"] == 2
    assert listed["score"] == detail["stats"]["score"] == 77


def test_report_detail_preserves_explicit_score_for_complete_empty_historical_task(db, admin_user):
    """完整覆盖的无问题历史任务保留显式分数，不被默认值覆盖。"""
    project = _make_project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="无问题任务",
        status="success",
        total_issues=0,
        score=100,
        coverage={"coverage_status": "complete", "score_available": True},
    )
    db.add(task)
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)

    assert detail["task"]["score"] == 100
    assert detail["stats"]["total_issues"] == 0
    assert detail["stats"]["score"] == 100
    assert detail["stats"]["score_breakdown"]["score"] == 100
    assert detail["stats"]["score_breakdown"]["score_source"] == "task_explicit_empty_report"
    assert detail["stats"]["risk_level"] == "低风险"


def test_refuted_issue_remains_counted_but_does_not_affect_report_score(db, admin_user):
    project = _make_project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="已排除问题",
        status="success",
        total_issues=1,
        severe_issues=1,
        score=45,
        coverage={"coverage_status": "complete", "score_available": True},
    )
    db.add(task)
    db.commit()
    db.add(ReviewIssue(
        task_id=task.id,
        issue_type="安全漏洞",
        severity="严重",
        title="误报",
        description="人工确认不构成问题",
        status="refuted",
    ))
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)
    listed = next(item for item in list_reports(db, admin_user)["items"] if item["task_id"] == task.id)

    assert detail["stats"]["total_issues"] == 1
    assert detail["stats"]["refuted"] == 1
    assert detail["stats"]["severe"] == 0
    assert detail["stats"]["score"] == 100
    assert listed["score"] == detail["stats"]["score"]


def test_legacy_report_without_coverage_scores_historically_and_explains_unknown_scope(db, admin_user):
    project = _make_project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="缺失覆盖快照的历史任务",
        status="success",
        total_issues=0,
        score=100,
    )
    db.add(task)
    empty_coverage_task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="空覆盖快照的历史任务",
        status="success",
        total_issues=0,
        score=100,
        coverage={},
    )
    db.add(empty_coverage_task)
    verification_only_task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="仅核验完成的历史任务",
        status="success",
        total_issues=0,
        score=100,
        coverage={"verification_status": "complete", "score_available": True},
    )
    db.add(verification_only_task)
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)
    listed = next(
        item for item in list_reports(db, admin_user, include_history=True)["items"]
        if item["task_id"] == task.id
    )

    # 无覆盖账本的历史成功任务按已保存问题集复算出分，同时明确覆盖范围未知。
    assert detail["task"]["score"] == 100
    assert detail["stats"]["score"] == 100
    assert detail["task"]["score_scope"]["scope"] == "historical"
    assert detail["task"]["coverage"]["coverage_status"] == "unknown"
    assert listed["score"] == 100
    assert listed["coverage"]["coverage_status"] == "unknown"
    default_items = list_reports(db, admin_user)["items"]
    assert task.id in {item["task_id"] for item in default_items}
    assert empty_coverage_task.id not in {item["task_id"] for item in default_items}
    assert verification_only_task.id not in {item["task_id"] for item in default_items}


def test_report_list_hides_failed_and_unscored_history_by_default_but_can_include_it(
    db, admin_user, monkeypatch,
):
    """演示列表默认只展示成功且有评分依据的记录；历史筛选显式恢复查看，不删除数据。"""
    from app.services import report_service

    project = _make_project(db, admin_user)
    valid = ReviewTask(
        user_id=admin_user.id, project_id=project.id, task_name="有评分的报告", status="success",
        coverage={"coverage_status": "complete", "verification_status": "complete", "score_available": True},
        score=88,
    )
    failed = ReviewTask(
        user_id=admin_user.id, project_id=project.id, task_name="失败历史", status="failed",
        coverage={"coverage_status": "failed", "verification_status": "failed", "score_available": False},
        score=0,
    )
    unscored = ReviewTask(
        user_id=admin_user.id, project_id=project.id, task_name="无评分历史", status="success",
        coverage={"coverage_status": "partial", "verification_status": "complete", "score_available": False},
        score=0,
    )
    db.add_all([valid, failed, unscored])
    db.commit()

    monkeypatch.setattr(report_service.rbac_service, "check_permission", lambda *_args: True)
    monkeypatch.setattr(report_service.rbac_service, "is_admin_user", lambda *_args: True)
    monkeypatch.setattr(report_service, "_standard_report_permission_filter", lambda *_args: True)
    monkeypatch.setattr(report_service, "get_visible_project_ids", lambda *_args: ([project.id], None))
    monkeypatch.setattr(
        report_service, "project_resource_capabilities",
        lambda *_args, **_kwargs: {project.id: {"can_execute": True}},
    )
    monkeypatch.setattr(report_service, "has_source_report_permission", lambda *_args: True)

    default = list_reports(db, admin_user)
    history = list_reports(db, admin_user, include_history=True)

    assert [item["task_name"] for item in default["items"]] == ["有评分的报告"]
    assert default["total"] == 1
    assert {item["task_name"] for item in history["items"]} == {"有评分的报告", "失败历史", "无评分历史"}
    assert history["total"] == 3


def test_sandbox_report_detail_localizes_legacy_statuses_without_mutating_snapshot(db, admin_user):
    """详情页应使用与导出相同的可读状态，不直接暴露旧快照中的英文状态码。"""
    project = _make_project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="黑盒验证报告",
        review_type="sandbox_test",
        status="success",
        total_files=1,
        processed_files=1,
        total_issues=0,
        score=100,
        coverage={
            "coverage_status": "partial",
            "verification_status": "partial",
            "deterministic_status": "passed",
            "ai_dynamic_status": "skipped",
            "blackbox_status": "passed",
            "reason": "动态用例未执行",
        },
    )
    db.add(task)
    db.flush()
    original_markdown = (
        "## 总体结论\n\n"
        "确定性执行结论：passed\n\n"
        "覆盖状态：确定性检查=passed，AI 动态用例=skipped，黑盒路由=passed。\n"
    )
    snapshot = ReviewReport(
        task_id=task.id,
        user_id=admin_user.id,
        content_json={"source": "sandbox_test", "report_md": original_markdown},
        create_time=task.create_time,
    )
    db.add(snapshot)
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)

    markdown = detail["source"]["domain_report_md"]
    assert "确定性执行结论：已通过" in markdown
    assert "AI 动态用例=未执行" in markdown
    assert "黑盒路由=已通过" in markdown
    assert "passed" not in markdown
    assert "skipped" not in markdown
    db.refresh(snapshot)
    assert snapshot.content_json["report_md"] == original_markdown


def test_blackbox_report_detail_omits_unrequested_whitebox_findings_without_mutating_snapshot(db, admin_user):
    """历史黑盒报告不得把未请求的白盒证据缺失算成发现。"""
    project = _make_project(db, admin_user)
    report_md = (
        "## 总体结论\n\n黑盒路由未通过。\n\n"
        "## 白盒结果\n\n结论：未通过（无有效白盒证据）。\n\n"
        "## 发现\n\n### [高] 无任何白盒证据，白盒维度不可判定为通过。\n\n"
        "## 黑盒结果\n\n结论：未通过（路由探测失败）。\n\n"
        "## 问题清单（按严重度）\n\n### 高\n"
        "1. 黑盒路由探测未发现 2xx 路由。\n"
        "2. 无任何白盒证据，白盒维度不可判定为通过。\n"
        "  证据：本次请求模式为 blackbox。\n"
    )
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=project.id,
        task_name="沙箱黑白盒测试 · sbx_legacy",
        review_type="sandbox_test",
        status="failed",
        total_issues=2,
        score=100,
        coverage={"requested_mode": "blackbox", "verification_status": "failed"},
    )
    db.add(task)
    db.flush()
    snapshot = ReviewReport(
        task_id=task.id,
        user_id=admin_user.id,
        content_json={
            "source": "sandbox_test",
            "report_md": report_md,
            "coverage": {"requested_mode": "blackbox", "verification_status": "failed"},
        },
        create_time=task.create_time,
    )
    db.add(snapshot)
    db.commit()

    detail = get_report_detail(db, admin_user, task.id)
    displayed = detail["source"]["domain_report_md"]
    listed = next(
        item for item in list_reports(db, admin_user, include_history=True)["items"]
        if item["task_id"] == task.id
    )
    from app.services.report_service import get_domain_report_export

    exported = get_domain_report_export(db, admin_user, task, "json")

    assert "黑盒路由探测未发现 2xx 路由" in displayed
    assert "白盒验证未纳入范围" in displayed
    assert "无任何白盒证据" not in displayed
    assert "黑盒路由探测未发现 2xx 路由" in exported["domain_markdown"]
    assert "无任何白盒证据" not in exported["domain_markdown"]
    assert detail["stats"]["total_issues"] == 1
    assert detail["task"]["score"] is None
    assert listed["source"]["test_mode"] == "blackbox"
    assert listed["total_issues"] == detail["stats"]["total_issues"] == exported["statistics"]["total_issues"] == 1
    db.refresh(snapshot)
    assert snapshot.content_json["report_md"] == report_md


def test_legacy_sandbox_mode_resolves_from_deterministic_execution_evidence(db, admin_user):
    task = ReviewTask(
        user_id=admin_user.id,
        project_id=1,
        task_name="旧格式黑盒任务",
        review_type="sandbox_test",
        status="failed",
        total_files=0,
        processed_files=0,
        coverage={"requested_mode": None},
    )
    content = {
        "coverage": {"requested_mode": None},
        "evidence": {"deterministic_test_execution": {"requested_mode": "blackbox"}},
    }

    assert _sandbox_requested_mode(task, content) == "blackbox"


def test_report_list_fetches_projects_in_one_batch_query(db, admin_user, monkeypatch):
    """不同项目的报告列表不应为每一行单独读取 Project。"""
    projects = [
        Project(
            user_id=admin_user.id,
            project_name=f"批量报告项目 {index}",
            language="python",
        )
        for index in range(4)
    ]
    db.add_all(projects)
    db.commit()
    db.add_all([
        ReviewTask(
            user_id=admin_user.id,
            project_id=project.id,
            task_name=f"批量项目任务 {index}",
            status="success",
            score=100,
            coverage={
                "coverage_status": "complete",
                "verification_status": "complete",
                "score_available": True,
            },
        )
        for index, project in enumerate(projects)
    ])
    db.commit()
    project_ids = [project.id for project in projects]
    monkeypatch.setattr(
        "app.services.report_service.get_visible_project_ids",
        lambda *_args: (project_ids, None),
    )
    monkeypatch.setattr(
        "app.services.report_service.rbac_service.is_admin_user",
        lambda *_args: True,
    )
    db.expunge_all()
    project_selects = 0
    engine = db.get_bind()

    def count_project_queries(_conn, _cursor, statement, _params, _context, _many):
        nonlocal project_selects
        lowered = statement.lower()
        if lowered.lstrip().startswith("select") and (
            "\nfrom project\n" in lowered or "\nfrom project " in lowered
        ):
            project_selects += 1

    event.listen(engine, "before_cursor_execute", count_project_queries)
    try:
        result = list_reports(db, admin_user, page_size=4)
    finally:
        event.remove(engine, "before_cursor_execute", count_project_queries)

    assert result["total"] == 4
    assert {item["project_name"] for item in result["items"]} == {
        project.project_name for project in projects
    }
    assert project_selects == 1
