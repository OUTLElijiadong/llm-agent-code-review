"""多智能体 finding-aggregation-v1 确定性聚合契约。"""

from __future__ import annotations

from itertools import permutations

import app.ai.finding_aggregator as aggregator_module
from app.ai.finding_aggregator import (
    AGGREGATION_VERSION,
    aggregate_agent_findings,
    normalize_severity_with_score,
)

CODE = """import os

def run(user_input):
    return os.system(user_input)
"""


def _claim(
    source: str,
    *,
    severity: object = "高",
    confidence: object = 0.85,
    cwe: str = "CWE-78",
    evidence: str = "return os.system(user_input)",
) -> tuple[str, list[dict]]:
    return source, [{
        "title": "命令注入",
        "issue_type": "安全漏洞",
        "severity": severity,
        "line_start": 4,
        "line_end": 4,
        "description": "外部输入进入系统命令",
        "suggestion": "使用参数数组",
        "cwe": cwe,
        "evidence": evidence,
        "confidence": confidence,
        "source": f"llm:{source}",
    }]


def test_severity_aliases_have_one_versioned_mapping() -> None:
    assert normalize_severity_with_score("critical") == ("严重", 100.0)
    assert normalize_severity_with_score("P1") == ("高", 75.0)
    assert normalize_severity_with_score("warning") == ("中", 50.0)
    assert normalize_severity_with_score("info") == ("低", 25.0)
    assert normalize_severity_with_score(9.4) == ("严重", 100.0)


def test_numeric_percent_confidence_is_normalized_before_evidence_cap() -> None:
    source, claims = _claim("security", confidence=85)
    result = aggregate_agent_findings(
        {source: claims},
        {source: "安全审查代理"},
        code=CODE,
        file_name="runner.py",
        chunk_id="chunk-0",
    )

    claim = result.issues[0]["aggregation"]["claims"][0]
    assert claim["confidence"]["raw"] == 85
    assert claim["confidence"]["calibrated"] == 0.85


def test_conflicting_claims_never_create_an_unclaimed_severity_confidence_pair() -> None:
    inputs = dict([
        _claim("security", severity="严重", confidence=0.55),
        _claim("reliability", severity="低", confidence=0.95),
    ])

    result = aggregate_agent_findings(
        inputs,
        {"security": "安全审查代理", "reliability": "可靠性代理"},
        code=CODE,
        file_name="runner.py",
        chunk_id="chunk-0",
    )

    assert result.schema_version == AGGREGATION_VERSION
    assert len(result.issues) == 1
    issue = result.issues[0]
    asserted_pairs = {
        (claim["severity"]["normalized"], claim["confidence"]["calibrated"])
        for claim in issue["aggregation"]["claims"]
    }
    assert (issue["severity"], issue["confidence"]) in asserted_pairs
    assert issue["conflict_status"] == "unresolved"
    assert issue["human_review_status"] == "pending"
    assert "severity_disagreement" in issue["aggregation"]["conflicts"]


def test_confirmation_count_uses_unique_real_sources_not_model_declared_count() -> None:
    source, claims = _claim("security")
    claims[0]["confirmation_count"] = 99
    claims.append({**claims[0], "title": "同源重复描述"})

    result = aggregate_agent_findings(
        {source: claims},
        {source: "安全审查代理"},
        code=CODE,
        file_name="runner.py",
        chunk_id="chunk-0",
    )

    assert len(result.issues) == 1
    assert result.issues[0]["confirmation_count"] == 1
    assert {d["source"] for d in result.issues[0]["source_details"]} == {"llm:security"}


def test_every_claim_is_accounted_for_and_output_is_order_stable() -> None:
    pairs = [
        _claim("security", severity="高", confidence=0.82),
        _claim("reliability", severity="中", confidence=0.78),
        _claim("performance", severity="低", confidence=0.7, cwe="", evidence=""),
    ]
    outputs = []
    for order in permutations(pairs):
        result = aggregate_agent_findings(
            dict(order),
            {key: key for key, _ in pairs},
            code=CODE,
            file_name="runner.py",
            chunk_id="chunk-0",
        )
        coverage = result.coverage
        assert set(coverage["input_claim_ids"]) == (
            set(coverage["output_claim_ids"]) | set(coverage["discarded_claim_ids"])
        )
        assert not (set(coverage["output_claim_ids"]) & set(coverage["discarded_claim_ids"]))
        outputs.append(result.model_dump(mode="json"))

    assert all(item == outputs[0] for item in outputs[1:])


def test_bad_agent_item_is_isolated_while_valid_claim_continues() -> None:
    source, claims = _claim("security")
    claims.append("bad")

    result = aggregate_agent_findings(
        {source: claims},
        {source: "安全审查代理"},
        code=CODE,
        file_name="runner.py",
        chunk_id="chunk-0",
    )

    assert len(result.issues) == 1
    assert result.diagnostics[0]["code"] == "finding_not_object"
    assert result.coverage["invalid_input_count"] == 1


def test_internal_aggregation_error_preserves_claim_for_human_review(monkeypatch) -> None:
    source, claims = _claim("security")

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic aggregation failure")

    monkeypatch.setattr(aggregator_module, "aggregate_agent_findings", explode)
    result = aggregator_module.aggregate_agent_findings_safely(
        {source: claims},
        {source: "安全审查代理"},
        code=CODE,
        file_name="runner.py",
        chunk_id="chunk-0",
    )

    assert result.summary["fallback"] is True
    assert result.diagnostics[0]["code"] == "aggregation_internal_error"
    assert len(result.issues) == 1
    assert result.issues[0]["human_review_status"] == "pending"
    assert result.issues[0]["confirmation_count"] == 1
    assert result.coverage["input_claim_ids"] == result.coverage["output_claim_ids"]
