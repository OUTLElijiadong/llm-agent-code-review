"""T11 报告导出服务单元测试

覆盖范围:
- JSON / dict / HTML 三种导出形态的结构与内容
- 3 套 Jinja2 模板(simple/detailed/compliance)渲染与关键字段
- 按严重度分组(严重>高>中>低)、按类型分组(9 种 issue_type)
- 合规摘要聚合(4 套标准)、Top N 漏洞按 CVSS 降序
- 空问题列表、单问题、多问题混合场景
- IssueOut Pydantic 模型与原生 dict 输入兼容性
- 内置模板载入
"""
from datetime import datetime
from typing import Any, Dict, List

import pytest

from app.schemas.review import IssueOut
from app.services.report_exporter import (
    _build_compliance_summary,
    _build_report_context,
    _build_top_vulnerabilities,
    _group_issues_by_severity,
    _group_issues_by_type,
    export_to_dict,
    export_to_html,
    export_to_json,
    load_builtin_template,
)


# ============ 测试数据工厂 ============

def _make_task(**overrides: Any) -> Dict[str, Any]:
    """构造测试用 task 字典。

    Args:
        **overrides: 覆盖默认字段的关键字参数。

    Returns:
        Dict[str, Any]: 含 task 基础字段的字典。
    """
    base: Dict[str, Any] = {
        "id": 1,
        "task_name": "示例审查任务",
        "project_id": 10,
        "project_name": "示例项目",
        "review_type": "security",
        "status": "success",
        "total_files": 3,
        "processed_files": 3,
        "total_issues": 5,
        "severe_issues": 1,
        "high_issues": 2,
        "medium_issues": 1,
        "low_issues": 1,
        "score": 72,
        "summary": "发现多处安全漏洞,建议尽快修复。",
        "model_name": "deepseek-v3",
        "duration_ms": 5600,
        "start_time": "2026-06-25T10:00:00",
        "end_time": "2026-06-25T10:05:36",
        "create_time": "2026-06-25T10:05:36",
    }
    base.update(overrides)
    return base


def _make_issue(**overrides: Any) -> Dict[str, Any]:
    """构造测试用 issue 字典。

    Args:
        **overrides: 覆盖默认字段的关键字参数。

    Returns:
        Dict[str, Any]: 含 issue 全量字段的字典。
    """
    base: Dict[str, Any] = {
        "id": 1,
        "task_id": 1,
        "file_id": 100,
        "file_name": "app.py",
        "line_number": 42,
        "end_line": 45,
        "issue_type": "安全漏洞",
        "severity": "高",
        "title": "SQL 注入漏洞",
        "description": "用户输入未经过滤直接拼接进 SQL 查询,可能导致数据泄露。",
        "suggestion": "使用参数化查询替代字符串拼接。",
        "fixed_code": "cursor.execute(\"SELECT * FROM users WHERE id=%s\", (user_id,))",
        "status": "unfixed",
        "create_time": "2026-06-25T10:05:00",
        "owasp": "A03:2021-Injection",
        "cwe": "CWE-89",
        "evidence": "query = \"SELECT * FROM users WHERE id=\" + user_id",
        "exploit_scenario": "攻击者构造 user_id=1 OR 1=1 可绕过认证获取全部用户数据。",
        "references_json": ["https://owasp.org/www-community/attacks/SQL_Injection"],
        "confidence": 0.95,
        "source": "llm",
        "cvss_score": 8.6,
        "cvss_vector": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        "compliance_mapping": {
            "iso27001": ["A.8.25", "A.8.28"],
            "gdpr": ["Art.32"],
            "pci_dss": ["Req-6.4.1"],
            "hipaa": ["§164.312(a)"],
        },
        "remediation": "1. 改用参数化查询;2. 增加输入校验;3. 最小化数据库账号权限。",
        "static_rule_hits": 2,
    }
    base.update(overrides)
    return base


def _make_mixed_issues() -> List[Dict[str, Any]]:
    """构造覆盖 4 种严重度 + 9 种 issue_type 的混合 issue 列表。

    Returns:
        List[Dict[str, Any]]: 9 条 issue,覆盖严重/高/中/低 与 9 种类型。
    """
    types = ["安全漏洞", "潜在Bug", "性能问题", "代码风格", "可维护性",
             "兼容性", "并发问题", "资源泄漏", "配置问题"]
    severities = ["严重", "高", "高", "中", "中", "低", "低", "严重", "高"]
    cwes = ["CWE-89", "CWE-79", "CWE-78", "CWE-22", "CWE-200",
            "CWE-327", "CWE-502", "CWE-798", "CWE-918"]
    cvss = [9.1, 8.6, 8.2, 6.5, 5.4, 3.2, 7.1, 9.8, 8.0]
    issues: List[Dict[str, Any]] = []
    for idx in range(9):
        issues.append(_make_issue(
            id=idx + 1,
            issue_type=types[idx],
            severity=severities[idx],
            title=f"{types[idx]} #{idx + 1}",
            description=f"{types[idx]} 详细描述。",
            cwe=cwes[idx],
            cvss_score=cvss[idx],
            compliance_mapping=None,  # 触发按 cwe 自动补全
            file_name=f"file_{idx + 1}.py",
            line_number=idx * 10 + 1,
        ))
    return issues


# ============ JSON / dict 导出结构 ============

def test_export_to_json_returns_parseable_json_with_required_keys():
    """JSON 导出应可被 json.loads 解析,且包含 5 个顶层键。"""
    task = _make_task()
    issues = [_make_issue()]
    raw = export_to_json(task, issues, "摘要", 80)
    import json
    payload = json.loads(raw)
    assert set(["task_info", "summary", "score", "issues", "statistics"]).issubset(payload.keys())


def test_export_to_json_contains_task_info_and_score():
    """JSON 字符串应包含 task_name 与 score 文本。"""
    task = _make_task(task_name="JSON任务")
    raw = export_to_json(task, [], "摘要", 88)
    assert "JSON任务" in raw
    assert "88" in raw


def test_export_to_json_preserves_chinese_chars():
    """JSON 应使用 ensure_ascii=False,中文不转义。"""
    raw = export_to_json(_make_task(), [_make_issue(severity="严重")], "摘要", 70)
    assert "严重" in raw
    assert "\\u" not in raw


def test_export_to_dict_returns_dict_with_required_keys():
    """dict 导出应返回字典且包含 5 个顶层键。"""
    result = export_to_dict(_make_task(), [], None, 60)
    assert isinstance(result, dict)
    assert "task_info" in result
    assert "summary" in result
    assert "score" in result
    assert "issues" in result
    assert "statistics" in result


def test_export_to_dict_statistics_contains_required_fields():
    """statistics 应包含 severity_count / type_count / cwe_count / compliance_summary / top_vulnerabilities。"""
    result = export_to_dict(_make_task(), _make_mixed_issues(), "摘要", 70)
    stats = result["statistics"]
    for key in ("total_issues", "fixed_count", "severity_count", "type_count",
                "cwe_count", "compliance_summary", "top_vulnerabilities"):
        assert key in stats, f"statistics 缺少字段 {key}"


# ============ HTML 导出(3 套模板) ============

def test_export_to_html_simple_renders_with_key_content():
    """简洁版模板渲染应包含任务名、分数与问题列表。"""
    task = _make_task(task_name="简洁任务")
    issues = [_make_issue(title="问题A"), _make_issue(id=2, title="问题B", severity="中")]
    tpl = load_builtin_template("simple")
    html = export_to_html(task, issues, "总体评价", 75, tpl)
    assert "简洁任务" in html
    assert "75" in html
    assert "问题A" in html
    assert "问题B" in html
    assert "<table" in html


def test_export_to_html_detailed_renders_with_key_content():
    """详细版模板渲染应包含任务名、分数、问题描述与修复代码。"""
    task = _make_task(task_name="详细任务")
    issues = [_make_issue(title="SQL注入")]
    tpl = load_builtin_template("detailed")
    html = export_to_html(task, issues, "详细评价", 65, tpl)
    assert "详细任务" in html
    assert "SQL注入" in html
    assert "参数化查询" in html  # suggestion
    assert "<pre>" in html and "<code>" in html  # 修复代码
    assert "SQL注入" in html  # 标题(已覆盖)


def test_export_to_html_compliance_renders_with_key_content():
    """合规版模板渲染应包含任务名、4 标准概览与合规条款编号。"""
    task = _make_task(project_name="合规项目")
    issues = [_make_issue(cwe="CWE-89"), _make_issue(id=2, cwe="CWE-79", title="XSS")]
    tpl = load_builtin_template("compliance")
    html = export_to_html(task, issues, "合规评价", 55, tpl)
    assert "合规项目" in html
    assert "ISO 27001" in html
    assert "GDPR" in html
    assert "PCI-DSS" in html
    assert "HIPAA" in html
    assert "A.8.25" in html or "A.8.28" in html  # 合规条款编号
    assert "<code>" in html


def test_export_to_html_simple_uses_inline_css():
    """简洁版模板应使用内联 CSS(<style> 标签),不依赖外部样式表。"""
    tpl = load_builtin_template("simple")
    html = export_to_html(_make_task(), [], "", 80, tpl)
    assert "<style>" in html
    assert "<link" not in html  # 无外部样式表


def test_export_to_html_detailed_contains_evidence_and_exploit():
    """详细版应渲染 evidence 与 exploit_scenario。"""
    issues = [_make_issue(evidence="evidence_line", exploit_scenario="攻击场景X")]
    tpl = load_builtin_template("detailed")
    html = export_to_html(_make_task(), issues, "", 70, tpl)
    assert "evidence_line" in html
    assert "攻击场景X" in html


def test_export_to_html_detailed_includes_anchor_links():
    """详细版应为每个问题生成锚点 id。"""
    issues = [_make_issue(id=101), _make_issue(id=102, title="第二个")]
    tpl = load_builtin_template("detailed")
    html = export_to_html(_make_task(), issues, "", 70, tpl)
    assert 'id="issue-101"' in html
    assert 'id="issue-102"' in html
    assert "#issue-101" in html  # TOC 链接


def test_export_to_html_compliance_contains_cvss_distribution_and_top10():
    """合规版应包含 CVSS 分布章节与 Top 10 漏洞表格。"""
    issues = _make_mixed_issues()
    tpl = load_builtin_template("compliance")
    html = export_to_html(_make_task(), issues, "", 50, tpl)
    assert "CVSS 评分分布" in html
    assert "Top 10" in html
    assert "cvss-badge" in html or "cvss-bar" in html


def test_export_to_html_compliance_renders_cvss_vector():
    """合规版应渲染 cvss_vector 字段。"""
    issues = [_make_issue(cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")]
    tpl = load_builtin_template("compliance")
    html = export_to_html(_make_task(), issues, "", 50, tpl)
    assert "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H" in html


def test_template_rendering_includes_cvss_score_field():
    """详细版与合规版均应包含 cvss_score 数值。"""
    issues = [_make_issue(cvss_score=8.6)]
    for tpl_type in ("detailed", "compliance"):
        tpl = load_builtin_template(tpl_type)
        html = export_to_html(_make_task(), issues, "", 60, tpl)
        assert "8.6" in html


def test_template_rendering_includes_compliance_mapping():
    """合规版应渲染 compliance_mapping 中的条款编号。"""
    issues = [_make_issue(compliance_mapping={
        "iso27001": ["A.8.25"], "gdpr": ["Art.32"],
        "pci_dss": ["Req-6.4.1"], "hipaa": ["§164.312(a)"],
    })]
    tpl = load_builtin_template("compliance")
    html = export_to_html(_make_task(), issues, "", 60, tpl)
    assert "Art.32" in html
    assert "Req-6.4.1" in html


# ============ 按严重度分组 ============

def test_group_issues_by_severity_returns_four_groups_in_order():
    """按严重度分组应返回 严重/高/中/低 4 组,顺序固定。"""
    issues = [
        _make_issue(id=1, severity="低"),
        _make_issue(id=2, severity="严重"),
        _make_issue(id=3, severity="中"),
        _make_issue(id=4, severity="高"),
    ]
    grouped = _group_issues_by_severity(issues)
    assert list(grouped.keys())[:4] == ["严重", "高", "中", "低"]
    assert len(grouped["严重"]) == 1
    assert len(grouped["高"]) == 1
    assert len(grouped["中"]) == 1
    assert len(grouped["低"]) == 1
    assert grouped["严重"][0]["id"] == 2


def test_group_issues_by_severity_handles_unknown_severity():
    """未知严重度应归入 '其他' 组。"""
    issues = [_make_issue(id=1, severity="未知"), _make_issue(id=2, severity="严重")]
    grouped = _group_issues_by_severity(issues)
    assert "其他" in grouped
    assert len(grouped["其他"]) == 1
    assert grouped["其他"][0]["id"] == 1


def test_group_issues_by_severity_empty_input():
    """空列表分组应返回空分组结构。"""
    grouped = _group_issues_by_severity([])
    assert grouped["严重"] == []
    assert grouped["高"] == []
    assert grouped["中"] == []
    assert grouped["低"] == []


# ============ 按类型分组 ============

def test_group_issues_by_type_returns_nine_types():
    """按类型分组应返回 9 种 issue_type。"""
    issues = _make_mixed_issues()
    grouped = _group_issues_by_type(issues)
    assert len(grouped) == 9
    expected_types = {"安全漏洞", "潜在Bug", "性能问题", "代码风格", "可维护性",
                      "兼容性", "并发问题", "资源泄漏", "配置问题"}
    assert set(grouped.keys()) == expected_types


def test_group_issues_by_type_preserves_issues_count():
    """每个类型分组应保留正确的 issue 数量。"""
    issues = [
        _make_issue(id=1, issue_type="安全漏洞"),
        _make_issue(id=2, issue_type="安全漏洞"),
        _make_issue(id=3, issue_type="性能问题"),
    ]
    grouped = _group_issues_by_type(issues)
    assert len(grouped["安全漏洞"]) == 2
    assert len(grouped["性能问题"]) == 1


def test_group_issues_by_type_sorted_by_key():
    """类型分组键应按字典序升序排列。"""
    issues = [
        _make_issue(id=1, issue_type="性能问题"),
        _make_issue(id=2, issue_type="安全漏洞"),
        _make_issue(id=3, issue_type="代码风格"),
    ]
    grouped = _group_issues_by_type(issues)
    keys = list(grouped.keys())
    assert keys == sorted(keys)


# ============ 合规摘要聚合 ============

def test_build_compliance_summary_aggregates_four_standards():
    """合规摘要应聚合 4 套标准,且命中条目数正确。"""
    issues = [
        _make_issue(id=1, cwe="CWE-89", compliance_mapping=None),
        _make_issue(id=2, cwe="CWE-79", compliance_mapping=None),
    ]
    summary = _build_compliance_summary(issues)
    assert set(summary.keys()) == {"iso27001", "gdpr", "pci_dss", "hipaa"}
    # 两条 issue 均映射到 4 标准
    assert summary["iso27001"]["total_findings"] == 2
    assert summary["gdpr"]["total_findings"] == 2
    assert summary["pci_dss"]["total_findings"] == 2
    assert summary["hipaa"]["total_findings"] == 2
    # covered 字段非空
    assert len(summary["iso27001"]["covered_controls"]) > 0
    assert len(summary["gdpr"]["covered_articles"]) > 0
    assert len(summary["pci_dss"]["covered_requirements"]) > 0
    assert len(summary["hipaa"]["covered_sections"]) > 0


def test_build_compliance_summary_uses_explicit_mapping_when_provided():
    """当 issue 提供 compliance_mapping 时应直接使用,不按 cwe 补全。"""
    issues = [_make_issue(
        cwe="CWE-89",
        compliance_mapping={
            "iso27001": ["A.8.25"], "gdpr": [], "pci_dss": [], "hipaa": [],
        },
    )]
    summary = _build_compliance_summary(issues)
    assert summary["iso27001"]["total_findings"] == 1
    assert summary["gdpr"]["total_findings"] == 0
    assert "A.8.25" in summary["iso27001"]["covered_controls"]


def test_build_compliance_summary_empty_issues():
    """空问题列表时合规摘要所有 total_findings 为 0,covered 为空。"""
    summary = _build_compliance_summary([])
    for std in ("iso27001", "gdpr", "pci_dss", "hipaa"):
        assert summary[std]["total_findings"] == 0


def test_build_compliance_summary_deduplicates_codes():
    """合规条款编号应去重。"""
    issues = [
        _make_issue(id=1, cwe="CWE-89", compliance_mapping=None),
        _make_issue(id=2, cwe="CWE-89", compliance_mapping=None),
    ]
    summary = _build_compliance_summary(issues)
    # CWE-89 的 iso27001 映射为 ["A.8.25", "A.8.26", "A.8.28", "A.8.29"],去重后仍为 4 条
    assert len(summary["iso27001"]["covered_controls"]) == 4
    assert summary["iso27001"]["total_findings"] == 2


# ============ Top N 漏洞 ============

def test_build_top_vulnerabilities_sorted_by_cvss_desc():
    """Top N 应按 cvss_score 降序排序。"""
    issues = [
        _make_issue(id=1, cvss_score=5.0),
        _make_issue(id=2, cvss_score=9.8),
        _make_issue(id=3, cvss_score=7.2),
    ]
    top = _build_top_vulnerabilities(issues, top_n=3)
    assert [t["id"] for t in top] == [2, 3, 1]
    assert top[0]["cvss_score"] == 9.8


def test_build_top_vulnerabilities_respects_top_n():
    """Top N 应截断到指定数量。"""
    issues = _make_mixed_issues()
    top = _build_top_vulnerabilities(issues, top_n=3)
    assert len(top) == 3


def test_build_top_vulnerabilities_handles_none_cvss():
    """cvss_score 为 None 的 issue 应排到最后。"""
    issues = [
        _make_issue(id=1, cvss_score=None),
        _make_issue(id=2, cvss_score=4.0),
    ]
    top = _build_top_vulnerabilities(issues, top_n=2)
    assert top[0]["id"] == 2
    assert top[1]["id"] == 1


def test_build_top_vulnerabilities_empty_returns_empty_list():
    """空列表应返回空列表。"""
    assert _build_top_vulnerabilities([], top_n=10) == []


def test_build_top_vulnerabilities_zero_top_n_returns_empty():
    """top_n<=0 应返回空列表。"""
    issues = [_make_issue(cvss_score=9.0)]
    assert _build_top_vulnerabilities(issues, top_n=0) == []


# ============ 空场景 / 单问题 / 混合场景 ============

def test_empty_issues_does_not_raise_and_statistics_zero():
    """空问题列表场景下导出函数不应报错,统计字段为 0。"""
    task = _make_task()
    for fn in (export_to_json, export_to_dict):
        if fn is export_to_json:
            result = fn(task, [], None, 100)
        else:
            result = fn(task, [], None, 100)
    # HTML 也应能渲染空场景
    tpl = load_builtin_template("simple")
    html = export_to_html(task, [], "", 100, tpl)
    assert "未发现问题" in html

    payload = export_to_dict(task, [], None, 100)
    assert payload["statistics"]["total_issues"] == 0
    assert payload["statistics"]["fixed_count"] == 0
    assert payload["statistics"]["severity_count"]["严重"] == 0
    assert payload["statistics"]["severity_count"]["高"] == 0
    assert payload["issues"] == []
    assert payload["statistics"]["top_vulnerabilities"] == []


def test_single_issue_scenario():
    """单个问题场景应正确导出。"""
    issues = [_make_issue(id=7, title="单点问题", severity="严重")]
    result = export_to_dict(_make_task(), issues, "摘要", 40)
    assert result["statistics"]["total_issues"] == 1
    assert result["statistics"]["severity_count"]["严重"] == 1
    assert len(result["issues"]) == 1
    assert result["issues"][0]["id"] == 7


def test_multi_issue_mixed_scenario_sorts_by_severity():
    """多问题混合场景应按严重度排序(严重>高>中>低)。"""
    issues = [
        _make_issue(id=1, severity="低"),
        _make_issue(id=2, severity="严重"),
        _make_issue(id=3, severity="中"),
        _make_issue(id=4, severity="高"),
    ]
    result = export_to_dict(_make_task(), issues, "摘要", 50)
    severities = [it["severity"] for it in result["issues"]]
    assert severities == ["严重", "高", "中", "低"]


def test_build_report_context_includes_top_vulnerabilities_capped_at_10():
    """上下文中 top_vulnerabilities 最多 10 条。"""
    issues = [_make_issue(id=i, cvss_score=float(10 - i)) for i in range(1, 15)]
    ctx = _build_report_context(_make_task(), issues, "", 50)
    assert len(ctx["statistics"]["top_vulnerabilities"]) == 10
    # 第一条 cvss 最高
    assert ctx["statistics"]["top_vulnerabilities"][0]["cvss_score"] == 9.0


# ============ Pydantic 模型输入兼容性 ============

def test_export_to_html_with_issueout_pydantic_model():
    """export_to_html 应兼容 IssueOut Pydantic 模型输入。"""
    issue_dict = _make_issue()
    issue_out = IssueOut(**issue_dict)
    tpl = load_builtin_template("detailed")
    html = export_to_html(_make_task(), [issue_out], "摘要", 70, tpl)
    assert "SQL 注入漏洞" in html
    assert "参数化查询" in html


def test_export_to_dict_with_issueout_normalizes_fields():
    """dict 导出应正确归一化 IssueOut 模型字段。"""
    issue_out = IssueOut(**_make_issue())
    result = export_to_dict(_make_task(), [issue_out], None, 70)
    assert result["issues"][0]["cwe"] == "CWE-89"
    assert result["issues"][0]["cvss_score"] == 8.6
    assert isinstance(result["issues"][0]["compliance_mapping"], dict)


# ============ 内置模板载入 ============

def test_load_builtin_template_returns_content_for_each_type():
    """3 套内置模板均能载入且非空。"""
    for tpl_type in ("simple", "detailed", "compliance"):
        content = load_builtin_template(tpl_type)
        assert isinstance(content, str)
        assert len(content) > 0
        assert "<!DOCTYPE html>" in content


def test_load_builtin_template_invalid_type_raises():
    """不支持的模板类型应抛出 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        load_builtin_template("unknown_type")


# ============ JSON 序列化与 datetime ============

def test_json_serialization_handles_datetime():
    """JSON 导出应正确序列化 datetime 对象。"""
    task = _make_task(create_time=datetime(2026, 6, 25, 10, 0, 0))
    issues = [_make_issue(create_time=datetime(2026, 6, 25, 10, 5, 0))]
    raw = export_to_json(task, issues, "摘要", 70)
    import json
    payload = json.loads(raw)
    assert "2026-06-25T10:00:00" in payload["task_info"]["create_time"]
    assert "2026-06-25T10:05:00" in payload["issues"][0]["create_time"]


def test_export_to_dict_normalizes_references_json_to_list():
    """references_json 缺失时应归一化为空列表,避免模板渲染错误。"""
    issue = _make_issue(references_json=None)
    issue.pop("references_json", None)
    result = export_to_dict(_make_task(), [issue], None, 70)
    assert result["issues"][0]["references_json"] == []


def test_export_to_dict_normalizes_compliance_mapping_to_dict():
    """compliance_mapping 缺失时应归一化为空字典。"""
    issue = _make_issue()
    issue.pop("compliance_mapping", None)
    result = export_to_dict(_make_task(), [issue], None, 70)
    assert result["issues"][0]["compliance_mapping"] == {}


def test_export_to_html_all_three_templates_with_empty_issues():
    """3 套模板在空问题列表下均能成功渲染不报错。"""
    task = _make_task()
    for tpl_type in ("simple", "detailed", "compliance"):
        tpl = load_builtin_template(tpl_type)
        html = export_to_html(task, [], "", 90, tpl)
        assert "<!DOCTYPE html>" in html
        assert "未发现问题" in html or "暂无高危漏洞" in html
