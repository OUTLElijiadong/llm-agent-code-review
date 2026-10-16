"""Regression tests for grounding normal code-review findings to source."""

import json

import pytest

from app.agents.review_agent import _issue_to_finding
from app.ai.result_parser import Issue, parse
from app.ai.static_analyzer import Finding
from app.models.code_file import CodeFile
from app.services.issue_merger import finding_to_issue, merge_findings_and_issues
from app.services.review_service import _issue_to_review_issue


def _merge_model_claim(raw_issue: dict, source: str):
    parsed = parse(json.dumps({"issues": [raw_issue], "summary": "done"}))
    finding = _issue_to_finding(parsed.issues[0])
    return merge_findings_and_issues(
        [], [finding_to_issue(finding)], file_id=1, code=source, language="python"
    )[0]


def test_security_claim_with_out_of_range_line_and_no_evidence_is_not_confirmed():
    issue = _merge_model_claim(
        {
            "title": "疑似 SQL 注入",
            "issue_type": "安全漏洞",
            "severity": "严重",
            "line_number": 999,
            "description": "可能存在注入",
        },
        'query = "SELECT 1"\n',
    )

    assert issue.line_number == 0
    assert issue.end_line is None
    assert issue.evidence_quality == "unsupported"
    assert issue.human_review_status == "pending"
    assert issue.conflict_status != "resolved"


def test_security_claim_requires_review_even_with_an_exact_source_quote():
    source = 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")\n'
    issue = _merge_model_claim(
        {
            "title": "SQL 注入",
            "issue_type": "安全漏洞",
            "severity": "高",
            "line_number": 999,
            "evidence": 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")',
            "description": "外部输入可能进入 SQL 查询",
        },
        source,
    )

    assert issue.line_number == 1
    assert issue.end_line == 1
    assert issue.evidence_quality == "verified"
    assert issue.human_review_status == "pending"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("SQL 注入漏洞", "用户控制的 user_id 拼入 SQL 查询"),
        ("越权读取", "未授权用户可以访问其他账号数据"),
        ("Stored XSS", "Untrusted content is rendered as HTML without escaping"),
        ("IDOR in report endpoint", "Cross-tenant report data can be read by changing the identifier."),
        ("对象读取", "这是对象级授权缺陷，可能导致跨租户读取其他用户报告。"),
        ("SQL query construction", "User input is concatenated into the SQL query."),
        ("Query construction", "The SQL query is built by concatenating untrusted input."),
        ("动态 SQL 查询", "用户输入未经转义就拼接进 SQL 查询。"),
    ],
)
def test_security_language_requires_review_even_if_model_labels_issue_as_other(
    title: str, description: str
):
    source = "value = user_input\n"
    issue = _merge_model_claim(
        {
            "title": title,
            "issue_type": "其他",
            "severity": "高",
            "line_number": 1,
            "evidence": "value = user_input",
            "description": description,
        },
        source,
    )

    assert issue.evidence_quality == "verified"
    assert issue.human_review_status == "pending"
    assert issue.aggregation["source_grounding"]["requires_human_review"] is True


def test_generic_performance_risk_is_not_misclassified_as_security():
    issue = _merge_model_claim(
        {
            "title": "重复计算风险",
            "issue_type": "其他",
            "severity": "低",
            "line_number": 1,
            "evidence": "value = compute_once()",
            "description": "循环中重复计算可能影响性能",
        },
        "value = compute_once()\n",
    )

    assert issue.evidence_quality == "verified"
    assert issue.human_review_status == "not_required"


def test_dynamic_sql_claim_requires_review_even_if_model_labels_issue_as_other():
    source = 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")\n'
    issue = _merge_model_claim(
        {
            "title": "Dynamic SQL query uses a request parameter",
            "issue_type": "其他",
            "severity": "高",
            "line_number": 1,
            "evidence": 'cursor.execute(f"SELECT * FROM users WHERE id={user_id}")',
            "description": "The user-controlled identifier is interpolated into the query without escaping.",
            "confidence": 0.999,
        },
        source,
    )

    assert issue.evidence_quality == "verified"
    assert issue.confidence <= 0.98
    assert issue.human_review_status == "pending"
    assert issue.aggregation["source_grounding"]["requires_human_review"] is True


def test_security_claim_with_valid_line_but_no_quote_is_inferred_and_pending():
    issue = _merge_model_claim(
        {
            "title": "疑似 SQL 注入",
            "issue_type": "安全漏洞",
            "severity": "高",
            "line_number": 1,
            "description": "可能存在注入",
        },
        'query = "SELECT 1"\n',
    )

    assert issue.line_number == 1
    assert issue.evidence_quality == "inferred"
    assert issue.human_review_status == "pending"


def test_non_security_claim_with_impossible_location_is_unlocated_and_pending():
    issue = _merge_model_claim(
        {
            "title": "资源未释放",
            "issue_type": "潜在Bug",
            "severity": "中",
            "line_number": 12,
            "description": "文件句柄可能未关闭",
        },
        "value = 1\n",
    )

    assert issue.line_number == 0
    assert issue.end_line is None
    assert issue.evidence_quality == "unsupported"
    assert issue.human_review_status == "pending"


def test_static_findings_are_not_downgraded_by_llm_grounding_gate():
    issue = merge_findings_and_issues(
        [
            # Static rules are generated from a deterministic source scan and
            # retain their established status/coordinates.
            Finding(
                line_number=1,
                end_line=1,
                issue_type="安全漏洞",
                severity="高",
                title="硬编码凭据",
                description="检测到疑似硬编码凭据",
                evidence='password = "secret"',
                source="static",
                cwe="CWE-798",
            )
        ],
        [],
        file_id=1,
        code='password = "secret"\n',
        language="python",
    )[0]

    assert issue.source == "static"
    assert issue.line_number == 1
    assert issue.human_review_status == "not_required"


def test_repeated_quote_with_no_disambiguating_line_is_not_assigned_arbitrarily():
    issue = _merge_model_claim(
        {
            "title": "危险调用",
            "issue_type": "安全漏洞",
            "severity": "高",
            "line_number": 80,
            "evidence": "dangerous(user_input)",
            "description": "可能存在不安全调用",
        },
        "dangerous(user_input)\nprint('separator')\ndangerous(user_input)\n",
    )

    assert issue.line_number == 0
    assert issue.end_line is None
    assert issue.evidence_quality == "verified"
    assert issue.human_review_status == "pending"
    assert issue.aggregation["source_grounding"]["location_state"] == "source_quote_location_ambiguous"


def test_non_security_claim_with_mismatched_quote_is_pending_review():
    issue = _merge_model_claim(
        {
            "title": "可能遗漏异常处理",
            "issue_type": "潜在Bug",
            "severity": "中",
            "line_number": 1,
            "evidence": "close(resource)",
            "description": "资源可能未释放",
        },
        "value = open(path)\n",
    )

    assert issue.line_number == 1
    assert issue.evidence_quality == "direct"
    assert issue.human_review_status == "pending"
    assert issue.aggregation["source_grounding"]["evidence_match"] is False


def test_grounded_non_security_finding_keeps_normal_review_state():
    issue = _merge_model_claim(
        {
            "title": "重复计算",
            "issue_type": "性能问题",
            "severity": "低",
            "line_number": 1,
            "evidence": "value = compute_once()",
            "description": "可缓存重复结果",
        },
        "value = compute_once()\n",
    )

    assert issue.evidence_quality == "verified"
    assert issue.human_review_status == "not_required"


def test_unavailable_source_is_never_treated_as_verified_or_auto_accepted():
    parsed = parse(
        '{"issues":[{"title":"SQL 注入","issue_type":"安全漏洞",'
        '"severity":"高","line_number":8,"confidence":0.99,'
        '"description":"可能存在注入"}]}'
    )
    issue = merge_findings_and_issues(
        [], [finding_to_issue(_issue_to_finding(parsed.issues[0]))], file_id=1
    )[0]

    assert issue.line_number == 8
    assert issue.evidence_quality == "unsupported"
    assert issue.human_review_status == "pending"
    assert issue.confidence == pytest.approx(0.45)
    assert issue.aggregation["source_grounding"]["location_state"] == "source_unavailable"


def test_non_finite_confidence_cannot_escape_evidence_caps():
    issue = Issue(
        line_number=1,
        issue_type="其他",
        severity="高",
        title="对象读取风险",
        description="Cross-tenant report data can be read by changing the identifier.",
        evidence="return repository.get(report_id)",
        source="llm:security",
        confidence=float("nan"),
        source_details=[
            {
                "source": "llm:security",
                "confidence": float("nan"),
                "evidence": "return repository.get(report_id)",
                "line_number": 1,
            }
        ],
    )
    grounded = merge_findings_and_issues(
        [], [issue], file_id=1, code="return repository.get(report_id)\n", language="python"
    )[0]

    assert grounded.human_review_status == "pending"
    assert grounded.confidence is None or 0.0 <= grounded.confidence <= 0.98
    assert all(0.0 <= item["confidence"] <= 0.98 for item in grounded.source_details)


def test_pending_grounding_status_is_preserved_in_report_issue_record():
    source = "query = 'SELECT 1'\n"
    issue = _merge_model_claim(
        {
            "title": "疑似 SQL 注入",
            "issue_type": "安全漏洞",
            "severity": "高",
            "line_number": 90,
            "description": "可能存在注入",
        },
        source,
    )
    code_file = CodeFile(
        id=1, project_id=1, file_name="query.py", file_path="query.py",
        language="python", content=source, size_bytes=len(source.encode("utf-8")),
        line_count=1, version_no=1, status="active", is_binary=0,
    )

    record = _issue_to_review_issue(7, code_file, issue)

    assert record.status == "pending_review"
    assert record.human_review_status == "pending"
    assert record.line_number == 0
    assert record.aggregation_json["source_grounding"]["reported_line_number"] == 90


def test_collaborator_source_details_keep_each_reported_and_grounded_line():
    source = "dangerous(user_input)\n"
    quote = "dangerous(user_input)"
    issue = Issue(
        line_number=999,
        end_line=999,
        issue_type="安全漏洞",
        severity="高",
        title="危险调用",
        description="不可信输入进入危险函数",
        evidence=quote,
        source="llm_collab",
        source_details=[
            {"source": "llm:security", "line_number": 999, "evidence": quote},
            {"source": "llm:reviewer", "line_number": 1, "evidence": quote},
        ],
    )

    merged = merge_findings_and_issues([], [issue], 1, code=source, language="python")[0]

    assert merged.line_number == 1
    assert merged.human_review_status == "pending"
    details = {item["source"]: item for item in merged.source_details}
    assert details["llm:security"]["reported_line_number"] == 999
    assert details["llm:security"]["line_number"] == 1
    assert details["llm:reviewer"]["reported_line_number"] == 1
    assert details["llm:reviewer"]["line_number"] == 1


def test_model_source_anchor_is_replaced_with_verified_anchor_in_source_details():
    source = "dangerous(user_input)\n"
    issue = Issue(
        line_number=999,
        issue_type="安全漏洞",
        severity="高",
        title="危险调用",
        description="不可信输入进入危险函数",
        evidence="dangerous(user_input)",
        source="llm_collab",
        source_details=[
            {
                "source": "llm:security",
                "line_number": 999,
                "evidence": "dangerous(user_input)",
                "source_anchor": "MODEL-FABRICATED-ANCHOR",
            },
        ],
    )

    merged = merge_findings_and_issues([], [issue], 1, code=source, language="python")[0]

    assert merged.source_anchor
    assert merged.source_anchor != "MODEL-FABRICATED-ANCHOR"
    assert merged.source_details[0]["source_anchor"] == merged.source_anchor
