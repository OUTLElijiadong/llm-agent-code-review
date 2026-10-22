"""LLM 审查结果解析必须可恢复且不能把坏格式伪装成零发现。"""

from __future__ import annotations

import json

import pytest

from app.ai.exceptions import ResultParseError
from app.ai.result_parser import load_json_object, parse


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


def test_mixed_items_preserve_ungraded_findings_with_diagnostics() -> None:
    result = parse(json.dumps({
        "summary": "partial",
        "score": 70,
        "issues": [_valid_issue(), "bad", {}, {"severity": "unknown", "title": "bad"}],
    }))

    assert [(issue.title, issue.severity) for issue in result.issues] == [
        ("命令注入", "高"),
        ("bad", "未分级"),
    ]
    # 枚举错误仍是诊断，但不能把真实发现丢掉或伪装成中危后参与评分。
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


@pytest.mark.parametrize("line_field", ["line_number", "end_line"])
def test_boolean_source_line_is_rejected(line_field: str) -> None:
    payload = _valid_issue(**{line_field: True})

    with pytest.raises(ResultParseError, match="全部.*无效"):
        parse(json.dumps({"summary": "bad line", "score": 60, "issues": [payload]}))


def test_scan_trailing_comma_at_reported_column_preserves_finding() -> None:
    """同为第 3069 列的多余尾逗号必须恢复完整发现，而不能补成空 issues。"""
    issue = json.dumps(_valid_issue(), ensure_ascii=False)
    prefix = '{"summary":"'
    suffix = '","score":60,"issues":[' + issue + ',]}'
    raw = prefix + '审' * (3068 - len(prefix) - len(suffix) + 2) + suffix
    with pytest.raises(json.JSONDecodeError) as malformed:
        json.loads(raw)
    assert (malformed.value.msg, malformed.value.lineno, malformed.value.colno) == (
        "Expecting value", 1, 3069,
    )

    result = parse(raw)

    assert result.input_issue_count == 1
    assert result.invalid_issue_count == 0
    assert result.issues[0].title == "命令注入"
    assert result.issues[0].evidence == "os.system(user_input)"
    assert result.issues[0].line_number == 3
    assert result.issues[0].severity == "高"


@pytest.mark.parametrize("wrapped", [False, True])
def test_json_format_repair_preserves_quoted_code_and_all_findings(wrapped: bool) -> None:
    evidence = 'print("quoted\\path,] ,}") # ```json ```'
    body = json.dumps({
        "summary": "审查", "score": 40,
        "issues": [_valid_issue(evidence=evidence), _valid_issue(title="第二个发现")],
    }, ensure_ascii=False)
    raw = body[:-2] + ',],}'
    if wrapped:
        raw = f"```json\n{raw}\n```"

    result = parse(raw)

    assert result.input_issue_count == len(result.issues) == 2
    assert result.invalid_issue_count == 0
    assert result.issues[0].evidence == evidence
    assert result.issues[1].title == "第二个发现"


@pytest.mark.parametrize("raw", [
    '{"issues":[,]}',
    '{"issues":[{"title":"命令注入"},,]}',
    '{"issues":[{"title":"命令注入"},',
    '{"issues":[{"title":"命令注入","confidence":}]}',
    '{"issues":None}',
    '{"issues":[...]}',
    '{"issues":[{"title":"命令注入"}]',
])
def test_json_repair_never_invents_missing_values_or_closing_brackets(raw: str) -> None:
    with pytest.raises(ResultParseError):
        parse(raw)


@pytest.mark.parametrize("raw", [
    '{"issues":[{"title":"命令注入"}],"issues":[]}',
    '{"issues":[]} {"issues":[{"title":"命令注入"}]}',
    '{"issues":[],"score":NaN}',
    '{"issues":[],"score":1e400}',
    '以下是结果： {"issues":[]} {"issues":[',
    '{"issues":[]}}',
    '{"issues":[]}, {"issues":[{"title":"命令注入"}]}',
    '以下是结果：\n```json\n{"issues":[]}\n```\n{"issues":[{"title":"命令注入"}]}',
    '说明文字\n{"issues":[{"title":"命令注入","evidence":"synthetic"}]}\n```json\n{"issues":[]}\n```',
    '说明文字\n{"issues":[]}\n```json\n{"issues":[{"title":"命令注入"}]}\n```',
])
def test_ambiguous_or_nonfinite_json_cannot_be_a_clean_scan(raw: str) -> None:
    with pytest.raises(ResultParseError):
        parse(raw)


@pytest.fixture
def redacted_bare_severity_review() -> tuple[str, dict]:
    """仅复现四条发现、完整字段及两处裸枚举；所有正文均为合成脱敏内容。"""
    evidence = 'sample = {"severity":低, "quoted": "\\path"}; markers = ",] ,}"'
    issues = [
        _valid_issue(
            line_number=3 + index * 10, end_line=5 + index * 10,
            severity="高" if index < 2 else "低",
            title=f"脱敏发现 {index + 1}", description=f"合成说明 {index + 1}",
            suggestion="校验输入并报告错误", fixed_code='check("sample")\nreturn value',
            owasp="A03:2021-Injection", cwe="CWE-20", evidence=evidence,
            exploit_scenario="合成输入触发校验缺口",
            references=["https://example.invalid/reference"], confidence=0.8,
            cvss_score=None, cvss_vector=None, remediation="添加输入校验及失败测试",
        )
        for index in range(4)
    ]
    expected = {"summary": "合成审查结果", "score": 58, "issues": issues}

    def encode() -> str:
        encoded_issues = []
        for index, issue in enumerate(issues):
            encoded = json.dumps(issue, ensure_ascii=False, separators=(",", ":"))
            if index >= 2:
                encoded = encoded.replace('"severity":"低"', '"severity":低', 1)
            encoded_issues.append(encoded)
        return (
            '{"summary":' + json.dumps(expected["summary"], ensure_ascii=False)
            + ',"score":58,"issues":[' + ",".join(encoded_issues) + "]}"
        )

    raw = encode()
    first_bare_value = raw.index('"severity":低') + len('"severity":')
    expected["summary"] += "审" * (3068 - first_bare_value)
    return encode(), expected


def test_scan_bare_severity_at_reported_column_preserves_every_field(
    redacted_bare_severity_review: tuple[str, dict],
) -> None:
    raw, expected = redacted_bare_severity_review
    with pytest.raises(json.JSONDecodeError) as malformed:
        json.loads(raw)
    assert (malformed.value.msg, malformed.value.pos, malformed.value.colno) == (
        "Expecting value", 3068, 3069,
    )

    assert load_json_object(raw) == expected
    result = parse(raw)
    assert result.input_issue_count == len(result.issues) == 4
    assert result.invalid_issue_count == 0
    for issue, original in zip(result.issues, expected["issues"]):
        for name in original:
            assert getattr(issue, name) == original[name]


@pytest.mark.parametrize("severity", ["严重", "高", "中", "低"])
def test_bare_severity_repair_only_quotes_registered_enum_outside_strings(severity: str) -> None:
    evidence = 'literal "severity":低, escaped \\" text and ,] ,} fences ```json ```'
    expected = {"issues": [_valid_issue(severity=severity, evidence=evidence)]}
    raw = json.dumps(expected, ensure_ascii=False).replace(
        f'"severity": "{severity}"', f'"severity": \n {severity} ', 1,
    )
    assert load_json_object(raw) == expected


@pytest.mark.parametrize("raw", [
    '{"issues":[{"title":"脱敏发现","severity":低风险}]}',
    '{"issues":[{"title":"脱敏发现","severity":unknown}]}',
    '{"issues":[{"title":"脱敏发现","severity":}]}',
    '{"issues":[{"title":"脱敏发现","evidence":低}]}',
    '{"issues":[{"title":"脱敏发现","severity":低},{"title":"截断',
    '{"issues":[{"title":"脱敏发现","severity":低',
    '{"issues":[{"title":"脱敏发现","severity":低,"severity":"高"}]}',
    '{"issues":[{"title":"脱敏发现","severity":低}]} {"issues":[]}',
])
def test_bare_severity_repair_rejects_unknown_values_truncation_and_ambiguity(raw: str) -> None:
    with pytest.raises(ResultParseError):
        load_json_object(raw)


def test_bare_severity_and_trailing_commas_repair_preserves_escaped_field_name() -> None:
    raw = '```json\n{"issues":[{"title":"脱敏发现","\\u0073everity":低,},],}\n```'
    assert load_json_object(raw) == {"issues": [{"title": "脱敏发现", "severity": "低"}]}


@pytest.mark.parametrize(("field", "value"), [
    ("issue_type", value) for value in (
        "代码规范", "潜在Bug", "安全漏洞", "性能问题", "异常处理",
        "命名规范", "可维护性", "注释完整性", "其他",
    )
] + [("severity", value) for value in ("严重", "高", "中", "低")])
@pytest.mark.parametrize("quotes", ["neither", "opening", "closing"])
def test_registered_enum_missing_quotes_preserves_complete_payload(field, value, quotes) -> None:
    """生产日志20696/20706及20209：枚举缺一侧或两侧引号，不改正文。"""
    evidence = 'literal "issue_type":代码规范, "severity":"中, and escaped \\" quote'
    expected = {"summary": "脱敏原文", "issues": [_valid_issue(evidence=evidence, **{field: value})]}
    encoded = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    broken = value
    if quotes == "opening":
        broken = '"' + broken
    elif quotes == "closing":
        broken += '"'
    raw = encoded.replace(json.dumps(field) + ":" + json.dumps(value, ensure_ascii=False),
                          json.dumps(field) + ":" + broken, 1)
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert load_json_object(raw) == expected
    result = parse(raw)
    assert result.input_issue_count == 1
    assert result.invalid_issue_count == 0
    assert result.issues[0].evidence == evidence


def test_real_missing_issue_type_quotes_column_4068_preserves_both_findings() -> None:
    expected = {"summary": "", "score": 61, "issues": [
        _valid_issue(), _valid_issue(issue_type="代码规范", title="脱敏第二项"),
    ]}
    def encode():
        return json.dumps(expected, ensure_ascii=False, separators=(",", ":")).replace(
            '"issue_type":"代码规范"', '"issue_type":代码规范', 1,
        )
    offset = encode().index('"issue_type":代码规范') + len('"issue_type":')
    expected["summary"] = "审" * (4067 - offset)
    raw = encode()
    with pytest.raises(json.JSONDecodeError) as malformed:
        json.loads(raw)
    assert (malformed.value.pos, malformed.value.colno) == (4067, 4068)
    assert load_json_object(raw) == expected
    assert len(parse(raw).issues) == 2


@pytest.mark.parametrize("raw", [
    '{"issues":[{"title":"脱敏发现","issue_type":未知类型}]}',
    '{"issues":[{"title":"脱敏发现","issue_type":代码规范扩展}]}',
    '{"issues":[{"title":"脱敏发现","issue_type":代码规范,"description":"截断',
    '{"issues":[{"title":"脱敏发现","severity":"中',
    '{"issues":[{"title":"脱敏发现","issue_type":代码规范,"issue_type":"安全漏洞"}]}',
    '{"issues":[{"title":"脱敏发现","other_field":代码规范}]}',
    '{"issues":[{"title":"脱敏发现","severity":"中,"description":"截断',
])
def test_enum_quote_repair_rejects_unknown_missing_values_and_ambiguous_output(raw) -> None:
    with pytest.raises(ResultParseError):
        load_json_object(raw)


def test_enum_quote_repair_keeps_valid_string_ending_in_comma_and_repairs_trailing_comma() -> None:
    expected = {"issues": [_valid_issue(severity="中,", evidence='"issue_type":代码规范,]')]}
    raw = json.dumps(expected, ensure_ascii=False)[:-2] + ",]}"
    assert load_json_object(raw) == expected


def test_enum_quote_repair_and_trailing_commas_can_coexist() -> None:
    raw = '{"issues":[{"title":"脱敏发现","issue_type":代码规范,"severity":"中,},],}'
    assert load_json_object(raw) == {"issues": [{
        "title": "脱敏发现", "issue_type": "代码规范", "severity": "中",
    }]}
