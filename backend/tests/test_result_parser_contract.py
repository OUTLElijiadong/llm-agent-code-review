"""LLM 审查结果解析必须可恢复且不能把坏格式伪装成零发现。"""

from __future__ import annotations

import json

import pytest

from app.ai.exceptions import ResultParseError
from app.ai.result_parser import parse


def _valid_issue(**overrides) -> dict:
    payload = {
        "line_number": 3,
        "issue_type": "安全漏洞",
        "severity": "高",
        "title": "命令注入",
        "description": "外部输入未经约束进入系统命令",
        "suggestion": "使用参数数组并校验允许值",
        "evidence": "os.system(user_input)",
        "confidence": 0.86,
    }
    payload.update(overrides)
    return payload


def test_non_list_issues_is_contract_error_not_clean_result() -> None:
    with pytest.raises(ResultParseError, match="issues.*数组"):
        parse(json.dumps({"summary": "ok", "score": 100, "issues": _valid_issue()}))


def test_mixed_valid_and_invalid_items_keep_valid_with_diagnostics() -> None:
    result = parse(json.dumps({
        "summary": "partial",
        "score": 70,
        "issues": [_valid_issue(), "bad", {}, {"severity": "unknown", "title": "bad"}],
    }))

    assert len(result.issues) == 1
    assert result.input_issue_count == 4
    assert result.invalid_issue_count == 3
    assert {item.code for item in result.diagnostics} == {
        "issue_not_object",
        "issue_missing_identity",
        "issue_invalid_severity",
    }


def test_all_invalid_items_is_contract_error_not_zero_findings() -> None:
    with pytest.raises(ResultParseError, match="全部.*无效"):
        parse(json.dumps({"summary": "bad", "score": 100, "issues": ["bad", {}]}))


def test_missing_confidence_remains_unknown() -> None:
    issue = _valid_issue()
    issue.pop("confidence")

    result = parse(json.dumps({"summary": "one", "score": 60, "issues": [issue]}))

    assert result.issues[0].confidence is None
