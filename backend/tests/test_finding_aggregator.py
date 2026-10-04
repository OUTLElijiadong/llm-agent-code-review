"""多智能体确定性聚合契约，以及历史版本的持久化兼容。"""

from __future__ import annotations

from itertools import permutations

import pytest

import app.ai.finding_aggregator as aggregator_module
from app.ai.finding_aggregator import (
    AGGREGATION_VERSION,
    ReviewFindingTooLarge,
    aggregate_agent_findings,
    aggregate_agent_findings_safely,
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


def test_risk_score_weights_each_real_source_only_once() -> None:
    _, security_claims = _claim("security", severity="严重", confidence=0.8)
    _, reliability_claims = _claim("reliability", severity="低", confidence=0.8)
    baseline = aggregate_agent_findings(
        {
            "security": security_claims,
            "reliability": reliability_claims,
        },
        {"security": "安全审查代理", "reliability": "可靠性代理"},
        code=CODE,
        file_name="runner.py",
        chunk_id="chunk-0",
    ).issues[0]
    duplicated = aggregate_agent_findings(
        {
            "security": [dict(security_claims[0]) for _ in range(10)],
            "reliability": reliability_claims,
        },
        {"security": "安全审查代理", "reliability": "可靠性代理"},
        code=CODE,
        file_name="runner.py",
        chunk_id="chunk-0",
    ).issues[0]

    assert baseline["confirmation_count"] == duplicated["confirmation_count"] == 2
    assert baseline["risk_score"] == 62.5
    assert duplicated["risk_score"] == baseline["risk_score"]


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


def test_long_claim_keeps_late_evidence_and_description() -> None:
    source, claims = _claim("security")
    tail = "末尾关键证据: os.system(user_input)"
    claims[0]["evidence"] = "前置代码\n" + "x" * 4500 + tail
    claims[0]["description"] = "背景\n" + "y" * 4500 + "末尾说明: 未校验输入"

    result = aggregate_agent_findings(
        {source: claims}, {source: "安全审查代理"},
        code=CODE, file_name="runner.py", chunk_id="chunk-0",
    )

    issue = result.issues[0]
    assert issue["evidence"].endswith(tail)
    assert issue["description"].endswith("末尾说明: 未校验输入")
    assert issue["source_details"][0]["evidence"].endswith(tail)
    assert issue["aggregation"]["claims"][0]["evidence"].endswith(tail)


def test_fallback_keeps_long_claim_tail(monkeypatch) -> None:
    source, claims = _claim("security")
    claims[0]["evidence"] = "x" * 4500 + "末尾证据"
    claims[0]["description"] = "y" * 4500 + "末尾描述"
    monkeypatch.setattr(
        aggregator_module, "aggregate_agent_findings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("聚合异常")),
    )

    result = aggregator_module.aggregate_agent_findings_safely(
        {source: claims}, {source: "安全审查代理"},
        code=CODE, file_name="runner.py", chunk_id="chunk-0",
    )

    issue = result.issues[0]
    assert issue["evidence"].endswith("末尾证据")
    assert issue["description"].endswith("末尾描述")
    assert issue["aggregation"]["claims"][0]["evidence"].endswith("末尾证据")


def test_all_narrative_claim_fields_keep_their_tail() -> None:
    source, claims = _claim("security")
    for field in ("suggestion", "fixed_code", "exploit_scenario", "remediation"):
        claims[0][field] = "前置内容" + "x" * 8500 + f"{field}:末尾"

    result = aggregate_agent_findings(
        {source: claims}, {source: "安全审查代理"},
        code=CODE, file_name="runner.py", chunk_id="chunk-0",
    )

    issue = result.issues[0]
    for field in ("suggestion", "fixed_code", "exploit_scenario", "remediation"):
        assert issue[field].endswith(f"{field}:末尾")
        assert issue["aggregation"]["claims"][0][field].endswith(f"{field}:末尾")


def test_fallback_keeps_all_narrative_field_tails(monkeypatch) -> None:
    source, claims = _claim("security")
    for field in ("suggestion", "fixed_code", "exploit_scenario", "remediation"):
        claims[0][field] = "x" * 8500 + f"{field}:末尾"
    monkeypatch.setattr(
        aggregator_module, "aggregate_agent_findings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("聚合异常")),
    )

    result = aggregate_agent_findings_safely(
        {source: claims}, {source: "安全审查代理"},
        code=CODE, file_name="runner.py", chunk_id="chunk-0",
    )

    issue = result.issues[0]
    for field in ("suggestion", "fixed_code", "exploit_scenario", "remediation"):
        assert issue[field].endswith(f"{field}:末尾")


def test_mysql_text_overflow_fails_review_instead_of_dropping_claim() -> None:
    source, claims = _claim("security")
    claims[0]["remediation"] = "中" * 21_846  # 65,538 UTF-8 bytes

    with pytest.raises(ReviewFindingTooLarge, match="remediation.*65535"):
        aggregate_agent_findings_safely(
            {source: claims}, {source: "安全审查代理"},
            code=CODE, file_name="runner.py", chunk_id="chunk-0",
        )


def test_mysql_text_byte_limit_not_character_limit() -> None:
    source, claims = _claim("security")
    claims[0]["suggestion"] = "中" * 21_845  # 65,535 UTF-8 bytes

    result = aggregate_agent_findings_safely(
        {source: claims}, {source: "安全审查代理"},
        code=CODE, file_name="runner.py", chunk_id="chunk-0",
    )

    assert result.issues[0]["suggestion"] == claims[0]["suggestion"]


@pytest.mark.parametrize("evidence,line", [
    ("nonexistent_line()", 2),
    ("", 2),
    ("", 0),
    ("   ", 0),
])
@pytest.mark.parametrize("issue_type", ["安全漏洞", "代码规范"])
def test_unmatched_or_absent_source_quote_requires_human_review(evidence, line, issue_type) -> None:
    """自报置信度不能把虚构、只有位置或空引用提升为已接受结论。"""
    result = aggregate_agent_findings(
        {"one": [{"title": "待核实的规则主张", "description": "缺少缺陷条件和影响证明。",
                  "issue_type": issue_type, "severity": "高", "confidence": 1,
                  "evidence": evidence, "line_number": line}]},
        {"one": "单一模型来源"}, code="def healthy():\n    return True\n",
        file_name="benign.py", chunk_id="quote-test",
    )

    issue = result.issues[0]
    assert issue["human_review_status"] == "pending"
    assert issue["aggregation"]["decision"] == "human_review"
    assert "source_quote_unverified" in issue["aggregation"]["review_reasons"]
    assert result.summary["pending_human_review_count"] == 1


@pytest.mark.parametrize("issue_type", ["安全漏洞", "security", "security_vulnerability", "vulnerability", " SECURITY "])
def test_matching_source_quote_does_not_verify_security_claim(issue_type) -> None:
    result = aggregate_agent_findings(
        {"one": [{"title": "未获验证的安全主张", "description": "原文存在不证明缺陷条件及影响。",
                  "issue_type": issue_type, "severity": "高", "confidence": 1,
                  "evidence": "return True", "line_number": 2,
                  "verification": "confirmed", "human_review_status": "accepted"}]},
        {"one": "单一模型来源"}, code="def healthy():\n    return True\n",
        file_name="benign.py", chunk_id="quote-test",
    )

    issue = result.issues[0]
    assert issue["evidence_quality"] == "verified"  # 仅引用匹配，非漏洞验证。
    assert issue["human_review_status"] == "pending"
    assert issue["aggregation"]["decision"] == "human_review"
    assert "security_claim_requires_verification" in issue["aggregation"]["review_reasons"]


@pytest.mark.parametrize("evidence", ['label = "alpha beta"', 'label = "Alpha  Beta"'])
def test_case_or_string_whitespace_changes_are_not_verified_quotes(evidence) -> None:
    result = aggregate_agent_findings(
        {"one": [{"title": "常量规则主张", "issue_type": "代码规范", "severity": "低",
                  "confidence": 1, "evidence": evidence}]},
        {"one": "单一模型来源"}, code='label = "Alpha Beta"\n',
        file_name="constants.py", chunk_id="quote-test",
    )

    assert result.issues[0]["evidence_quality"] != "verified"
    assert result.issues[0]["human_review_status"] == "pending"


def test_verified_non_security_quote_keeps_existing_positive_path() -> None:
    result = aggregate_agent_findings(
        {"one": [{"title": "命名规范主张", "issue_type": "命名规范", "severity": "低",
                  "confidence": 0.9, "evidence": 'label = "Alpha Beta"'}]},
        {"one": "单一模型来源"}, code='label = "Alpha Beta"\n',
        file_name="constants.py", chunk_id="quote-test",
    )

    assert result.issues[0]["evidence_quality"] == "verified"
    assert result.issues[0]["human_review_status"] == "not_required"
    assert result.issues[0]["aggregation"]["decision"] == "accepted"


def test_new_evidence_semantics_are_distinguishable_from_historical_v1() -> None:
    result = aggregate_agent_findings(
        {"one": [{"title": "命名规范主张", "issue_type": "命名规范", "severity": "低",
                  "confidence": 0.9, "evidence": 'label = "Alpha Beta"'}]},
        {}, code='label = "Alpha Beta"\n', file_name="constants.py", chunk_id="version-test",
    )

    assert result.schema_version == AGGREGATION_VERSION == "finding-aggregation-v2"
    assert result.issues[0]["aggregation_version"] == "finding-aggregation-v2"
    assert result.issues[0]["aggregation"]["schema_version"] == "finding-aggregation-v2"
    assert result.risk_scoring_version == "claim-risk-v2"  # 风险分算法版本保持原值。


@pytest.mark.parametrize("version", [None, "finding-aggregation-v1", "finding-aggregation-v2"])
def test_legacy_aggregation_version_is_not_rewritten_by_persistence_or_api(db, admin_user, version) -> None:
    from app.api.v1 import issues as issues_api
    from app.models.code_file import CodeFile
    from app.models.project import Project
    from app.models.review_issue import ReviewIssue
    from app.models.review_task import ReviewTask

    project = Project(user_id=admin_user.id, project_name="旧聚合版本", language="python", status="active")
    db.add(project)
    db.flush()
    task = ReviewTask(user_id=admin_user.id, project_id=project.id, task_name="旧聚合版本",
                      review_type="standard", status="success")
    code_file = CodeFile(project_id=project.id, file_name="benign.py", language="python", content="label = 'ok'")
    db.add_all([task, code_file])
    db.flush()
    metadata = {"schema_version": version, "decision": "accepted"} if version else None
    issue = ReviewIssue(task_id=task.id, file_id=code_file.id, issue_type="命名规范", severity="低",
                        description="保留历史人工复核元数据。", aggregation_version=version,
                        aggregation_json=metadata, human_review_status="not_required")
    db.add(issue)
    db.commit()

    response = issues_api.get_issue(issue.id, db=db, user=admin_user).data
    assert response.aggregation_version == version
    assert response.aggregation_json == metadata
    assert response.human_review_status == "not_required"
    assert not db.dirty and not db.deleted


def test_multiple_sources_do_not_replace_security_claim_verification() -> None:
    common = {"title": "同一规则主张", "severity": "高", "evidence": "return True", "line_number": 2}
    result = aggregate_agent_findings(
        {"quality": [{**common, "issue_type": "代码规范", "confidence": 0.98}],
         "security": [{**common, "issue_type": "安全漏洞", "confidence": 0.6}]},
        {"quality": "规范来源", "security": "安全来源"},
        code="def healthy():\n    return True\n", file_name="benign.py", chunk_id="quote-test",
    )

    issue = result.issues[0]
    assert issue["issue_type"] == "代码规范"  # 最佳引用仍来自原规范来源。
    assert issue["confirmation_count"] == 2  # 表示两个来源，非两次验证。
    assert issue["human_review_status"] == "pending"
    assert "security_claim_requires_verification" in issue["aggregation"]["review_reasons"]


@pytest.mark.parametrize("decision", ["accepted", "rejected", "evidence_requested"])
def test_pending_claim_survives_persistence_api_and_manual_adjudication(db, admin_user, decision) -> None:
    """真实聚合结果经既有Worker转换、SQLite、API序列化后不自动接受。"""
    from app.api.v1 import issues as issues_api
    from app.models.code_file import CodeFile
    from app.models.project import Project
    from app.models.review_task import ReviewTask
    from app.services import issue_service, review_service

    code = "def healthy():\n    return True\n"
    project = Project(user_id=admin_user.id, project_name="引用复核", language="python", status="active")
    db.add(project)
    db.flush()
    task = ReviewTask(user_id=admin_user.id, project_id=project.id, task_name="引用复核",
                      review_type="standard", status="success")
    code_file = CodeFile(project_id=project.id, file_name="benign.py", language="python", content=code)
    db.add_all([task, code_file])
    db.flush()
    item = aggregate_agent_findings(
        {"one": [{"title": "未获验证的安全主张", "issue_type": "安全漏洞", "severity": "高",
                  "description": "片段存在不证明主张成立。", "evidence": "return True", "confidence": 1}]},
        {"one": "单一模型来源"}, code=code, file_name=code_file.file_name, chunk_id="persistence-test",
    ).issues[0]
    finding = review_service._final_issue_to_finding(item)
    issue = review_service._finding_to_review_issue(task.id, code_file, finding)
    db.add(issue)
    db.commit()
    db.refresh(issue)

    response = issues_api.get_issue(issue.id, db=db, user=admin_user).data
    assert response.human_review_status == "pending" and response.status == "pending_review"
    assert response.evidence_quality == "verified"
    assert response.aggregation_json["review_reasons"] == ["security_claim_requires_verification"]
    original_claims = response.aggregation_json["claims"]

    updated = issue_service.review_decision(db, admin_user, issue.id, decision, "记录人工核对范围")
    after = issues_api.get_issue(updated.id, db=db, user=admin_user).data
    assert after.human_review_status == decision
    assert after.aggregation_json["claims"] == original_claims
    assert after.aggregation_json["human_review"]["reviewer_id"] == admin_user.id
    assert after.aggregation_json["human_review"]["note"] == "记录人工核对范围"


def test_noncanonical_unmatched_quote_also_requires_review() -> None:
    common = {"title": "同一规范主张", "issue_type": "代码规范", "severity": "低", "source_anchor": "same-anchor"}
    result = aggregate_agent_findings(
        {"matched": [{**common, "evidence": "return True", "confidence": 0.95}],
         "unmatched": [{**common, "evidence": "nonexistent_line()", "confidence": 0.8}]},
        {}, code="def healthy():\n    return True\n", file_name="benign.py", chunk_id="quote-test",
    )

    issue = result.issues[0]
    assert issue["evidence_quality"] == "verified"
    assert issue["human_review_status"] == "pending"
    assert "source_quote_unverified" in issue["aggregation"]["review_reasons"]
