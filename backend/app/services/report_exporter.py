"""报告导出服务模块 (T11)

提供 JSON / HTML / dict 三种导出形态,HTML 基于 Jinja2 渲染。配套 3 套内置
HTML 模板(simple/detailed/compliance),分别面向快速浏览、开发排查与合规审计。

依赖:
- T01 IssueOut / ReviewIssue: 全量漏洞元数据字段(owasp/cwe/cvss/compliance_mapping 等)
- T04 app.constants.compliance.build_compliance_summary: 4 套标准合规摘要聚合

上下文结构(由 _build_report_context 构建,供 Jinja2 模板渲染):
    {
        "task_info":   {id, task_name, project_name, review_type, status, ...},
        "summary":     str,           # AI 总体评价
        "score":       int,           # 综合评分 0-100
        "issues":      [issue_dict],  # 已按严重度排序的 issue 字典列表
        "statistics":  {
            "total_issues":       int,
            "fixed_count":        int,
            "severity_count":     {"严重": N, "高": N, "中": N, "低": N},
            "type_count":         {issue_type: N, ...},
            "cwe_count":          {cwe: N, ...},
            "compliance_summary": {iso27001/gdpr/pci_dss/hipaa: {total_findings, covered_*}},
            "top_vulnerabilities":[issue_dict],  # CVSS 最高的 Top N
        },
    }
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, select_autoescape

from app.constants.compliance import build_compliance_summary

# ============ 模块常量 ============

# 严重度排序权重: 严重 > 高 > 中 > 低(数字越小优先级越高)
_SEVERITY_ORDER: Dict[str, int] = {"严重": 0, "高": 1, "中": 2, "低": 3}
# 未知严重度排到最后
_DEFAULT_SEVERITY_WEIGHT: int = 99
# 4 个合规标准(与 T04 SUPPORTED_STANDARDS 保持一致)
_COMPLIANCE_STANDARDS: tuple = ("iso27001", "gdpr", "pci_dss", "hipaa")
# 标准中文名映射(模板展示用)
_STANDARD_LABELS: Dict[str, str] = {
    "iso27001": "ISO 27001",
    "gdpr": "GDPR",
    "pci_dss": "PCI-DSS",
    "hipaa": "HIPAA",
}
# 严重度顺序(用于分组结果保持固定顺序)
_SEVERITY_KEYS: tuple = ("严重", "高", "中", "低")

# Issue 已知字段列表(用于 ORM → dict 转换,与 IssueOut schema 对齐)
_ISSUE_FIELDS: tuple = (
    "id", "task_id", "file_id", "file_name", "line_number", "end_line",
    "issue_type", "severity", "title", "description", "suggestion",
    "fixed_code", "status", "create_time",
    "owasp", "cwe", "evidence", "exploit_scenario", "references_json",
    "confidence", "source", "cvss_score", "cvss_vector",
    "compliance_mapping", "remediation", "static_rule_hits",
)
# Task 已知字段列表(用于 ORM → dict 转换,与 ReviewTask ORM 对齐)
_TASK_FIELDS: tuple = (
    "id", "task_name", "project_id", "review_type", "status",
    "total_files", "processed_files", "total_issues",
    "severe_issues", "high_issues", "medium_issues", "low_issues",
    "score", "summary", "model_name", "duration_ms",
    "start_time", "end_time", "create_time",
)

# 模板目录路径(app/templates/)
_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"

# Jinja2 渲染环境(开启 HTML 自动转义,避免 XSS)
_JINJA_ENV: Environment = Environment(
    autoescape=select_autoescape(default=True, default_for_string=True),
    auto_reload=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


# ============ 内部归一化辅助 ============

def _to_serializable(value: Any) -> Any:
    """将值递归转换为 JSON 可序列化类型。

    处理 datetime → ISO 字符串、Path → 字符串,其余递归处理 dict/list。

    Args:
        value: 任意待转换值,可能是 datetime / Path / dict / list / 基础类型。

    Returns:
        Any: 转换后的 JSON 兼容值;datetime 转为 ISO 格式字符串,
             不可识别对象转为 str()。
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _to_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_serializable(v) for v in value]
    return value


def _normalize_issue(issue: Any) -> Dict[str, Any]:
    """将单个 issue 归一化为字典。

    兼容三种输入:Pydantic 模型(IssueOut)、ORM 模型(ReviewIssue)、原生 dict。
    归一化后所有字段均为 JSON 可序列化类型。

    Args:
        issue: 待归一化的 issue 对象,可为 IssueOut / ReviewIssue / dict。

    Returns:
        Dict[str, Any]: 包含 _ISSUE_FIELDS 全部字段的字典;缺失字段补 None,
            references_json 缺失补 None,compliance_mapping 缺失补 None。
    """
    if isinstance(issue, dict):
        raw = dict(issue)
    elif hasattr(issue, "model_dump"):
        # Pydantic v2 模型(IssueOut)
        raw = issue.model_dump(mode="json")
    else:
        # SQLAlchemy ORM 对象(ReviewIssue):按已知字段提取
        raw = {field: getattr(issue, field, None) for field in _ISSUE_FIELDS}
    # 统一字段默认值,避免模板渲染时 KeyError
    normalized: Dict[str, Any] = {}
    for field in _ISSUE_FIELDS:
        normalized[field] = _to_serializable(raw.get(field))
    # references_json / compliance_mapping 保持原值(可能为 None 或容器)
    if normalized.get("references_json") is None:
        normalized["references_json"] = []
    if normalized.get("compliance_mapping") is None:
        normalized["compliance_mapping"] = {}
    return normalized


def _normalize_task(task: Any) -> Dict[str, Any]:
    """将 task 归一化为字典。

    兼容三种输入:Pydantic 模型(TaskDetailOut/TaskOut)、ORM 模型(ReviewTask)、原生 dict。
    project_name 不在 ReviewTask ORM 上,缺失时补空字符串。

    Args:
        task: 待归一化的 task 对象,可为 TaskDetailOut / ReviewTask / dict。

    Returns:
        Dict[str, Any]: 包含 task 基础字段的字典;project_name 缺失补 "",
            所有 datetime 转为 ISO 字符串。
    """
    if isinstance(task, dict):
        raw = dict(task)
    elif hasattr(task, "model_dump"):
        raw = task.model_dump(mode="json")
    else:
        raw = {field: getattr(task, field, None) for field in _TASK_FIELDS}
    normalized: Dict[str, Any] = {}
    for field in _TASK_FIELDS:
        normalized[field] = _to_serializable(raw.get(field))
    # project_name 不在 ReviewTask ORM 上,需调用方传入或补空
    normalized["project_name"] = _to_serializable(raw.get("project_name", "")) or ""
    return normalized


def _severity_weight(severity: Optional[str]) -> int:
    """获取严重度排序权重。

    Args:
        severity: 严重度字符串(严重/高/中/低)。

    Returns:
        int: 排序权重,严重=0 / 高=1 / 中=2 / 低=3,未知=99。
    """
    if not severity:
        return _DEFAULT_SEVERITY_WEIGHT
    return _SEVERITY_ORDER.get(severity, _DEFAULT_SEVERITY_WEIGHT)


# ============ 分组与统计辅助 ============

def _group_issues_by_severity(issues: List[Any]) -> Dict[str, List[Dict[str, Any]]]:
    """按严重度分组 issue。

    分组顺序固定为 严重 > 高 > 中 > 低,每组内 issue 保持传入顺序。
    非 4 种标准严重度的 issue 归入 "其他" 组(置于最后)。

    Args:
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。

    Returns:
        Dict[str, List[Dict[str, Any]]]: 键为严重度(严重/高/中/低/其他),
            值为该严重度下的 issue 字典列表(已归一化)。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {key: [] for key in _SEVERITY_KEYS}
    grouped["其他"] = []
    for issue in issues or []:
        normalized = _normalize_issue(issue)
        severity = normalized.get("severity")
        if severity in grouped:
            grouped[severity].append(normalized)
        else:
            grouped["其他"].append(normalized)
    return grouped


def _group_issues_by_type(issues: List[Any]) -> Dict[str, List[Dict[str, Any]]]:
    """按 issue_type 分组 issue。

    分组顺序按 issue_type 字典序升序排列(便于稳定展示)。

    Args:
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。

    Returns:
        Dict[str, List[Dict[str, Any]]]: 键为 issue_type,值为该类型下的
            issue 字典列表(已归一化);键按字典序升序排列。
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues or []:
        normalized = _normalize_issue(issue)
        issue_type = normalized.get("issue_type") or "未分类"
        buckets.setdefault(issue_type, []).append(normalized)
    # 按 issue_type 字典序返回
    return {key: buckets[key] for key in sorted(buckets.keys())}


def _build_compliance_summary(issues: List[Any]) -> Dict[str, Dict[str, Any]]:
    """基于 issues 的 compliance_mapping 聚合 4 套标准合规摘要。

    委托 T04 app.constants.compliance.build_compliance_summary 实现:
    遍历 issues,统计每个标准命中的 issue 数(total_findings)与去重条款列表。
    若 issue.compliance_mapping 为空,尝试用 issue.cwe 自动补全映射。

    Args:
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。

    Returns:
        Dict[str, Dict[str, Any]]: 形如
            {
                "iso27001": {"total_findings": N, "covered_controls": [...]},
                "gdpr":      {"total_findings": N, "covered_articles": [...]},
                "pci_dss":   {"total_findings": N, "covered_requirements": [...]},
                "hipaa":     {"total_findings": N, "covered_sections": [...]},
            }
            空问题列表时 total_findings 均为 0,covered_* 均为空列表。
    """
    normalized = [_normalize_issue(issue) for issue in issues or []]
    return build_compliance_summary(normalized)


def _build_top_vulnerabilities(issues: List[Any], top_n: int = 10) -> List[Dict[str, Any]]:
    """取 CVSS 评分最高的 Top N 漏洞。

    按 cvss_score 降序排序,cvss_score 为 None 视为 -1 排到最后;
    同分时按 severity 严重度排序,再按 id 升序保持稳定。

    Args:
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。
        top_n: 返回的漏洞数量上限,默认 10。

    Returns:
        List[Dict[str, Any]]: 排序后的 issue 字典列表(已归一化),
            长度 min(top_n, len(issues));空列表返回 []。
    """
    if top_n <= 0 or not issues:
        return []
    normalized = [_normalize_issue(issue) for issue in issues]
    normalized.sort(
        key=lambda it: (
            -(it.get("cvss_score") if it.get("cvss_score") is not None else -1),
            _severity_weight(it.get("severity")),
            it.get("id") or 0,
        )
    )
    return normalized[:top_n]


def _build_report_context(
    task: Any,
    issues: List[Any],
    summary: Optional[str],
    score: int,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建 Jinja2 渲染上下文(同时用于 JSON / dict 导出)。

    聚合 task_info / summary / score / issues(已排序)/ statistics,
    其中 statistics 含 severity_count / type_count / cwe_count /
    compliance_summary / top_vulnerabilities。

    Args:
        task: 审查任务(TaskDetailOut / ReviewTask / dict)。
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。
        summary: AI 总体评价文本,可为空字符串。
        score: 综合评分 0-100。

    Returns:
        Dict[str, Any]: 渲染上下文字典,结构见模块文档字符串。
    """
    task_info = _normalize_task(task)
    normalized_issues = [_normalize_issue(issue) for issue in issues or []]

    # 按严重度排序(严重 > 高 > 中 > 低),同级别保持原顺序
    sorted_issues = sorted(
        normalized_issues,
        key=lambda it: (_severity_weight(it.get("severity")), it.get("id") or 0),
    )

    # 严重度计数
    severity_count: Dict[str, int] = {key: 0 for key in _SEVERITY_KEYS}
    severity_count["其他"] = 0
    # 类型计数
    type_count: Dict[str, int] = {}
    # CWE 计数
    cwe_count: Dict[str, int] = {}
    fixed_count = 0
    for item in normalized_issues:
        severity = item.get("severity")
        if severity in severity_count:
            severity_count[severity] += 1
        else:
            severity_count["其他"] += 1
        issue_type = item.get("issue_type") or "未分类"
        type_count[issue_type] = type_count.get(issue_type, 0) + 1
        cwe = item.get("cwe")
        if cwe:
            cwe_count[cwe] = cwe_count.get(cwe, 0) + 1
        if item.get("status") == "fixed":
            fixed_count += 1

    compliance_summary = _build_compliance_summary(normalized_issues)
    top_vulnerabilities = _build_top_vulnerabilities(normalized_issues, top_n=10)

    statistics: Dict[str, Any] = {
        "total_issues": len(normalized_issues),
        "fixed_count": fixed_count,
        "severity_count": severity_count,
        "type_count": type_count,
        "cwe_count": cwe_count,
        "compliance_summary": compliance_summary,
        "top_vulnerabilities": top_vulnerabilities,
        "standard_labels": dict(_STANDARD_LABELS),
    }

    return {
        "task_info": task_info,
        "summary": summary or "",
        "score": score,
        "issues": sorted_issues,
        "statistics": statistics,
        "evidence": _to_serializable(evidence or {}),
    }


# ============ 对外导出接口 ============

def export_to_dict(
    task: Any,
    issues: List[Any],
    summary: Optional[str],
    score: int,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """导出报告为字典(便于后续 PDF / Word 渲染消费)。

    Args:
        task: 审查任务(TaskDetailOut / ReviewTask / dict)。
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。
        summary: AI 总体评价文本,可为 None。
        score: 综合评分 0-100。

    Returns:
        Dict[str, Any]: 报告字典,包含 task_info / summary / score / issues / statistics
            五个顶层键;所有值均为 JSON 可序列化类型。
    """
    return _build_report_context(task, issues, summary, score, evidence)


def export_to_json(
    task: Any,
    issues: List[Any],
    summary: Optional[str],
    score: int,
    evidence: Optional[Dict[str, Any]] = None,
) -> str:
    """导出报告为 JSON 字符串。

    Args:
        task: 审查任务(TaskDetailOut / ReviewTask / dict)。
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。
        summary: AI 总体评价文本,可为 None。
        score: 综合评分 0-100。

    Returns:
        str: JSON 字符串,ensure_ascii=False 以正常显示中文,缩进 2 空格;
            结构包含 task_info / summary / score / issues / statistics。
    """
    payload = export_to_dict(task, issues, summary, score, evidence)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def export_to_html(
    task: Any,
    issues: List[Any],
    summary: Optional[str],
    score: int,
    template_content: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> str:
    """导出报告为 HTML 字符串(使用 Jinja2 渲染)。

    Args:
        task: 审查任务(TaskDetailOut / ReviewTask / dict)。
        issues: issue 对象列表(IssueOut / ReviewIssue / dict)。
        summary: AI 总体评价文本,可为 None。
        score: 综合评分 0-100。
        template_content: Jinja2 模板字符串(由调用方从文件或数据库载入)。

    Returns:
        str: 渲染后的 HTML 字符串;模板渲染异常时抛出 jinja2 异常。
    """
    context = _build_report_context(task, issues, summary, score, evidence)
    # 008 迁移预置的模板使用 task / metrics / compliance_summary，后续内置
    # HTML 模板改为 task_info / statistics。保留旧变量别名，确保历史数据库中
    # 已保存的模板和用户基于旧契约创建的模板仍可导出。
    severity_aliases = {
        "critical": {"严重", "critical"},
        "high": {"高", "high"},
        "medium": {"中", "medium"},
        "low": {"低", "low"},
        "info": {"信息", "info"},
    }
    legacy_severity_counts = {
        key: sum(
            1
            for issue in context["issues"]
            if str(issue.get("severity") or "").lower() in aliases
        )
        for key, aliases in severity_aliases.items()
    }
    context.update({
        "task": context["task_info"],
        "metrics": {
            "total_files": context["task_info"].get("total_files") or 0,
            "total_issues": context["statistics"]["total_issues"],
            "severity_counts": legacy_severity_counts,
        },
        "compliance_summary": context["statistics"]["compliance_summary"],
    })
    template = _JINJA_ENV.from_string(template_content)
    return template.render(**context)


def load_builtin_template(template_type: str) -> str:
    """载入内置 HTML 模板文件内容。

    3 套内置模板位于 app/templates/ 目录:
        - simple      → report_simple.html.j2
        - detailed    → report_detailed.html.j2
        - compliance  → report_compliance.html.j2

    Args:
        template_type: 模板类型(simple / detailed / compliance)。

    Returns:
        str: 模板文件文本内容。

    Raises:
        FileNotFoundError: 模板类型不支持或文件不存在。
    """
    file_map = {
        "simple": "report_simple.html.j2",
        "detailed": "report_detailed.html.j2",
        "compliance": "report_compliance.html.j2",
    }
    file_name = file_map.get(template_type)
    if not file_name:
        raise FileNotFoundError(f"不支持的内置模板类型: {template_type}")
    template_path = _TEMPLATES_DIR / file_name
    if not template_path.exists():
        raise FileNotFoundError(f"内置模板文件不存在: {template_path}")
    return template_path.read_text(encoding="utf-8")
