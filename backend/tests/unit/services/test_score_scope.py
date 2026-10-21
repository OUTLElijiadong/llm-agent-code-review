"""评分范围：覆盖不完整也按规则出分，只有没有任何已审内容时不出分。"""

from types import SimpleNamespace

from app.services import remote_pentest_service
from app.services.score_credibility import audit_report_score
from app.services.score_scope import issue_rule_score, issue_score_scope


def _task(**overrides):
    base = dict(
        status="success", coverage=None, review_type="standard", total_files=2, processed_files=0,
        total_issues=0, severe_issues=0, high_issues=0, medium_issues=0, low_issues=0,
        score=0, score_version=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_complete_coverage_is_complete_scope() -> None:
    scope = issue_score_scope(_task(coverage={"coverage_status": "complete"}, processed_files=2))
    assert scope["available"] is True
    assert scope["scope"] == "complete"


def test_partial_review_with_reviewed_files_is_scored() -> None:
    task = _task(status="failed", coverage={"coverage_status": "failed", "completed_files": 1}, high_issues=1)
    scope = issue_score_scope(task)
    assert scope["scope"] == "partial"
    assert issue_rule_score(task) == 92


def test_roundtable_valid_speeches_are_scored() -> None:
    task = _task(
        review_type="discuss", status="failed",
        coverage={"coverage_status": "partial", "valid_speeches": 2, "extraction_status": "success"},
    )
    assert issue_score_scope(task)["scope"] == "partial"


def test_nothing_reviewed_is_not_scored_and_explains_why() -> None:
    scope = issue_score_scope(_task(status="failed", coverage={"coverage_status": "failed"}))
    assert scope["available"] is False
    assert "没有任何已完成审查" in scope["reason"]


def test_legacy_success_without_ledger_is_historical() -> None:
    assert issue_score_scope(_task(coverage=None))["scope"] == "historical"


def test_failed_placeholder_zero_is_not_treated_as_rejected_score() -> None:
    task = _task(status="failed", coverage={"completed_files": 1}, score=0)
    audit = audit_report_score(task, 100, {"严重": 0, "高": 0, "中": 0, "低": 0}, 0,
                               score_scope=issue_score_scope(task))
    assert audit["status"] == "trusted"
    assert audit["final_score"] == 100
    assert "仅代表已审范围" in audit["reason"]


def test_remote_assessment_scores_observed_configuration_scope() -> None:
    phases = [
        SimpleNamespace(phase_key="recon", status="success"),
        SimpleNamespace(phase_key="detect", status="success"),
    ]
    findings = [
        SimpleNamespace(severity="低", status="config_candidate"),
        SimpleNamespace(severity="低", status="config_candidate"),
        SimpleNamespace(severity="中", status="refuted"),
    ]
    score = remote_pentest_service._remote_rule_score(phases, findings)
    assert score["available"] is True
    assert score["score"] == 90
    assert score["scope"] == "partial"


def test_remote_assessment_without_detection_is_not_scored() -> None:
    phases = [SimpleNamespace(phase_key="recon", status="failed")]
    score = remote_pentest_service._remote_rule_score(phases, [])
    assert score["available"] is False
    assert score["score"] is None
    assert score["reason"]
