"""Issue 合并去重工具(T08 双引擎核心组件)

将静态分析引擎(Finding)与 LLM 审查引擎(Issue)的结果合并去重,
生成统一的 Issue 列表供 ReviewIssue 持久化使用。

去重规则:
    file_id + line_number(±2 行内) + cwe 相同视为同一问题

合并策略:
    - 静态+LLM 同时命中: source="hybrid", static_rule_hits += 1, confidence 取较高者
    - 仅静态命中: source 保留 Finding 原值(regex/static), confidence=Finding.confidence
    - 仅 LLM 命中: source="llm", confidence=Issue.confidence

设计要点:
    1. 纯函数,无 DB 写入,无 LLM 调用,可独立测试
    2. 不修改输入列表(返回新列表)
    3. 合并后的 Issue 携带全量 v3 字段(cvss_score/cvss_vector/compliance_mapping/
       remediation/static_rule_hits),可直接转换为 ReviewIssue ORM
"""
from typing import List, Optional

from app.ai.result_parser import Issue
from app.ai.static_analyzer import Finding

# 行号邻近阈值:±2 行内视为同一位置
_LINE_PROXIMITY = 2


def merge_findings_and_issues(
    findings: List[Finding],
    issues: List[Issue],
    file_id: int,
) -> List[Issue]:
    """合并静态分析结果与 LLM 审查结果,去重后返回 Issue 列表

    将静态引擎的 Finding 列表与 LLM 引擎的 Issue 列表合并,
    基于 (file_id, line_number ±2, cwe) 三元组去重。
    同时被两个引擎命中时合并为 hybrid 问题,置信度取较高者,
    static_rule_hits +1;仅被一个引擎命中时保留原来源标记。

    Args:
        findings: 静态分析引擎产出的 Finding 列表(可能为空)
        issues: LLM 审查引擎产出的 Issue 列表(可能为空)
        file_id: 当前文件 ID(用于上下文标识,不写入 Issue 本身)

    Returns:
        List[Issue]: 合并去重后的 Issue 列表,每个 Issue 的 source 和
                     static_rule_hits 字段已正确填充,可直接持久化到 ReviewIssue
    """
    # 1. 将静态 Finding 转换为 Issue(source="static")
    static_issues: List[Issue] = [finding_to_issue(f) for f in findings]

    # 2. 确保 LLM Issues 的 source 默认为 "llm"(parse 时已设默认值,这里做保护)
    llm_issues: List[Issue] = []
    for it in issues:
        if not it.source or it.source == "llm":
            llm_issues.append(it)
        else:
            llm_issues.append(it)

    # 3. 匹配合并:对每个 LLM Issue,寻找行号邻近且 cwe 相同的 static Issue
    matched_static_indices: set[int] = set()
    merged: List[Issue] = []

    for llm_issue in llm_issues:
        match_idx = _find_match(llm_issue, static_issues, matched_static_indices)
        if match_idx is not None:
            matched_static_indices.add(match_idx)
            merged.append(_merge_pair(static_issues[match_idx], llm_issue))
        else:
            # 仅 LLM 命中:保留原 Issue,确保 source="llm"
            merged.append(_ensure_source(llm_issue, "llm"))

    # 4. 未被匹配的 static Issues 作为仅静态命中加入结果
    for idx, static_issue in enumerate(static_issues):
        if idx not in matched_static_indices:
            merged.append(static_issue)

    return merged


def finding_to_issue(finding: Finding) -> Issue:
    """将 Finding 数据类转换为 Issue 数据类(保留全量 v3 字段)

    Finding 与 Issue 字段对齐,此函数做 1:1 映射。
    source 保留 Finding 原值(regex/static/llm),为空时回退到 "static";
    static_rule_hits 保留 Finding 原值(至少为 1)。
    此函数为公开接口,供 review_service 将 LLM 路径的 Finding 转换为 Issue 后参与合并。

    Args:
        finding: 静态分析或 LLM 审查产出的标准化漏洞发现

    Returns:
        Issue: 转换后的问题对象,source 保留原值(默认 "static"),携带全量 v3 字段
    """
    return Issue(
        line_number=finding.line_number,
        end_line=finding.end_line,
        issue_type=finding.issue_type,
        severity=finding.severity,
        title=finding.title or None,
        description=finding.description,
        suggestion=finding.suggestion or None,
        fixed_code=finding.fixed_code or None,
        owasp=finding.owasp,
        cwe=finding.cwe,
        evidence=finding.evidence,
        exploit_scenario=finding.exploit_scenario,
        references=list(finding.references) if finding.references else [],
        confidence=finding.confidence,
        cvss_score=finding.cvss_score,
        cvss_vector=finding.cvss_vector,
        compliance_mapping=dict(finding.compliance_mapping) if finding.compliance_mapping else {},
        remediation=finding.remediation,
        source=finding.source or "static",
        static_rule_hits=finding.static_rule_hits,
    )


def _find_match(
    llm_issue: Issue,
    static_issues: List[Issue],
    excluded: Optional[set] = None,
) -> Optional[int]:
    """为 LLM Issue 寻找行号邻近且 cwe 相同的 static Issue 索引

    匹配条件(全部满足):
        1. cwe 非空且相同(空 cwe 不参与匹配,避免误合并)
        2. line_number 差值 ≤ _LINE_PROXIMITY(±2 行)
        3. 该 static Issue 索引不在 excluded 集合中(避免重复匹配)

    Args:
        llm_issue: LLM 审查产出的问题
        static_issues: 静态分析产出的问题列表
        excluded: 已匹配的 static_issues 索引集合(跳过这些索引)

    Returns:
        Optional[int]: 匹配到的 static_issues 索引;未匹配返回 None
    """
    llm_cwe = (llm_issue.cwe or "").strip().upper()
    if not llm_cwe:
        return None

    llm_line = llm_issue.line_number or 0
    if llm_line == 0:
        # 行号为 0(文件级问题)不参与行号邻近匹配
        return None

    excluded_set = excluded or set()
    for idx, static_issue in enumerate(static_issues):
        if idx in excluded_set:
            continue
        static_cwe = (static_issue.cwe or "").strip().upper()
        if static_cwe != llm_cwe:
            continue
        static_line = static_issue.line_number or 0
        if static_line == 0:
            continue
        if abs(static_line - llm_line) <= _LINE_PROXIMITY:
            return idx

    return None


def _merge_pair(static_issue: Issue, llm_issue: Issue) -> Issue:
    """合并同一问题(静态+LLM 双引擎命中)为 hybrid Issue

    合并策略:
        - source = "hybrid"
        - static_rule_hits = static_issue.static_rule_hits + 1(静态命中 + LLM 确认)
        - confidence = max(static, llm)(取较高者)
        - 其余字段优先取 LLM(LLM 描述更丰富),LLM 为空时回退到 static

    Args:
        static_issue: 静态分析产出的问题(已转换为 Issue)
        llm_issue: LLM 审查产出的问题

    Returns:
        Issue: 合并后的问题对象,source="hybrid"
    """
    merged_confidence = max(static_issue.confidence, llm_issue.confidence)
    merged_hits = static_issue.static_rule_hits + 1

    # v3 字段优先取 LLM(LLM 通常给出更详细的 CVSS/合规/修复方案),为空时回退 static
    merged_cvss_score = llm_issue.cvss_score or static_issue.cvss_score
    merged_cvss_vector = llm_issue.cvss_vector or static_issue.cvss_vector
    merged_compliance = llm_issue.compliance_mapping or static_issue.compliance_mapping
    merged_remediation = llm_issue.remediation or static_issue.remediation

    return Issue(
        line_number=llm_issue.line_number or static_issue.line_number,
        end_line=llm_issue.end_line or static_issue.end_line,
        issue_type=llm_issue.issue_type or static_issue.issue_type,
        severity=llm_issue.severity or static_issue.severity,
        title=llm_issue.title or static_issue.title,
        description=llm_issue.description or static_issue.description,
        suggestion=llm_issue.suggestion or static_issue.suggestion,
        fixed_code=llm_issue.fixed_code or static_issue.fixed_code,
        owasp=llm_issue.owasp or static_issue.owasp,
        cwe=llm_issue.cwe or static_issue.cwe,
        evidence=llm_issue.evidence or static_issue.evidence,
        exploit_scenario=llm_issue.exploit_scenario or static_issue.exploit_scenario,
        references=llm_issue.references or static_issue.references,
        confidence=merged_confidence,
        cvss_score=merged_cvss_score,
        cvss_vector=merged_cvss_vector,
        compliance_mapping=merged_compliance,
        remediation=merged_remediation,
        source="hybrid",
        static_rule_hits=merged_hits,
    )


def _ensure_source(issue: Issue, source: str) -> Issue:
    """确保 Issue 的 source 字段为指定值(不修改原对象)

    Args:
        issue: 原始问题对象
        source: 期望的来源标识

    Returns:
        Issue: source 已设置为指定值的新 Issue 对象
    """
    if issue.source == source:
        return issue
    return Issue(
        line_number=issue.line_number,
        end_line=issue.end_line,
        issue_type=issue.issue_type,
        severity=issue.severity,
        title=issue.title,
        description=issue.description,
        suggestion=issue.suggestion,
        fixed_code=issue.fixed_code,
        owasp=issue.owasp,
        cwe=issue.cwe,
        evidence=issue.evidence,
        exploit_scenario=issue.exploit_scenario,
        references=issue.references,
        confidence=issue.confidence,
        cvss_score=issue.cvss_score,
        cvss_vector=issue.cvss_vector,
        compliance_mapping=issue.compliance_mapping,
        remediation=issue.remediation,
        source=source,
        static_rule_hits=issue.static_rule_hits,
    )
