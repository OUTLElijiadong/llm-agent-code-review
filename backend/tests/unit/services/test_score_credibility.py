"""小菱分数可信度终审：一致即可信，不一致打回按规则重判，无依据不出分。"""

from types import SimpleNamespace

from app.services.sandbox_service import SANDBOX_SCORE_VERSION
from app.services.score_credibility import audit_report_score


def _sandbox_task(score, checks):
    return SimpleNamespace(
        review_type="sandbox_test",
        score=score,
        coverage={
            "score_available": True,
            "score_basis": SANDBOX_SCORE_VERSION,
            "score_sample": {"executed_checks": len(checks), "checks": checks},
        },
    )


CHECKS = [
    {"check": "deterministic_runner", "state": "passed"},
    {"check": "ai_dynamic_case_1", "state": "failed"},
    {"check": "blackbox_entry", "state": "partial"},
]


def test_matching_sandbox_score_is_trusted() -> None:
    audit = audit_report_score(_sandbox_task(50, CHECKS), 50, {}, 0)
    assert audit["status"] == "trusted"
    assert audit["final_score"] == 50


def test_mismatched_sandbox_score_is_rescored_by_rule() -> None:
    audit = audit_report_score(_sandbox_task(90, CHECKS), 90, {}, 0)
    assert audit["status"] == "rescored"
    assert audit["final_score"] == 50
    assert audit["rejected_score"] == 90


def test_code_review_stale_stored_score_is_rescored_from_issues() -> None:
    task = SimpleNamespace(review_type="code_review", score=100, coverage={})
    severity = {"严重": 1, "高": 1, "中": 0, "低": 2}
    audit = audit_report_score(task, 75, severity, 4)
    assert audit["status"] == "rescored"
    assert audit["final_score"] == 75
    assert audit["rejected_score"] == 100


def test_missing_rule_inputs_never_fall_back_to_zero() -> None:
    task = _sandbox_task(80, [])
    audit = audit_report_score(task, 80, {}, 0)
    assert audit["status"] == "unavailable"
    assert audit["final_score"] is None
    unavailable = audit_report_score(SimpleNamespace(review_type="code_review", score=0, coverage={}), None, {}, 0)
    assert unavailable["status"] == "unavailable"
    assert unavailable["final_score"] is None


def test_score_recalculation_does_not_claim_independent_finding_review() -> None:
    task = SimpleNamespace(review_type="discussion", score=29, coverage={})
    audit = audit_report_score(task, 29, {"高": 6, "中": 6, "低": 5}, 17)
    assert audit["status"] == "trusted"
    assert audit["reviewer"] == "规则复算"
    assert audit["verification_scope"] == "score_only"
    assert audit["independent_review"] is False
