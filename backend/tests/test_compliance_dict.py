"""单元测试 (T04): 合规条款字典模块

覆盖 4 套合规标准字典、CWE → 合规反向映射,以及对外查询函数的
正常流程、边界条件与异常情况。
"""
from app.constants.compliance import (
    CWE_TO_COMPLIANCE,
    GDPR_ARTICLES,
    HIPAA_SECTIONS,
    ISO_27001_CONTROLS,
    PCI_DSS_REQUIREMENTS,
    SUPPORTED_STANDARDS,
    build_compliance_summary,
    get_compliance_mapping,
    list_controls,
    lookup_control,
)

# ============ 字典导入与完整性 ============

def test_iso_27001_controls_importable_and_non_empty():
    """ISO 27001 控制字典可导入且非空,且至少含安全开发相关 15 条"""
    assert ISO_27001_CONTROLS, "ISO_27001_CONTROLS 不应为空"
    # 抽样校验关键字段存在
    for code in ("A.8.25", "A.8.28", "A.8.29", "A.5.17"):
        assert code in ISO_27001_CONTROLS, f"缺少关键控制 {code}"
    assert len(ISO_27001_CONTROLS) >= 15


def test_gdpr_articles_importable_and_non_empty():
    """GDPR 条款字典可导入且非空"""
    assert GDPR_ARTICLES, "GDPR_ARTICLES 不应为空"
    for code in ("Art.5", "Art.25", "Art.32", "Art.44"):
        assert code in GDPR_ARTICLES, f"缺少关键条款 {code}"
    assert len(GDPR_ARTICLES) >= 10


def test_pci_dss_requirements_importable_and_non_empty():
    """PCI-DSS 要求字典可导入且非空"""
    assert PCI_DSS_REQUIREMENTS, "PCI_DSS_REQUIREMENTS 不应为空"
    for code in ("Req-6.2.4", "Req-6.4.1", "Req-8.1", "Req-10.1"):
        assert code in PCI_DSS_REQUIREMENTS, f"缺少关键要求 {code}"
    assert len(PCI_DSS_REQUIREMENTS) >= 12


def test_hipaa_sections_importable_and_non_empty():
    """HIPAA 条款字典可导入且非空"""
    assert HIPAA_SECTIONS, "HIPAA_SECTIONS 不应为空"
    for code in ("§164.308", "§164.312(a)", "§164.312(e)(2)(ii)"):
        assert code in HIPAA_SECTIONS, f"缺少关键条款 {code}"
    assert len(HIPAA_SECTIONS) >= 8


def test_cwe_to_compliance_importable_and_non_empty():
    """CWE 反向映射字典可导入且非空,至少覆盖 10 个 OWASP Top 10 CWE"""
    assert CWE_TO_COMPLIANCE, "CWE_TO_COMPLIANCE 不应为空"
    for cwe in ("CWE-22", "CWE-78", "CWE-79", "CWE-89", "CWE-200",
                "CWE-327", "CWE-502", "CWE-798", "CWE-862", "CWE-918"):
        assert cwe in CWE_TO_COMPLIANCE, f"缺少关键 CWE 映射 {cwe}"
    assert len(CWE_TO_COMPLIANCE) >= 10


# ============ get_compliance_mapping ============

def test_get_compliance_mapping_cwe_89_returns_all_standards_non_empty():
    """CWE-89 命中后 4 个标准均应返回非空条款列表"""
    mapping = get_compliance_mapping("CWE-89")
    assert set(mapping.keys()) == set(SUPPORTED_STANDARDS)
    for std in SUPPORTED_STANDARDS:
        assert mapping[std], f"标准 {std} 映射不应为空"


def test_get_compliance_mapping_case_insensitive():
    """小写 cwe-89 应与 CWE-89 返回一致结果"""
    lower = get_compliance_mapping("cwe-89")
    upper = get_compliance_mapping("CWE-89")
    assert lower == upper


def test_get_compliance_mapping_unknown_cwe_returns_empty_lists():
    """未命中 CWE 应返回 4 个标准均为空列表的字典"""
    mapping = get_compliance_mapping("CWE-99999")
    assert set(mapping.keys()) == set(SUPPORTED_STANDARDS)
    for std in SUPPORTED_STANDARDS:
        assert mapping[std] == [], f"标准 {std} 应为空列表"


def test_get_compliance_mapping_returns_copy_not_reference():
    """返回值应为副本,修改不影响内部字典"""
    mapping = get_compliance_mapping("CWE-89")
    mapping["iso27001"].append("FAKE-001")
    fresh = get_compliance_mapping("CWE-89")
    assert "FAKE-001" not in fresh["iso27001"]


# ============ lookup_control ============

def test_lookup_control_iso27001_known_code():
    """查询 ISO 27001 已知条款应返回非 None"""
    control = lookup_control("iso27001", "A.8.25")
    assert control is not None
    assert control.code == "A.8.25"
    assert control.title
    assert control.category == "安全开发"


def test_lookup_control_unknown_standard_returns_none():
    """未知标准查询应返回 None"""
    assert lookup_control("unknown", "X") is None


def test_lookup_control_unknown_code_returns_none():
    """已知标准但未知条款应返回 None"""
    assert lookup_control("iso27001", "A.99.99") is None


def test_lookup_control_all_standards():
    """4 套标准各抽样一条条款均应可查询到"""
    cases = [
        ("iso27001", "A.8.28"),
        ("gdpr", "Art.32"),
        ("pci_dss", "Req-6.2.4"),
        ("hipaa", "§164.312(b)"),
    ]
    for std, code in cases:
        control = lookup_control(std, code)
        assert control is not None, f"{std}/{code} 应可查询到"
        assert control.code == code


# ============ list_controls ============

def test_list_controls_gdpr_returns_non_empty():
    """列出 GDPR 全部条款应返回非空字典"""
    controls = list_controls("gdpr")
    assert controls, "gdpr 条款列表不应为空"
    assert all(hasattr(c, "code") for c in controls.values())


def test_list_controls_unknown_standard_returns_empty():
    """未知标准应返回空字典"""
    assert list_controls("unknown") == {}


def test_list_controls_returns_copy():
    """返回值应为副本,修改不影响内部字典"""
    controls = list_controls("iso27001")
    controls["FAKE"] = None  # type: ignore[assignment]
    fresh = list_controls("iso27001")
    assert "FAKE" not in fresh


# ============ build_compliance_summary ============

def test_build_compliance_summary_empty_issues():
    """空 issue 列表应返回 4 标准空汇总"""
    summary = build_compliance_summary([])
    assert set(summary.keys()) == set(SUPPORTED_STANDARDS)
    for std in SUPPORTED_STANDARDS:
        assert summary[std]["total_findings"] == 0
        # covered_* 字段应为空列表
        covered_field = {
            "iso27001": "covered_controls",
            "gdpr": "covered_articles",
            "pci_dss": "covered_requirements",
            "hipaa": "covered_sections",
        }[std]
        assert summary[std][covered_field] == []


def test_build_compliance_summary_auto_fills_missing_mapping():
    """compliance_mapping 为空时按 cwe 自动补全映射"""
    issues = [{"cwe": "CWE-89", "compliance_mapping": {}}]
    summary = build_compliance_summary(issues)
    # CWE-89 命中 4 个标准,total_findings 均应为 1
    for std in SUPPORTED_STANDARDS:
        assert summary[std]["total_findings"] == 1, f"{std} 应计 1 次"
    # 抽样: iso27001 应包含 A.8.25
    assert "A.8.25" in summary["iso27001"]["covered_controls"]


def test_build_compliance_summary_uses_provided_mapping():
    """提供 compliance_mapping 时直接使用,正确统计"""
    issues = [{
        "cwe": "CWE-89",
        "compliance_mapping": {"iso27001": ["A.8.25"]},
    }]
    summary = build_compliance_summary(issues)
    # iso27001 命中 1 次,且仅 A.8.25
    assert summary["iso27001"]["total_findings"] == 1
    assert summary["iso27001"]["covered_controls"] == ["A.8.25"]
    # 其余标准未在 mapping 中提供,应计 0
    for std in ("gdpr", "pci_dss", "hipaa"):
        assert summary[std]["total_findings"] == 0


def test_build_compliance_summary_deduplicates_codes():
    """同一标准多个 issue 命中相同条款应去重"""
    issues = [
        {"cwe": "CWE-89", "compliance_mapping": {"iso27001": ["A.8.25", "A.8.28"]}},
        {"cwe": "CWE-79", "compliance_mapping": {"iso27001": ["A.8.25", "A.8.29"]}},
    ]
    summary = build_compliance_summary(issues)
    controls = summary["iso27001"]["covered_controls"]
    assert controls == ["A.8.25", "A.8.28", "A.8.29"]
    assert summary["iso27001"]["total_findings"] == 2


def test_build_compliance_summary_covered_field_names_correct():
    """各标准 covered_* 字段名应正确"""
    issues = [{
        "cwe": "CWE-89",
        "compliance_mapping": {
            "iso27001": ["A.8.25"],
            "gdpr": ["Art.32"],
            "pci_dss": ["Req-6.4.1"],
            "hipaa": ["§164.312(a)"],
        },
    }]
    summary = build_compliance_summary(issues)
    assert summary["iso27001"]["covered_controls"] == ["A.8.25"]
    assert summary["gdpr"]["covered_articles"] == ["Art.32"]
    assert summary["pci_dss"]["covered_requirements"] == ["Req-6.4.1"]
    assert summary["hipaa"]["covered_sections"] == ["§164.312(a)"]


def test_build_compliance_summary_none_issues_returns_empty():
    """传入 None 应等价于空列表"""
    summary = build_compliance_summary(None)  # type: ignore[arg-type]
    assert set(summary.keys()) == set(SUPPORTED_STANDARDS)
    for std in SUPPORTED_STANDARDS:
        assert summary[std]["total_findings"] == 0


def test_build_compliance_summary_issue_without_cwe_skipped():
    """issue 缺少 cwe 且 mapping 为空时应被跳过,不报错"""
    issues = [{"compliance_mapping": {}}]
    summary = build_compliance_summary(issues)
    for std in SUPPORTED_STANDARDS:
        assert summary[std]["total_findings"] == 0
