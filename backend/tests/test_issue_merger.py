"""单元测试:issue_merger 双引擎合并去重工具(T08)

验证静态分析(Finding)与 LLM 审查(Issue)结果的合并去重逻辑:
- 仅静态 Findings 转换为 Issue(source="static")
- 仅 LLM Issues 保留(source="llm")
- 同问题命中(static+llm)合并为 hybrid,static_rule_hits=1
- 行号接近(±2)且 cwe 相同视为重复
- 行号相同但 cwe 不同视为不同问题
- 置信度取较高者
- v3 字段(cvss/compliance/remediation)正确合并
- 空输入场景
"""
from __future__ import annotations

from app.ai.result_parser import Issue
from app.ai.static_analyzer import Finding
from app.services.issue_merger import (
    finding_to_issue,
    merge_findings_and_issues,
)


# ============ 辅助函数 ============

def _make_finding(
    *,
    line_number: int = 10,
    end_line: int = 10,
    cwe: str = "CWE-89",
    severity: str = "高",
    title: str = "SQL 注入",
    confidence: float = 0.95,
    source: str = "static",
    cvss_score: float = 7.5,
    cvss_vector: str = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    compliance_mapping: dict = None,
    remediation: str = "使用参数化查询",
    static_rule_hits: int = 1,
    owasp: str = "A03:2021-Injection",
) -> Finding:
    """构造 Finding 对象

    Args:
        line_number: 起始行号
        end_line: 结束行号
        cwe: CWE 编号
        severity: 严重程度
        title: 问题标题
        confidence: 置信度
        source: 发现来源
        cvss_score: CVSS 基础分
        cvss_vector: CVSS 向量
        compliance_mapping: 合规映射
        remediation: 修复方案
        static_rule_hits: 静态命中次数
        owasp: OWASP 编号

    Returns:
        Finding: 标准化漏洞发现
    """
    return Finding(
        line_number=line_number,
        end_line=end_line,
        issue_type="安全漏洞",
        severity=severity,
        title=title,
        description=f"第 {line_number} 行检测到 {title}",
        suggestion="请修复此问题",
        fixed_code="",
        owasp=owasp,
        cwe=cwe,
        evidence=f"evidence at line {line_number}",
        exploit_scenario="攻击者可利用此漏洞",
        references=["https://cwe.mitre.org/"],
        confidence=confidence,
        source=source,
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        compliance_mapping=compliance_mapping or {},
        remediation=remediation,
        static_rule_hits=static_rule_hits,
    )


def _make_issue(
    *,
    line_number: int = 10,
    end_line: int = 10,
    cwe: str = "CWE-89",
    severity: str = "高",
    title: str = "SQL 注入漏洞",
    confidence: float = 0.85,
    cvss_score: float = 8.0,
    cvss_vector: str = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    compliance_mapping: dict = None,
    remediation: str = "使用 ORM 替代裸 SQL",
    source: str = "llm",
    static_rule_hits: int = 0,
    owasp: str = "A03:2021-Injection",
) -> Issue:
    """构造 Issue 对象

    Args:
        line_number: 起始行号
        end_line: 结束行号
        cwe: CWE 编号
        severity: 严重程度
        title: 问题标题
        confidence: 置信度
        cvss_score: CVSS 基础分
        cvss_vector: CVSS 向量
        compliance_mapping: 合规映射
        remediation: 修复方案
        source: 发现来源
        static_rule_hits: 静态命中次数
        owasp: OWASP 编号

    Returns:
        Issue: 解析后的问题对象
    """
    return Issue(
        line_number=line_number,
        end_line=end_line,
        issue_type="安全漏洞",
        severity=severity,
        title=title,
        description=f"第 {line_number} 行存在 {title}",
        suggestion="请修复",
        fixed_code="",
        owasp=owasp,
        cwe=cwe,
        evidence=f"llm evidence at line {line_number}",
        exploit_scenario="攻击者可注入恶意 SQL",
        references=["https://owasp.org/"],
        confidence=confidence,
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        compliance_mapping=compliance_mapping or {},
        remediation=remediation,
        source=source,
        static_rule_hits=static_rule_hits,
    )


# ============ 空输入场景 ============

class TestEmptyInputs:
    """空输入场景测试"""

    def test_both_empty_returns_empty(self):
        """两个引擎都无结果时应返回空列表"""
        result = merge_findings_and_issues([], [], file_id=1)
        assert result == []

    def test_only_findings_empty(self):
        """findings 为空时,LLM issues 应原样返回"""
        issues = [_make_issue(line_number=5, cwe="CWE-79")]
        result = merge_findings_and_issues([], issues, file_id=1)
        assert len(result) == 1
        assert result[0].source == "llm"

    def test_only_issues_empty(self):
        """issues 为空时,static findings 应转为 Issue 返回"""
        findings = [_make_finding(line_number=5, cwe="CWE-79")]
        result = merge_findings_and_issues(findings, [], file_id=1)
        assert len(result) == 1
        assert result[0].source == "static"


# ============ 仅静态 / 仅 LLM 场景 ============

class TestSingleEngineOnly:
    """仅一个引擎命中的场景"""

    def test_only_static_findings_converted_to_issues(self):
        """仅静态命中:Finding 转为 Issue,source="static" """
        findings = [
            _make_finding(line_number=10, cwe="CWE-89"),
            _make_finding(line_number=20, cwe="CWE-79"),
        ]
        result = merge_findings_and_issues(findings, [], file_id=1)
        assert len(result) == 2
        assert all(r.source == "static" for r in result)
        assert all(r.static_rule_hits >= 1 for r in result)

    def test_only_llm_issues_preserved(self):
        """仅 LLM 命中:Issue 保留,source="llm" """
        issues = [
            _make_issue(line_number=10, cwe="CWE-89"),
            _make_issue(line_number=20, cwe="CWE-79"),
        ]
        result = merge_findings_and_issues([], issues, file_id=1)
        assert len(result) == 2
        assert all(r.source == "llm" for r in result)
        assert all(r.static_rule_hits == 0 for r in result)

    def test_static_confidence_preserved(self):
        """仅静态命中:confidence 保留 Finding 的值"""
        findings = [_make_finding(confidence=0.99)]
        result = merge_findings_and_issues(findings, [], file_id=1)
        assert result[0].confidence == 0.99

    def test_llm_confidence_preserved(self):
        """仅 LLM 命中:confidence 保留 Issue 的值"""
        issues = [_make_issue(confidence=0.7)]
        result = merge_findings_and_issues([], issues, file_id=1)
        assert result[0].confidence == 0.7


# ============ 双引擎命中(hybrid)场景 ============

class TestHybridMerge:
    """双引擎同时命中同一问题的合并测试"""

    def test_same_line_same_cwe_merges_to_hybrid(self):
        """行号相同 + cwe 相同 → 合并为 hybrid"""
        findings = [_make_finding(line_number=10, cwe="CWE-89")]
        issues = [_make_issue(line_number=10, cwe="CWE-89")]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert result[0].source == "hybrid"
        assert result[0].static_rule_hits == 2  # static=1 + LLM确认+1

    def test_line_within_proximity_merges(self):
        """行号差 ±2 内 + cwe 相同 → 合并"""
        findings = [_make_finding(line_number=10, cwe="CWE-89")]
        issues = [_make_issue(line_number=12, cwe="CWE-89")]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert result[0].source == "hybrid"

    def test_line_outside_proximity_no_merge(self):
        """行号差 > 2 + cwe 相同 → 不合并(两个独立问题)"""
        findings = [_make_finding(line_number=10, cwe="CWE-89")]
        issues = [_make_issue(line_number=15, cwe="CWE-89")]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 2

    def test_same_line_different_cwe_no_merge(self):
        """行号相同 + cwe 不同 → 不合并(不同问题)"""
        findings = [_make_finding(line_number=10, cwe="CWE-89")]
        issues = [_make_issue(line_number=10, cwe="CWE-79")]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 2
        sources = {r.source for r in result}
        assert sources == {"static", "llm"}

    def test_hybrid_takes_higher_confidence(self):
        """hybrid 合并时 confidence 取较高者"""
        findings = [_make_finding(confidence=0.99)]
        issues = [_make_issue(confidence=0.7)]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert result[0].confidence == 0.99

    def test_hybrid_takes_higher_confidence_llm_wins(self):
        """hybrid 合并时 LLM confidence 更高则取 LLM 的"""
        findings = [_make_finding(confidence=0.7)]
        issues = [_make_issue(confidence=0.95)]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert result[0].confidence == 0.95

    def test_hybrid_static_rule_hits_increments(self):
        """hybrid 合并后 static_rule_hits = static原值 + 1"""
        findings = [_make_finding(static_rule_hits=1)]
        issues = [_make_issue()]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert result[0].static_rule_hits == 2


# ============ v3 字段合并测试 ============

class TestV3FieldsMerge:
    """v3 字段(cvss/compliance/remediation)合并测试"""

    def test_hybrid_v3_fields_prefer_llm(self):
        """hybrid 合并: v3 字段优先取 LLM,LLM 为空时回退 static"""
        findings = [_make_finding(
            cvss_score=5.0,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N",
            remediation="static 修复方案",
            compliance_mapping={"iso27001": ["A.8"]},
        )]
        issues = [_make_issue(
            cvss_score=9.8,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            remediation="llm 修复方案",
            compliance_mapping={"gdpr": ["Art.32"]},
        )]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert result[0].cvss_score == 9.8
        assert result[0].cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        assert result[0].remediation == "llm 修复方案"
        assert result[0].compliance_mapping == {"gdpr": ["Art.32"]}

    def test_hybrid_v3_fallback_to_static_when_llm_empty(self):
        """hybrid 合并: LLM v3 字段为空时回退 static"""
        findings = [_make_finding(
            cvss_score=7.5,
            remediation="static 修复",
            compliance_mapping={"pci_dss": ["6.5.1"]},
        )]
        issues = [_make_issue(
            cvss_score=0.0,
            cvss_vector="",
            remediation="",
            compliance_mapping={},
        )]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert result[0].cvss_score == 7.5
        assert result[0].remediation == "static 修复"
        assert result[0].compliance_mapping == {"pci_dss": ["6.5.1"]}

    def test_static_v3_fields_preserved(self):
        """仅静态命中: v3 字段从 Finding 保留"""
        findings = [_make_finding(
            cvss_score=9.8,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            remediation="详细修复",
            compliance_mapping={"hipaa": ["164.308"]},
        )]
        result = merge_findings_and_issues(findings, [], file_id=1)
        assert len(result) == 1
        assert result[0].cvss_score == 9.8
        assert result[0].cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        assert result[0].remediation == "详细修复"
        assert result[0].compliance_mapping == {"hipaa": ["164.308"]}


# ============ 边界条件 ============

class TestEdgeCases:
    """边界条件测试"""

    def test_empty_cwe_does_not_match(self):
        """cwe 为空的 Finding/Issue 不参与匹配"""
        findings = [_make_finding(cwe="")]
        issues = [_make_issue(cwe="")]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 2  # 不合并,各自独立

    def test_zero_line_number_does_not_match(self):
        """line_number=0(文件级问题)不参与行号匹配"""
        findings = [_make_finding(line_number=0, cwe="CWE-89")]
        issues = [_make_issue(line_number=0, cwe="CWE-89")]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 2  # 不合并

    def test_multiple_findings_multiple_issues_mixed(self):
        """多个 Findings + 多个 Issues 混合场景"""
        findings = [
            _make_finding(line_number=10, cwe="CWE-89"),   # 与 issue[0] 合并
            _make_finding(line_number=30, cwe="CWE-79"),   # 独立 static
            _make_finding(line_number=50, cwe="CWE-22"),   # 独立 static
        ]
        issues = [
            _make_issue(line_number=11, cwe="CWE-89"),     # 与 finding[0] 合并(hybrid)
            _make_issue(line_number=40, cwe="CWE-352"),    # 独立 llm
        ]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        # 期望:1 hybrid + 2 static(独立) + 1 llm(独立) = 4
        assert len(result) == 4
        sources = [r.source for r in result]
        assert sources.count("hybrid") == 1
        assert sources.count("static") == 2
        assert sources.count("llm") == 1

    def test_finding_to_issue_preserves_all_fields(self):
        """finding_to_issue 公开函数保留全量字段"""
        finding = _make_finding(
            line_number=42,
            cwe="CWE-502",
            severity="严重",
            title="反序列化漏洞",
            confidence=0.98,
            cvss_score=9.8,
            cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            compliance_mapping={"iso27001": ["A.8.2"]},
            remediation="禁用 pickle",
            static_rule_hits=3,
        )
        issue = finding_to_issue(finding)
        assert issue.line_number == 42
        assert issue.cwe == "CWE-502"
        assert issue.severity == "严重"
        assert issue.title == "反序列化漏洞"
        assert issue.confidence == 0.98
        assert issue.cvss_score == 9.8
        assert issue.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
        assert issue.compliance_mapping == {"iso27001": ["A.8.2"]}
        assert issue.remediation == "禁用 pickle"
        assert issue.static_rule_hits == 3
        assert issue.source == "static"

    def test_cwe_case_insensitive_match(self):
        """cwe 大小写不敏感匹配"""
        findings = [_make_finding(cwe="cwe-89")]
        issues = [_make_issue(cwe="CWE-89")]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert result[0].source == "hybrid"

    def test_references_preserved_in_hybrid(self):
        """hybrid 合并后 references 列表保留(LLM 优先)"""
        findings = [_make_finding()]
        issues = [_make_issue()]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        assert len(result) == 1
        assert len(result[0].references) >= 1

    def test_one_finding_matches_multiple_llm_only_first(self):
        """一个 static Finding 只能匹配第一个 LLM Issue(不重复匹配)"""
        findings = [_make_finding(line_number=10, cwe="CWE-89")]
        issues = [
            _make_issue(line_number=10, cwe="CWE-89"),
            _make_issue(line_number=11, cwe="CWE-89"),
        ]
        result = merge_findings_and_issues(findings, issues, file_id=1)
        # 第一个 LLM issue 匹配 static → hybrid
        # 第二个 LLM issue 无匹配 → llm
        # static 已被匹配,不再独立出现
        assert len(result) == 2
        sources = [r.source for r in result]
        assert "hybrid" in sources
        assert "llm" in sources
