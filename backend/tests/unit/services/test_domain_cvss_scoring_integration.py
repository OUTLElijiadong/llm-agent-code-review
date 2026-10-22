"""Domain adapters for the shared CVSS weighted score policy."""

from types import SimpleNamespace

import pytest

from app.ai.scoring import score_risk_level
from app.services import pentest_service, sandbox_service
from app.services.domain_report_exporter import build_pentest_report_markdown, score_section_lines

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
    lines = score_section_lines(breakdown["score"], available=True, score_breakdown=breakdown)
    assert f"CVSS 向量：无有效向量" in lines


def test_sandbox_scored_breakdown_without_vector_list_does_not_claim_no_vector():
    quality = {
        "score": 40,
        "score_available": True,
        "score_basis": sandbox_service.SANDBOX_SCORE_VERSION,
        "sample": {"executed_checks": 5, "earned_credit": 2},
    }
    breakdown = sandbox_service._sandbox_composite_score_breakdown(
        {
            "test_mode": "combined",
            "cvss_findings": [{"title": "confirmed", "status": "confirmed", "cvss_vector": VECTOR_HIGH}],
        },
        quality,
    )

    lines = score_section_lines(breakdown["score"], available=True, score_breakdown=breakdown)

    assert breakdown["components"]["cvss"]["scored_findings"] == 1
    assert breakdown["components"]["cvss"]["max_base_score"] == 9.8
    assert breakdown["components"]["cvss"]["weight"] == 0.7
    assert breakdown["score"] == 13
    assert "CVSS 向量：已评分发现存在，但向量明细未随评分分项保存" in lines
    assert "CVSS 向量：无有效向量" not in lines
    assert VECTOR_HIGH not in lines


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


def test_cvss_score_section_exports_weight_vectors_and_ungraded_count():
    breakdown = {
        "policy_version": "cvss-weighted-v1",
        "score_label": "平台综合分（非 CVSS 官方综合分）",
        "cvss_standard": "CVSS 3.1（FIRST）",
        "cvss_vectors": [VECTOR_HIGH],
        "cvss_reason": "部分发现缺少有效向量，综合分受原规则分上限约束",
        "components": {
            "cvss": {
                "weight": 0.7, "max_base_score": 9.8,
                "scored_findings": 1, "unscored_findings": 1,
            },
            "quality": {"weight": 0.3},
        },
    }
    lines = score_section_lines(42, available=True, score_breakdown=breakdown)

    assert "CVSS 权重：70%" in lines
    assert f"CVSS 向量：{VECTOR_HIGH}" in lines
    assert "CVSS 最高基础分：9.8 / 10" in lines
    assert "原规则质量分权重：30%" in lines
    assert "CVSS 已评分发现：1 项" in lines
    assert "CVSS 未评分发现：1 项" in lines


def test_pentest_export_recalculates_cvss_score_from_vector():
    markdown = build_pentest_report_markdown(
        {
            "scope": {"remote_target_url": "https://example.test"},
            "findings": [{
                "title": "访问控制缺失", "severity": "高", "status": "confirmed",
                "cvss_score": 8.1, "cvss_vector": VECTOR_HIGH,
            }],
        },
        task_name="授权测试", project_name="项目", execution_status="completed",
        error_message=None, coverage={"coverage_status": "complete"},
    )

    assert "CVSS 基础分：9.8 / 10" in markdown
    assert f"CVSS 向量：{VECTOR_HIGH}" in markdown
    assert "CVSS 基础分：8.1 / 10" not in markdown


@pytest.mark.parametrize("vector", [None, "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:INVALID"])
def test_pentest_export_does_not_trust_score_without_valid_vector(vector):
    markdown = build_pentest_report_markdown(
        {
            "scope": {"remote_target_url": "https://example.test"},
            "findings": [{
                "title": "访问控制缺失", "severity": "高", "status": "confirmed",
                "cvss_score": 8.1, "cvss_vector": vector,
            }],
        },
        task_name="授权测试", project_name="项目", execution_status="completed",
        error_message=None, coverage={"coverage_status": "complete"},
    )

    assert "CVSS 基础分：未评分" in markdown
    assert "CVSS 向量：" not in markdown
    assert "CVSS 基础分：8.1 / 10" not in markdown
