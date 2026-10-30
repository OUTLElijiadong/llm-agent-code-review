"""报告详情的 8 维度评分：标准审查与圆桌共用同一条读取路径，历史圆桌读取时即时派生。

生产圆桌任务 #236（文件 timed.rst，12 个问题，评分 62）的报告雷达显示「暂无 8 维度数据」，
因为任何审查路径都没有写出 ``stats.dim_scores``。这里用已保存的 ReviewIssue 行证明：
读取时按既有规则派生、不回写数据库、无依据的维度是“无数据”。
"""
from __future__ import annotations

import json

import pytest

from app.models.custom_agent import ReviewTaskAgentRelease  # noqa: F401 - 注册报告关联表
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.services.report_service import get_report_detail

# #236 的形态：12 个问题，集中在安全、异常、可维护性三类，其余维度没有记录。
ISSUES_236 = (
    [("安全漏洞", "高")] * 3 + [("安全漏洞", "中")] * 2
    + [("异常处理", "中")] * 2 + [("异常处理", "低")]
    + [("可维护性", "低")] * 3 + [("其他", "低")]
)


def _project(db, admin_user) -> Project:
    project = db.query(Project).first()
    if project is None:
        project = Project(user_id=admin_user.id, project_name="timed 项目", language="rst")
        db.add(project)
        db.commit()
    return project


def _task(db, admin_user, *, review_type="discuss", issues=ISSUES_236, status="success", coverage=None,
          score=62, **extra) -> ReviewTask:
    project = _project(db, admin_user)
    task = ReviewTask(
        user_id=admin_user.id, project_id=project.id, task_name="timed.rst · 圆桌讨论审",
        review_type=review_type, status=status, total_files=1, processed_files=1,
        total_issues=len(issues), score=score,
        coverage=coverage if coverage is not None else {"coverage_status": "complete", "score_available": True},
        **extra,
    )
    db.add(task)
    db.commit()
    db.add_all([
        ReviewIssue(task_id=task.id, issue_type=kind, severity=severity, title=f"问题{index}", description="d",
                    file_name="timed.rst")
        for index, (kind, severity) in enumerate(issues)
    ])
    db.commit()
    return task


def _dims(detail: dict) -> dict:
    return {item["key"]: item for item in detail["stats"]["dimension_scores"]["items"]}


def test_roundtable_report_carries_eight_dimensions_derived_from_its_saved_issues(db, admin_user):
    task = _task(db, admin_user)

    detail = get_report_detail(db, admin_user, task.id)
    dims = _dims(detail)

    assert list(dims) == ["style", "naming", "comment", "maintain", "performance", "exception", "logic", "security"]
    assert dims["security"]["score"] == 100 - 3 * 8 - 2 * 3          # 70
    assert dims["exception"]["score"] == 100 - 2 * 3 - 1             # 93
    assert dims["maintain"]["score"] == 100 - 3 * 1                  # 97
    assert detail["stats"]["dim_scores"] == {"security": 70, "exception": 93, "maintain": 97}


def test_dimensions_without_evidence_are_no_data_not_zero(db, admin_user):
    task = _task(db, admin_user)

    dims = _dims(get_report_detail(db, admin_user, task.id))

    for key in ("style", "naming", "comment", "performance", "logic"):
        assert dims[key]["status"] == "no_data"
        assert dims[key]["score"] is None
        assert "没有记录" in dims[key]["reason"]
    assert "style" not in get_report_detail(db, admin_user, task.id)["stats"]["dim_scores"]


def test_issues_outside_the_eight_dimensions_are_reported_as_unmapped(db, admin_user):
    task = _task(db, admin_user)

    summary = get_report_detail(db, admin_user, task.id)["stats"]["dimension_scores"]

    assert summary["unmapped_issue_count"] == 1
    assert summary["scored_count"] == 3 and summary["no_data_count"] == 5


def test_standard_review_uses_the_same_derivation_as_the_roundtable(db, admin_user):
    roundtable = _task(db, admin_user, review_type="discuss")
    standard = _task(db, admin_user, review_type="standard")

    assert get_report_detail(db, admin_user, roundtable.id)["stats"]["dim_scores"] == \
        get_report_detail(db, admin_user, standard.id)["stats"]["dim_scores"]


def test_historical_completed_roundtable_is_recomputed_on_read_without_writing_back(db, admin_user):
    """历史任务只有 ReviewIssue 行、任务表里没有任何维度字段：读取即得，且不改动数据库。"""
    task = _task(db, admin_user, coverage={"stage": "complete", "coverage_status": "complete"})
    before = (
        json.dumps(task.coverage, sort_keys=True), task.score, task.score_version,
        json.dumps(task.score_breakdown, sort_keys=True), task.total_issues,
    )

    first = get_report_detail(db, admin_user, task.id)
    second = get_report_detail(db, admin_user, task.id)
    db.expire_all()
    saved = db.get(ReviewTask, task.id)

    assert first["stats"]["dim_scores"] == second["stats"]["dim_scores"] == {
        "security": 70, "exception": 93, "maintain": 97,
    }
    assert not db.dirty and not db.new
    assert (
        json.dumps(saved.coverage, sort_keys=True), saved.score, saved.score_version,
        json.dumps(saved.score_breakdown, sort_keys=True), saved.total_issues,
    ) == before


def test_refuted_issues_do_not_lower_any_dimension(db, admin_user):
    task = _task(db, admin_user, issues=[("安全漏洞", "高"), ("安全漏洞", "严重")])
    refuted = db.query(ReviewIssue).filter_by(task_id=task.id, severity="严重").one()
    refuted.status = "refuted"
    db.commit()

    dims = _dims(get_report_detail(db, admin_user, task.id))

    assert dims["security"]["score"] == 92
    assert dims["security"]["issue_count"] == 1


def test_a_task_with_no_issues_has_no_dimension_data_instead_of_a_full_radar(db, admin_user):
    task = _task(db, admin_user, issues=[], score=100)

    stats = get_report_detail(db, admin_user, task.id)["stats"]

    assert stats["dim_scores"] == {}
    assert stats["dimension_scores"]["scored_count"] == 0
    assert all(item["status"] == "no_data" for item in stats["dimension_scores"]["items"])


def test_unavailable_overall_score_hides_the_dimensions_with_the_reason(db, admin_user):
    task = _task(db, admin_user, coverage={"coverage_status": "complete", "score_available": False,
                                           "score_unavailable_reason": "ungraded_findings"})
    db.add(ReviewIssue(task_id=task.id, issue_type="安全漏洞", severity="未分级", title="x", description="d"))
    db.commit()

    stats = get_report_detail(db, admin_user, task.id)["stats"]

    assert stats["dim_scores"] == {}
    assert stats["dimension_scores"]["available"] is False


@pytest.mark.parametrize("review_type", ["sandbox_test", "pentest"])
def test_domain_reports_do_not_get_code_quality_dimensions(db, admin_user, review_type):
    """沙箱/渗透不是按代码问题类别评分的报告，不派生 8 维度（它们也不画雷达）。"""
    from app.services.report_service import should_derive_dimension_scores

    task = _task(db, admin_user, review_type=review_type, issues=[], score=90, score_version="x")

    assert should_derive_dimension_scores(task) is False


@pytest.mark.parametrize("review_type", ["quick", "standard", "security", "performance", "full", "discuss",
                                         "repo_discuss", "security_scan", "fullchain_audit"])
def test_every_issue_based_review_type_derives_dimensions(db, admin_user, review_type):
    from app.services.report_service import should_derive_dimension_scores

    task = _task(db, admin_user, review_type=review_type, issues=[])

    assert should_derive_dimension_scores(task) is True
