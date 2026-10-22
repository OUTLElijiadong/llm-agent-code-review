"""Domain adapters for the shared CVSS weighted score policy."""

from types import SimpleNamespace

import pytest

from app.ai.scoring import score_risk_level
from app.services import pentest_service, sandbox_service

VECTOR_HIGH = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def test_pentest_composite_counts_only_confirmed_findings_and_caps_at_rule_score():
    findings = [
        SimpleNamespace(status="confirmed", cvss_vector=VECTOR_HIGH),
        SimpleNamespace(status="candidate", cvss_vector=VECTOR_HIGH),
        SimpleNamespace(status="inconclusive", cvss_vector=VECTOR_HIGH),
        SimpleNamespace(status="refuted", cvss_vector=VECTOR_HIGH),
    ]
    counts = {"严重": 0, "高": 1, "中": 0, "低": 0, "提示": 0}

    breakdown = pentest_service._pentest_composite_score_breakdown(counts, findings)

    assert breakdown["policy_version"] == "cvss-weighted-v1"
    assert breakdown["components"]["cvss"]["scored_findings"] == 1
    assert breakdown["components"]["cvss"]["unscored_findings"] == 0
    assert breakdown["score"] <= pentest_service._pentest_rule_score(counts)
    assert breakdown["risk_level"] == score_risk_level(breakdown["score"])
    assert breakdown["unconfirmed_findings"] == {"candidate": 1, "inconclusive": 1, "refuted": 1}


def test_pentest_missing_vectors_keep_domain_version_and_score_basis():
    counts = {"严重": 0, "高": 1, "中": 0, "低": 0, "提示": 0}
    breakdown = pentest_service._pentest_composite_score_breakdown(
        counts,
        [SimpleNamespace(status="confirmed", cvss_vector=None)],
    )
    coverage = {}
    pentest_service._mark_pentest_score_basis(coverage, counts, scope="complete")
    coverage["composite_scoring"] = breakdown

    assert breakdown["version"] == pentest_service.PENTEST_SCORE_VERSION
    assert breakdown["policy_version"] == "cvss-weighted-v1"
    assert breakdown["cvss_status"] == "unavailable"
    assert breakdown["components"]["cvss"]["weight"] == 0
    assert breakdown["score"] == 75
    assert coverage["score_basis"] == pentest_service.PENTEST_SCORE_VERSION
    assert coverage["composite_scoring"] == breakdown


@pytest.mark.parametrize("mode", ["whitebox", "blackbox", "combined"])
def test_sandbox_composite_uses_mode_findings_but_never_exceeds_execution_quality(mode):
    conclusion = {
        "test_mode": mode,
        "cvss_findings": [{"status": "confirmed", "cvss_vector": VECTOR_HIGH}],
    }
    original = {
        "score": 40,
        "score_available": True,
        "score_basis": sandbox_service.SANDBOX_SCORE_VERSION,
        "sample": {"executed_checks": 5, "earned_credit": 2},
    }

    breakdown = sandbox_service._sandbox_composite_score_breakdown(conclusion, original)

    assert breakdown["policy_version"] == "cvss-weighted-v1"
    assert breakdown["components"]["quality"]["score"] == 40
    assert breakdown["score"] <= 40
    assert breakdown["coverage_status"] == mode


def test_sandbox_missing_vectors_keep_quality_and_domain_version():
    original = {
        "score": 40,
        "score_available": True,
        "score_basis": sandbox_service.SANDBOX_SCORE_VERSION,
        "sample": {"executed_checks": 5, "earned_credit": 2},
    }

    breakdown = sandbox_service._sandbox_composite_score_breakdown(
        {"test_mode": "combined", "cvss_findings": [{"title": "unscored"}]}, original,
    )

    assert breakdown["version"] == sandbox_service.SANDBOX_SCORE_VERSION
    assert breakdown["policy_version"] == "cvss-weighted-v1"
    assert breakdown["cvss_status"] == "unavailable"
    assert breakdown["components"]["cvss"]["weight"] == 0
    assert breakdown["score"] == 40
    assert breakdown["score_source"] == sandbox_service.SANDBOX_SCORE_VERSION


def test_sandbox_export_score_section_uses_saved_composite_once_and_exposes_quality_cap():
    quality = {
        "score": 40,
        "score_available": True,
        "score_basis": sandbox_service.SANDBOX_SCORE_VERSION,
        "sample": {"executed_checks": 5, "earned_credit": 2},
    }
    breakdown = sandbox_service._sandbox_composite_score_breakdown(
        {"test_mode": "combined", "cvss_findings": [{"status": "confirmed", "cvss_vector": VECTOR_HIGH}]},
        quality,
    )

    lines = sandbox_service._sandbox_composite_score_lines(breakdown)

    assert lines.count("## 评分与结论") == 1
    assert sum(line.startswith("评分：") for line in lines) == 1
    assert f"评分：{breakdown['score']} / 100" in lines
    assert f"综合风险评级：{breakdown['risk_level']}" in lines
    assert f"执行质量分：40；依据：{sandbox_service.SANDBOX_SCORE_VERSION}。" in lines
    assert breakdown["score"] <= 40
