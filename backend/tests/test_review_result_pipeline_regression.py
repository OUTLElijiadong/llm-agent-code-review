"""审查结果质量回归：N-way 去重、命令注入、CVSS 与评分追溯。"""

from __future__ import annotations

import json
from dataclasses import asdict
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.base import AgentResult
from app.agents.review_agent import _issue_to_finding
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.ai.cvss import normalize_cvss
from app.ai.discussion_orchestrator import _normalize_discussion_issues
from app.ai.result_parser import Issue, normalize_severity, parse
from app.ai.scoring import SCORING_VERSION, compute_score, compute_score_breakdown, score_risk_level
from app.ai.static_analyzer import Finding, scan
from app.services.issue_merger import merge_findings_and_issues
from app.services.review_service import _final_issue_to_finding, _issue_to_review_issue

FIXTURES = Path(__file__).parent / "fixtures" / "vuln_samples"
VECTOR_CRITICAL = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def _finding(
    *,
    line: int,
    evidence: str,
    source: str = "static",
    title: str = "SQL 注入",
    cwe: str = "CWE-89",
    confidence: float = 0.96,
) -> Finding:
    return Finding(
        line_number=line,
        end_line=line,
        issue_type="安全漏洞",
        severity="严重",
        title=title,
        description=f"{title} 的确定性证据",
        suggestion="使用安全 API",
        cwe=cwe,
        evidence=evidence,
        confidence=confidence,
        source=source,
        cvss_vector=VECTOR_CRITICAL,
        static_rule_hits=1,
    )


def _issue(
    *,
    line: int,
    evidence: str,
    source: str,
    title: str = "SQL 注入漏洞",
    cwe: str = "CWE-89",
    confidence: float = 0.85,
) -> Issue:
    return Issue(
        line_number=line,
        end_line=line,
        issue_type="安全漏洞",
        severity="严重",
        title=title,
        description=f"{title} 可被外部输入利用",
        suggestion="使用参数化调用",
        cwe=cwe,
        evidence=evidence,
        confidence=confidence,
        source=source,
        cvss_vector=VECTOR_CRITICAL,
    )


def test_n_way_merge_preserves_every_confirmation() -> None:
    evidence = 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")'
    merged = merge_findings_and_issues(
        [_finding(line=20, evidence=evidence)],
        [
            _issue(line=20, evidence=evidence, source="llm:security"),
            _issue(line=21, evidence=evidence, source="llm:reliability"),
        ],
        file_id=7,
    )

    assert len(merged) == 1
    issue = merged[0]
    assert issue.source == "hybrid"
    assert issue.confirmation_count == 3
    assert {item["source"] for item in issue.source_details} == {
        "static",
        "llm:security",
        "llm:reliability",
    }
    assert {item["evidence"] for item in issue.source_details} == {evidence}
    assert {item["confidence"] for item in issue.source_details} == {0.96, 0.85}
    assert issue.static_rule_hits == 1
    assert issue.finding_fingerprint


def test_n_way_merge_deduplicates_multiple_llm_results_without_static() -> None:
    evidence = "dangerous(user_input)"
    merged = merge_findings_and_issues(
        [],
        [
            _issue(line=8, evidence=evidence, source="llm:general"),
            _issue(line=8, evidence=evidence, source="llm:security"),
            _issue(line=9, evidence=evidence, source="llm:reviewer"),
        ],
        file_id=8,
    )

    assert len(merged) == 1
    assert merged[0].source == "llm"
    assert merged[0].confirmation_count == 3


def test_n_way_merge_is_stable_for_every_input_order() -> None:
    evidence = "pickle.loads(session_data)"
    static_findings = [
        _finding(
            line=12,
            evidence=evidence,
            title="不安全反序列化",
            cwe="CWE-502",
        ),
    ]
    llm_issues = [
        _issue(
            line=12,
            evidence=evidence,
            source="llm:security",
            title="pickle 可执行任意代码",
            cwe="CWE-502",
        ),
        _issue(
            line=12,
            evidence=evidence,
            source="llm:general",
            title="不可信数据反序列化",
            cwe="CWE-502",
        ),
        _issue(
            line=12,
            evidence=evidence,
            source="llm:reviewer",
            title="CWE-502 代码执行风险",
            cwe="CWE-502",
        ),
    ]

    outputs = [
        [asdict(item) for item in merge_findings_and_issues(static_findings, list(order), file_id=21)]
        for order in permutations(llm_issues)
    ]

    assert all(output == outputs[0] for output in outputs[1:])


def test_n_way_merge_preserves_declared_confirmation_count() -> None:
    issue = _issue(
        line=12,
        evidence="pickle.loads(payload)",
        source="llm:consensus",
        title="不安全反序列化",
        cwe="CWE-502",
    )
    issue.confirmation_count = 4
    issue.source_details = [{
        "source": "llm:consensus",
        "confidence": 0.85,
        "evidence": issue.evidence,
        "line_number": 12,
        "title": issue.title,
    }]

    merged = merge_findings_and_issues([], [issue], file_id=24)

    assert len(merged) == 1
    assert merged[0].confirmation_count == 4


def test_real_pickle_fixture_clusters_same_sink_with_multiple_phrasings() -> None:
    path = FIXTURES / "deserialization_python.py"
    static_findings = [
        item
        for item in scan(content=path.read_text(encoding="utf-8"), file_name=path.name)
        if item.cwe == "CWE-502" and item.line_number == 12
    ]
    assert len(static_findings) == 1
    evidence = static_findings[0].evidence
    llm_issues = [
        _issue(
            line=12,
            evidence=evidence,
            source="llm:security",
            title="不安全的 pickle 反序列化",
            cwe="CWE-502",
        ),
        _issue(
            line=12,
            evidence=evidence,
            source="llm:general",
            title="攻击者可通过 loads 执行代码",
            cwe="CWE-502",
        ),
    ]

    merged = merge_findings_and_issues(static_findings, llm_issues, file_id=22)

    assert len(merged) == 1
    assert merged[0].source == "hybrid"
    assert merged[0].confirmation_count == 3
    assert {detail["source"] for detail in merged[0].source_details} == {
        "static",
        "llm:security",
        "llm:general",
    }


def test_adjacent_independent_sinks_are_not_merged() -> None:
    merged = merge_findings_and_issues(
        [
            _finding(line=30, evidence="os.system(first_command)", cwe="CWE-78", title="命令注入"),
            _finding(line=31, evidence="os.system(second_command)", cwe="CWE-78", title="命令注入"),
        ],
        [],
        file_id=9,
    )

    assert len(merged) == 2
    assert {item.line_number for item in merged} == {30, 31}


def test_empty_evidence_line_drift_clusters_unique_pickle_sink() -> None:
    """模型只给出漂移行号且没有证据时，唯一代码接收点仍应归为一条。"""
    code = "import pickle\n\ndef load(data):\n    return pickle.loads(data)\n"
    issues = [
        _issue(
            line=line,
            evidence="",
            source=f"llm:agent-{index}",
            title=f"不安全反序列化发现 {index}",
            cwe="CWE-502",
        )
        for index, line in enumerate((2, 3, 4, 5, 6), 1)
    ]

    merged = merge_findings_and_issues([], issues, file_id=101, code=code)

    assert len(merged) == 1
    assert merged[0].confirmation_count == 5
    assert merged[0].line_number == 4
    assert "pickle.loads(data)" in merged[0].evidence


def test_same_line_independent_code_evidence_stays_separate() -> None:
    """同一行同 CWE 但指向两个不同 sink 时不能因标题相似而吞并。"""
    merged = merge_findings_and_issues(
        [],
        [
            _issue(
                line=10,
                evidence="os.system(first_command)",
                source="llm:first",
                title="命令注入",
                cwe="CWE-78",
            ),
            _issue(
                line=10,
                evidence="os.system(second_command)",
                source="llm:second",
                title="命令注入",
                cwe="CWE-78",
            ),
        ],
        file_id=102,
        code="import os\nos.system(first_command); os.system(second_command)\n",
    )

    assert len(merged) == 2
    assert {item.evidence for item in merged} == {
        "os.system(first_command)",
        "os.system(second_command)",
    }


def test_ast_keeps_two_same_line_command_sinks() -> None:
    """AST 不能再用行号作为唯一键，否则同一行第二个调用会丢失。"""
    code = "import os\n\ndef run(a, b):\n    return (os.system(a), os.system(b))\n"
    findings = [item for item in scan(content=code, file_name="same_line.py") if item.cwe == "CWE-78"]

    assert len(findings) == 2
    assert {item.source_anchor for item in findings}.__len__() == 2
    assert {item.column_start for item in findings} == {12, 26}


def test_source_anchor_resolves_python_import_aliases() -> None:
    code = (
        "import subprocess as sp\n"
        "from pickle import loads as decode\n\n"
        "def run(command, payload):\n"
        "    return sp.check_output(command, shell=True), decode(payload)\n"
    )
    issues = [
        _issue(
            line=3,
            evidence="",
            source="llm:command",
            title="命令注入",
            cwe="CWE-78",
        ),
        _issue(
            line=6,
            evidence="",
            source="llm:serialization",
            title="不安全反序列化",
            cwe="CWE-502",
        ),
    ]

    merged = merge_findings_and_issues([], issues, file_id=103, code=code)

    assert len(merged) == 2
    assert {item.line_number for item in merged} == {5}
    assert all(item.source_anchor.startswith("py:") for item in merged)
    assert len({item.source_anchor for item in merged}) == 2


def test_javascript_unique_evidence_resolves_drifted_lines_without_python_ast() -> None:
    """非 Python 源码使用完整源码证据定位，且同一行两个 sink 不得误合并。"""
    code = (
        'const childProcess = require("node:child_process")\n'
        'childProcess.exec(firstCommand); childProcess.exec(secondCommand)\n'
    )
    issues = [
        _issue(
            line=8,
            evidence="childProcess.exec(firstCommand)",
            source="llm:first",
            title="命令注入",
            cwe="CWE-78",
        ),
        _issue(
            line=9,
            evidence="childProcess.exec(secondCommand)",
            source="llm:second",
            title="命令注入",
            cwe="CWE-78",
        ),
    ]

    merged = merge_findings_and_issues(
        [],
        issues,
        file_id=104,
        code=code,
        language="javascript",
    )

    assert len(merged) == 2
    assert {item.line_number for item in merged} == {2}
    assert len({item.source_anchor for item in merged}) == 2
    assert all(item.source_anchor.startswith("src:") for item in merged)


def test_php_multiline_unique_evidence_sets_exact_start_and_end_lines() -> None:
    code = (
        "<?php\n"
        "$result = shell_exec(\n"
        "    $userCommand\n"
        ");\n"
    )
    issue = _issue(
        line=20,
        evidence="shell_exec($userCommand)",
        source="llm:security",
        title="命令注入",
        cwe="CWE-78",
    )

    merged = merge_findings_and_issues(
        [],
        [issue],
        file_id=105,
        code=code,
        language="php",
    )

    assert len(merged) == 1
    assert merged[0].line_number == 2
    assert merged[0].end_line == 4
    assert merged[0].source_anchor.startswith("src:")


def test_repeated_source_evidence_uses_unique_nearest_occurrence() -> None:
    """重复证据只有在模型位置能唯一消歧时才生成精确锚点。"""
    code = "exec(command)\nconst safe = true\nexec(command)\n"
    issues = [
        _issue(
            line=1,
            evidence="exec(command)",
            source="llm:first",
            title="命令注入",
            cwe="CWE-78",
        ),
        _issue(
            line=3,
            evidence="exec(command)",
            source="llm:second",
            title="命令注入",
            cwe="CWE-78",
        ),
    ]

    merged = merge_findings_and_issues(
        [],
        issues,
        file_id=106,
        code=code,
        language="javascript",
    )

    assert len(merged) == 2
    assert {item.line_number for item in merged} == {1, 3}
    assert len({item.source_anchor for item in merged}) == 2


def test_cvss_requires_a_valid_vector_for_any_score() -> None:
    assert normalize_cvss(0.0, None) == (None, None, None, "unavailable")
    assert normalize_cvss(7.5, "not-a-vector") == (None, None, None, "unavailable")
    assert normalize_cvss(7.5, None) == (None, None, None, "unavailable")


def test_review_agent_conversion_preserves_cvss_and_provenance() -> None:
    issue = _issue(
        line=7,
        evidence="os.system(user_command)",
        source="llm:security",
        cwe="CWE-78",
    )
    issue.cvss_score = 1.0
    issue.cvss_vector = VECTOR_CRITICAL
    issue.source_details = [{
        "source": "llm:security",
        "confidence": 0.85,
        "evidence": issue.evidence,
        "line_number": 7,
        "title": issue.title,
    }]

    finding = _issue_to_finding(issue)

    assert finding.cvss_score == pytest.approx(9.8)
    assert finding.cvss_vector == VECTOR_CRITICAL
    assert finding.cvss_version == "3.1"
    assert finding.cvss_source == "vector"
    assert finding.source_details == issue.source_details


def test_python_command_injection_fixture_detects_sinks_and_safe_boundary() -> None:
    path = FIXTURES / "command_injection_python.py"
    findings = scan(content=path.read_text(encoding="utf-8"), file_name=path.name)
    command_findings = [item for item in findings if item.cwe == "CWE-78"]

    assert {item.line_number for item in command_findings} == {9, 15, 21, 27}
    assert all(item.cvss_score == pytest.approx(9.8) for item in command_findings)
    assert all(item.cvss_vector == VECTOR_CRITICAL for item in command_findings)
    assert not any("safe_ping" in item.evidence for item in command_findings)
    assert not any("uptime" in item.evidence for item in command_findings)


@pytest.mark.parametrize("line_offset", [200, 400])
def test_security_sentinel_preserves_second_and_third_chunk_offsets(
    monkeypatch: pytest.MonkeyPatch,
    line_offset: int,
) -> None:
    agent = SecuritySentinelAgent()
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={
                "findings": [
                    {
                        "title": "命令注入",
                        "category": "安全漏洞",
                        "owasp": "A03:2021-Injection",
                        "cwe": "CWE-78",
                        "severity": "严重",
                        "line_start": 3,
                        "line_end": 3,
                        "evidence": "os.system(user_command)",
                        "exploit_scenario": "外部输入可执行任意命令",
                        "fix_suggestion": "改用参数列表",
                        "references": [],
                        "confidence": 0.95,
                    },
                ],
            },
        ),
    )

    result = agent.scan_file_for_review(
        code="def run(user_command):\n    validate(user_command)\n    os.system(user_command)\n",
        language="python",
        file_name="worker.py",
        line_offset=line_offset,
    )

    assert result.success is True
    assert result.data["issues"][0].line_number == line_offset + 3
    assert result.data["issues"][0].end_line == line_offset + 3


def test_cvss_vector_is_authoritative_and_missing_score_stays_missing() -> None:
    parsed = parse(json.dumps({
        "issues": [
            {
                "issue_type": "安全漏洞",
                "severity": "严重",
                "title": "命令注入",
                "description": "外部输入进入 shell",
                "cwe": "CWE-78",
                "cvss_score": 1.0,
                "cvss_vector": VECTOR_CRITICAL,
            },
            {
                "issue_type": "安全漏洞",
                "severity": "高",
                "title": "缺少 CVSS 证据",
                "description": "模型没有提供向量或分数",
            },
        ],
    }, ensure_ascii=False))

    assert parsed.issues[0].cvss_score == pytest.approx(9.8)
    assert parsed.issues[0].cvss_version == "3.1"
    assert parsed.issues[0].cvss_source == "vector"
    assert parsed.issues[1].cvss_score is None
    assert parsed.issues[1].cvss_vector is None
    assert parsed.issues[1].cvss_source == "unavailable"


def test_collaborative_conversion_keeps_cvss_pair() -> None:
    finding = _final_issue_to_finding({
        "line_number": 12,
        "issue_type": "安全漏洞",
        "severity": "严重",
        "title": "命令注入",
        "description": "外部输入进入 shell",
        "cwe": "CWE-78",
        "evidence": "os.system(user_cmd)",
        "cvss_score": 1.0,
        "cvss_vector": VECTOR_CRITICAL,
    })

    assert finding.cvss_score == pytest.approx(9.8)
    assert finding.cvss_vector == VECTOR_CRITICAL
    assert finding.cvss_version == "3.1"
    assert finding.cvss_source == "vector"


def test_collaborative_conversion_normalizes_severity_and_expands_sources() -> None:
    finding = _final_issue_to_finding({
        "line_number": 12,
        "issue_type": "安全漏洞",
        "severity": "critical",
        "title": "命令注入",
        "description": "外部输入进入 shell",
        "cwe": "CWE-78",
        "evidence": "os.system(user_cmd)",
        "cross_agent_names": ["security_reviewer"],
        "cross_agent_count": 3,
    })

    assert finding.severity == "严重"
    assert finding.confirmation_count == 3
    assert len(finding.source_details) == 3
    assert {detail["source"] for detail in finding.source_details} == {
        "security_reviewer",
        "llm_collab:2",
        "llm_collab:3",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("critical", "严重"),
        ("危急", "严重"),
        ("HIGH", "高"),
        ("medium", "中"),
        ("info", "低"),
        ("unexpected", "中"),
        (None, "中"),
    ],
)
def test_severity_normalization_is_shared(raw: object, expected: str) -> None:
    assert normalize_severity(raw) == expected


def test_roundtable_results_use_shared_static_and_llm_normalization() -> None:
    code = "import os\n\ndef deploy(user_command):\n    return os.system(user_command)\n"
    extracted = [
        Issue(
            line_number=4,
            end_line=4,
            issue_type="安全漏洞",
            severity="严重",
            title="命令注入",
            description="外部输入未经约束进入系统命令",
            suggestion="改用参数列表并校验参数",
            cwe="CWE-78",
            evidence="    return os.system(user_command)",
            source="llm",
            cvss_vector=VECTOR_CRITICAL,
        ),
    ]

    merged = _normalize_discussion_issues(
        extracted,
        code=code,
        language="python",
        file_name="deploy.py",
        file_id=11,
    )

    assert len(merged) == 1
    assert merged[0].source == "hybrid"
    assert merged[0].confirmation_count == 2
    assert {detail["source"] for detail in merged[0].source_details} == {
        "static",
        "llm:roundtable",
    }


def test_review_issue_persistence_keeps_provenance_and_cvss_metadata() -> None:
    issue = _issue(
        line=14,
        evidence="os.system(user_command)",
        source="llm:security",
        cwe="CWE-78",
        title="命令注入",
    )
    issue.source_details = [
        {
            "source": "llm:security",
            "confidence": 0.85,
            "evidence": issue.evidence,
            "line_number": 14,
            "title": issue.title,
        },
    ]
    issue.confirmation_count = 1
    issue.finding_fingerprint = "a" * 64

    row = _issue_to_review_issue(
        task_id=3,
        code_file=SimpleNamespace(id=5, file_name="deploy.py"),
        issue=issue,
    )

    assert row.source_details == issue.source_details
    assert row.confirmation_count == 1
    assert row.finding_fingerprint == "a" * 64
    assert row.cvss_score == pytest.approx(9.8)
    assert row.cvss_version == "3.1"
    assert row.cvss_source == "vector"


def test_scoring_breakdown_is_versioned_and_compute_score_stays_compatible() -> None:
    counts = {"严重": 1, "高": 2, "中": 3, "低": 4}
    breakdown = compute_score_breakdown(counts)

    assert SCORING_VERSION == "severity-deduction-v1"
    assert breakdown == {
        "version": SCORING_VERSION,
        "base_score": 100,
        "weights": {"严重": 15, "高": 8, "中": 3, "低": 1},
        "counts": counts,
        "deductions": {"严重": 15, "高": 16, "中": 9, "低": 4},
        "total_deduction": 44,
        "raw_score": 56,
        "score": 56,
        "risk_level": "高风险",
    }
    assert compute_score(counts) == breakdown["score"]
    assert score_risk_level(80) == "低风险"
    assert score_risk_level(60) == "中风险"
    assert score_risk_level(40) == "高风险"
    assert score_risk_level(39) == "极高风险"
