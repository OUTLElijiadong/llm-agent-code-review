"""CVSS 主权重平台分必须可复算，且不隐藏未评分问题。"""
from types import SimpleNamespace

import pytest

from app.ai.scoring import compute_composite_score_breakdown

VECTOR = 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'


def test_cvss_is_seventy_percent_and_recalculated_from_vector():
    result = compute_composite_score_breakdown({'高': 1}, [
        {'severity': '高', 'issue_type': '安全漏洞', 'cvss_vector': VECTOR, 'cvss_score': 1.0},
    ])
    assert result['version'] == 'cvss-weighted-v1'
    assert result['components']['cvss']['max_base_score'] == 9.8
    assert result['components']['cvss']['score'] == pytest.approx(2)
    assert result['components']['cvss']['weight'] == .7
    assert result['components']['quality']['weight'] == .3
    assert result['components']['quality']['score'] == 92
    assert result['score'] == 29  # 0.7 * 2 + 0.3 * 92


def test_invalid_vector_or_model_number_never_becomes_cvss():
    for value in ['', 'CVSS:3.1/AV:N', None]:
        result = compute_composite_score_breakdown({'高': 1}, [{'cvss_score': 9.8, 'cvss_vector': value}])
        assert result['cvss_status'] == 'unavailable'
        assert result['version'] == 'severity-deduction-v1'
        assert result['score'] == 92
        assert result['components']['cvss']['weight'] == 0


def test_missing_vector_cannot_raise_existing_score():
    findings = [{'cvss_vector': 'AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N'}]
    findings += [{'issue_type': '安全漏洞'} for _ in range(8)]
    result = compute_composite_score_breakdown({'严重': 5, '高': 4}, findings)
    assert result['cvss_status'] == 'partial'
    assert result['components']['cvss']['unscored_findings'] == 8
    assert result['score'] == 0
    assert result['coverage_cap_applied'] is True


def test_refuted_findings_are_excluded_and_orm_rows_are_supported():
    rows = [SimpleNamespace(cvss_vector=VECTOR, status='refuted'),
            SimpleNamespace(cvss_vector='AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N', status='confirmed')]
    result = compute_composite_score_breakdown({'低': 1}, rows)
    assert result['components']['cvss']['max_base_score'] == 0
    assert result['components']['cvss']['scored_findings'] == 1
    assert result['score'] == 100


def test_sandbox_can_supply_measured_quality_score_without_inflating_failed_checks():
    result = compute_composite_score_breakdown({'高': 1}, [{'cvss_vector': VECTOR}], quality_score=20)
    assert result['score'] == 7
    fallback = compute_composite_score_breakdown({}, [], quality_score=20)
    assert fallback['score'] == 20
    assert fallback['cvss_status'] == 'not_applicable'


def test_low_findings_never_dilute_highest_cvss():
    worst = {'cvss_vector': VECTOR}
    one = compute_composite_score_breakdown({'高': 1}, [worst])
    many = compute_composite_score_breakdown({'高': 1, '低': 10}, [worst] + [worst] * 10)
    assert many['score'] <= one['score']


@pytest.mark.parametrize('score', [float('nan'), float('inf'), -1, 101, True])
def test_invalid_quality_score_is_rejected(score):
    with pytest.raises(ValueError):
        compute_composite_score_breakdown({}, [], quality_score=score)


def test_new_report_exports_use_cvss_while_history_keeps_its_recorded_method():
    from app.services.report_exporter import build_report_score_facts
    score, breakdown = build_report_score_facts(
        {'高': 1}, 1, findings=[{'cvss_vector': VECTOR}], scoring_version='cvss-weighted-v1',
    )
    assert score == 29
    assert breakdown['components']['cvss']['weight'] == .7
    old_score, old_breakdown = build_report_score_facts({'高': 1}, 1)
    assert old_score == 92
    assert old_breakdown['version'] == 'severity-deduction-v1'


def test_independent_arithmetic_audit_does_not_overwrite_cvss_with_severity_score():
    from app.services.score_credibility import audit_report_score
    task = SimpleNamespace(review_type='standard', score=29, score_version='cvss-weighted-v1', coverage={})
    result = audit_report_score(task, 29, {'高': 1}, 1, findings=[{'cvss_vector': VECTOR}])
    assert result['final_score'] == 29
    assert result['status'] == 'trusted'
    assert audit_report_score(task, 29, {'高': 1}, 1)['status'] == 'unavailable'


def test_report_list_and_detail_recompute_same_cvss_method():
    from app.services.report_service import _build_task_score_facts
    task = SimpleNamespace(review_type='standard', score=100, score_version='cvss-weighted-v1')
    stats = {'severity': {'高': 1}, 'total_issues': 1, 'cvss_findings': [{'cvss_vector': VECTOR}]}
    _, score, breakdown, _ = _build_task_score_facts(task, stats)
    assert score == 29
    assert breakdown['version'] == 'cvss-weighted-v1'


@pytest.mark.parametrize("status", ["partial", "failed", "cancelled"])
def test_partial_new_policy_display_keeps_cvss_score(status):
    from app.services.report_service import display_report_score

    task = SimpleNamespace(
        review_type="standard", status=status, score=29,
        score_version="cvss-weighted-v1", coverage={"completed_files": 1},
        total_files=2, processed_files=1, total_issues=1, high_issues=1,
    )
    assert display_report_score(task, 29) == 29
    assert display_report_score(task, None) is None


def test_new_policy_does_not_invent_score_without_scope_or_vector_inputs():
    from app.services.report_service import _build_task_score_facts, display_report_score

    task = SimpleNamespace(
        review_type="standard", status="failed", score=29,
        score_version="cvss-weighted-v1", coverage={},
        total_files=1, processed_files=0, total_issues=0,
    )
    assert display_report_score(task, 29) is None
    task.coverage = {"completed_files": 1}
    stats = {"severity": {"高": 1}, "total_issues": 1}
    _, score, breakdown, _ = _build_task_score_facts(task, stats)
    assert score is None
    assert breakdown["score_source"] == "unavailable"


def test_new_policy_still_refuses_ungraded_review_findings():
    from app.services.report_service import _build_task_score_facts

    task = SimpleNamespace(review_type="standard", score_version="cvss-weighted-v1")
    stats = {"severity": {}, "total_issues": 1, "cvss_findings": [{"cvss_vector": VECTOR}]}
    _, score, breakdown, _ = _build_task_score_facts(task, stats)
    assert score is None
    assert breakdown["score_unavailable_reason"] == "ungraded_findings"


def test_composite_score_uses_documented_half_up_rounding():
    result = compute_composite_score_breakdown({'高': 1}, [{'cvss_vector': VECTOR}], quality_score=17)
    assert result['raw_score'] == 6.5
    assert result['score'] == 7


def test_cvss_pentest_audit_requires_independent_confirmed_counts():
    from app.services.score_credibility import audit_report_score

    task = SimpleNamespace(
        review_type='pentest', score=88, score_version='pentest-confirmed-deduction-v1',
        score_breakdown={'policy_version': 'cvss-weighted-v1'}, coverage={},
    )
    result = audit_report_score(task, 88, {'高': 1}, 1, findings=[{'cvss_vector': None}])
    assert result['status'] == 'unavailable'
    assert result['final_score'] is None


def test_cvss_sandbox_refuses_ungraded_findings():
    from app.services.report_service import _build_task_score_facts

    task = SimpleNamespace(
        review_type='sandbox_test', score_version='cvss-weighted-v1',
        coverage={'score_sample': {'checks': [{'state': 'passed'}]}},
    )
    stats = {'severity': {'高': 1, '未分级': 1}, 'total_issues': 2,
             'cvss_findings': [{'cvss_vector': VECTOR}]}
    _, score, breakdown, _ = _build_task_score_facts(task, stats)
    assert score is None
    assert breakdown['score_unavailable_reason'] == 'ungraded_findings'


def test_cvss_pentest_refuses_ungraded_confirmed_but_not_unconfirmed_findings():
    from app.services.report_service import _build_task_score_facts

    task = SimpleNamespace(review_type='pentest', score_version='cvss-weighted-v1',
                           coverage={'confirmed_counts': {'高': 1}})
    stats = {'severity': {'高': 1, '未分级': 1}, 'total_issues': 2, 'confirmed': 1,
             'confirmed_severity': {'高': 1}, 'cvss_findings': [{'cvss_vector': VECTOR}]}
    _, score, breakdown, _ = _build_task_score_facts(task, stats)
    assert score == 24
    assert breakdown['counts'] == {'严重': 0, '高': 1, '中': 0, '低': 0}
    task.coverage = {'confirmed_counts': {'高': 1, '未分级': 1}}
    stats.update(confirmed=2, confirmed_severity={'高': 1, '未分级': 1})
    _, score, breakdown, _ = _build_task_score_facts(task, stats)
    assert score is None
    assert breakdown['score_unavailable_reason'] == 'ungraded_findings'


def test_cvss_pentest_rejects_mismatched_confirmed_counts():
    from app.services.report_service import _build_task_score_facts

    task = SimpleNamespace(review_type='pentest', score_version='cvss-weighted-v1',
                           coverage={'confirmed_counts': {'高': 1}})
    stats = {'severity': {'高': 2}, 'total_issues': 2, 'confirmed': 2,
             'confirmed_severity': {'高': 2}, 'cvss_findings': [{'cvss_vector': VECTOR}] * 2}
    _, score, breakdown, _ = _build_task_score_facts(task, stats)
    assert score is None
    assert breakdown['score_unavailable_reason'] == 'confirmed_counts_mismatch'
