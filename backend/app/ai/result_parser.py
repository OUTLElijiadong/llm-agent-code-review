"""
审查结果解析模块: 解析LLM输出的JSON为结构化ReviewResult

v2 增强(2026-06-25):
- Issue 数据类新增漏洞元数据字段(owasp/cwe/evidence/exploit_scenario/references/confidence)
- 新增 _infer_owasp_cwe() 辅助函数,对未填 cwe 的安全类 issue 推断补全
- 向后兼容:旧格式 JSON(无新字段)仍能解析,用默认值填充

v3 增强(2026-06-25):
- Issue 数据类新增 CVSS/合规映射/修复方案字段(cvss_score/cvss_vector/compliance_mapping/remediation)
- compliance_mapping 由后端基于 cwe 反查 CWE_TO_COMPLIANCE 字典填充,LLM 无需输出
- 向后兼容:旧格式 JSON(无 v3 字段)仍能解析,用默认值填充
"""
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

from app.ai.cvss import normalize_cvss, normalize_cvss_vector
from app.ai.exceptions import ResultParseError
from app.constants.compliance import get_compliance_mapping

ALLOWED_TYPES = {
    "代码规范", "潜在Bug", "安全漏洞", "性能问题",
    "异常处理", "命名规范", "可维护性", "注释完整性", "其他",
}
ALLOWED_SEVERITY = {"严重", "高", "中", "低"}

_SEVERITY_ALIASES = {
    "critical": "严重",
    "crit": "严重",
    "危急": "严重",
    "致命": "严重",
    "严重": "严重",
    "high": "高",
    "高危": "高",
    "高": "高",
    "medium": "中",
    "moderate": "中",
    "warning": "中",
    "中危": "中",
    "中": "中",
    "low": "低",
    "info": "低",
    "informational": "低",
    "低危": "低",
    "低": "低",
    "p0": "严重",
    "p1": "高",
    "p2": "中",
    "p3": "低",
    "p4": "低",
    "error": "严重",
    "note": "低",
}

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def normalize_severity(value: object, default: str = "中") -> str:
    """把模型、协同 Agent 与静态引擎的严重度收敛为四级中文枚举。"""

    fallback = default if default in ALLOWED_SEVERITY else "中"
    text = str(value or "").strip()
    if not text:
        return fallback
    return _SEVERITY_ALIASES.get(text.lower(), _SEVERITY_ALIASES.get(text, fallback))


@dataclass
class Issue:
    """解析后的问题数据

    v2 新增字段(2026-06-25):
        owasp: OWASP 编号,如 A03:2021-Injection
        cwe: CWE 编号,如 CWE-89
        evidence: 漏洞证据代码片段(1-3 行)
        exploit_scenario: 攻击场景说明
        references: 参考链接列表
        confidence: 置信度 0.0-1.0

    v3 新增字段(2026-06-25):
        cvss_score: CVSS v3.1 基础分 0.0-10.0
        cvss_vector: CVSS v3.1 向量字符串,如 AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H
        compliance_mapping: 合规映射字典,由后端基于 cwe 反查填充
            {"iso27001":[...], "gdpr":[...], "pci_dss":[...], "hipaa":[...]}
        remediation: 详细修复方案文本(50-500 字)
        source: 发现来源(llm/static/regex/hybrid),由 issue_merger 填充
        static_rule_hits: 静态规则命中次数(双引擎统计,hybrid 时 ≥1)
    """
    line_number: int = 0
    end_line: Optional[int] = None
    issue_type: str = "其他"
    severity: str = "中"
    title: Optional[str] = None
    description: str = ""
    suggestion: Optional[str] = None
    fixed_code: Optional[str] = None
    # v2 新增漏洞元数据字段
    owasp: str = ""
    cwe: str = ""
    evidence: str = ""
    exploit_scenario: str = ""
    references: List[str] = field(default_factory=list)
    # 缺失值保持未知，避免把模型未声明的确定性伪造为 0.8。
    confidence: Optional[float] = None
    # v3 新增 CVSS / 合规映射 / 修复方案字段
    cvss_score: Optional[float] = None
    cvss_vector: Optional[str] = None
    cvss_version: Optional[str] = None
    cvss_source: str = "unavailable"
    compliance_mapping: Dict[str, List[str]] = field(default_factory=dict)
    remediation: str = ""
    # v3 新增来源标识与静态命中统计(由 issue_merger 填充,默认 llm/0 向后兼容)
    source: str = "llm"
    static_rule_hits: int = 0
    source_details: List[dict] = field(default_factory=list)
    confirmation_count: int = 1
    finding_fingerprint: str = ""
    # 稳定的源码调用点；用于跨 Agent/分片归并，不依赖模型行号。
    source_anchor: str = ""
    column_start: Optional[int] = None
    column_end: Optional[int] = None
    aggregation_version: str = ""
    evidence_quality: str = ""
    conflict_status: str = "none"
    human_review_status: str = "not_required"
    risk_score: Optional[float] = None
    aggregation: Dict[str, object] = field(default_factory=dict)


@dataclass
class ReviewResult:
    """解析后的审查结果"""
    summary: str = ""
    score: int = 0
    issues: List[Issue] = field(default_factory=list)
    diagnostics: List["ParseDiagnostic"] = field(default_factory=list)
    input_issue_count: int = 0
    invalid_issue_count: int = 0


@dataclass(frozen=True)
class ParseDiagnostic:
    """单个问题条目的可恢复解析诊断。"""

    code: str
    message: str
    index: int


def _strip_fence(text: str) -> str:
    """去除Markdown围栏,提取JSON内容

    Args:
        text: 包含可能的```json...```围栏的文本

    Returns:
        str: 提取出的纯JSON文本
    """
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text


def _coerce_int(v, default: int = 0) -> int:
    """安全转换为整数

    Args:
        v: 待转换值
        default: 转换失败时的默认值

    Returns:
        int: 转换后的整数
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce_float(v, default: Optional[float] = None) -> Optional[float]:
    """安全转换为浮点数

    Args:
        v: 待转换值
        default: 转换失败时的默认值

    Returns:
        Optional[float]: 转换后的浮点数
    """
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_cvss_score(v) -> Optional[float]:
    """Parse a CVSS score without converting missing data to zero.

    Args:
        v: 待转换值(LLM 输出的原始字段)

    Returns:
        Optional[float]: 0.0-10.0 之间的浮点数;缺失或非法时返回 None
    """
    score, _, _, _ = normalize_cvss(v, None)
    return score


def _coerce_cvss_vector(v) -> Optional[str]:
    """安全解析 CVSS v3.1 向量字符串,做最小合法性校验

    Args:
        v: 待转换值(LLM 输出的原始字段)

    Returns:
        Optional[str]: 合法的 CVSS 向量字符串;非法或空则返回 None
    """
    return normalize_cvss_vector(v)


def _coerce_remediation(v) -> str:
    """安全解析修复方案文本,去除首尾空白并限制最大长度

    Args:
        v: 待转换值(LLM 输出的原始字段)

    Returns:
        str: 规范化后的修复方案文本;空值返回空字符串
    """
    if not v:
        return ""
    return str(v).strip()[:2000]


def _build_compliance_mapping(cwe: str) -> Dict[str, List[str]]:
    """基于 CWE 编号反查 4 大合规标准映射

    Args:
        cwe: CWE 编号,如 CWE-89

    Returns:
        Dict[str, List[str]]: 合规映射字典
            {"iso27001":[...], "gdpr":[...], "pci_dss":[...], "hipaa":[...]}
            未匹配到 cwe 或所有标准均为空时返回空字典 {}
    """
    if not cwe:
        return {}
    try:
        mapping = get_compliance_mapping(cwe)
        if not mapping:
            return {}
        result = {
            "iso27001": mapping.get("iso27001", []) or [],
            "gdpr": mapping.get("gdpr", []) or [],
            "pci_dss": mapping.get("pci_dss", []) or [],
            "hipaa": mapping.get("hipaa", []) or [],
        }
        # 所有标准都为空列表时视为未命中,返回空字典
        if not any(result.values()):
            return {}
        return result
    except Exception:
        # 合规反查失败不应阻断解析主流程
        return {}


def _coerce_references(v) -> List[str]:
    """安全转换为参考链接列表

    Args:
        v: 待转换值(可能是 list 或字符串)

    Returns:
        List[str]: 字符串列表
    """
    if not v:
        return []
    if isinstance(v, list):
        return [str(item) for item in v if item]
    if isinstance(v, str):
        return [v]
    return []


def _infer_owasp_cwe(title: str, description: str) -> Tuple[str, str]:
    """基于关键词推断 OWASP/CWE 编号(无 LLM 调用)

    当 LLM 返回的安全类 issue 缺失 cwe 字段时,用关键词匹配补全。
    规则集与 SecuritySentinelAgent._infer_owasp_cwe 保持一致。

    Args:
        title: 问题标题
        description: 问题描述

    Returns:
        Tuple[str, str]: (owasp, cwe) 元组;未匹配则返回 ("", "")
    """
    text = f"{title} {description}".lower()
    rules: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
        (("sql 注入", "sql注入", "sql injection"),
         "A03:2021-Injection", "CWE-89"),
        (("命令注入", "command injection"),
         "A03:2021-Injection", "CWE-78"),
        (("xss", "跨站脚本"),
         "A03:2021-Injection", "CWE-79"),
        (("ssrf", "服务端请求伪造"),
         "A10:2021-Server-Side Request Forgery", "CWE-918"),
        (("csrf", "跨站请求伪造"),
         "A01:2021-Broken Access Control", "CWE-352"),
        (("反序列化", "deserialization"),
         "A08:2021-Software and Data Integrity Failures", "CWE-502"),
        (("路径遍历", "path traversal", "directory traversal"),
         "A01:2021-Broken Access Control", "CWE-22"),
        (("越权", "idor", "broken access"),
         "A01:2021-Broken Access Control", "CWE-639"),
        (("硬编码", "hardcoded", "明文密码"),
         "A07:2021-Identification and Authentication Failures", "CWE-798"),
        (("弱加密", "md5", "sha1", "des", "ecb"),
         "A02:2021-Cryptographic Failures", "CWE-327"),
        (("jwt"), "A07:2021-Identification and Authentication Failures", "CWE-522"),
    )
    for keywords, owasp, cwe in rules:
        if isinstance(keywords, str):
            keywords = (keywords,)
        if any(k in text for k in keywords):
            return owasp, cwe
    return "", ""


def _normalize_issue(raw: dict) -> Issue:
    """规范化单个问题条目,处理非法枚举值与字段补全

    Args:
        raw: 原始问题字典

    Returns:
        Issue: 规范化后的问题对象
    """
    issue_type = raw.get("issue_type") or "其他"
    if issue_type not in ALLOWED_TYPES:
        issue_type = "其他"
    severity = normalize_severity(raw.get("severity"))

    title = (raw.get("title") or "")[:200] or None
    description = str(raw.get("description") or "")
    suggestion = str(raw.get("suggestion") or "") or None
    fixed_code = str(raw.get("fixed_code") or "") or None

    # 解析 v2 新增字段
    owasp = str(raw.get("owasp") or "")
    cwe = str(raw.get("cwe") or "")
    evidence = str(raw.get("evidence") or "")
    exploit_scenario = str(raw.get("exploit_scenario") or "")
    references = _coerce_references(raw.get("references"))
    confidence = _coerce_float(raw.get("confidence"), None)
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    # 解析 v3 新增字段
    cvss_score, cvss_vector, cvss_version, cvss_source = normalize_cvss(
        raw.get("cvss_score"),
        raw.get("cvss_vector"),
    )
    remediation = _coerce_remediation(raw.get("remediation"))

    # 安全类 issue 缺 cwe 时,基于 title/description 推断补全
    if issue_type == "安全漏洞" and not cwe:
        inferred_owasp, inferred_cwe = _infer_owasp_cwe(title or "", description)
        if not owasp:
            owasp = inferred_owasp
        if not cwe:
            cwe = inferred_cwe

    # 基于 cwe 反查 4 大合规标准映射(LLM 不输出 compliance_mapping)
    compliance_mapping = _build_compliance_mapping(cwe) if cwe else {}

    return Issue(
        line_number=_coerce_int(raw.get("line_number"), 0),
        end_line=_coerce_int(raw.get("end_line"), 0) or None,
        issue_type=issue_type,
        severity=severity,
        title=title,
        description=description,
        suggestion=suggestion,
        fixed_code=fixed_code,
        owasp=owasp,
        cwe=cwe,
        evidence=evidence,
        exploit_scenario=exploit_scenario,
        references=references,
        confidence=confidence,
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping=compliance_mapping,
        remediation=remediation,
    )


def parse(text: str) -> ReviewResult:
    """解析LLM输出的JSON文本为ReviewResult

    Args:
        text: LLM返回的原始文本

    Returns:
        ReviewResult: 标准化审查结果

    Raises:
        ResultParseError: JSON解析失败或格式异常
    """
    if not text or not text.strip():
        raise ResultParseError("AI 返回为空")

    cleaned = _strip_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON 解析失败: {e}, 原始片段: {cleaned[:200]}")
        raise ResultParseError(f"AI 返回非合法 JSON: {e}")

    if not isinstance(data, dict):
        raise ResultParseError("AI 返回不是 JSON 对象")

    issues_raw = data.get("issues")
    if not isinstance(issues_raw, list):
        raise ResultParseError("AI 返回的 issues 必须是数组")

    issues: List[Issue] = []
    diagnostics: List[ParseDiagnostic] = []
    for index, raw in enumerate(issues_raw):
        if not isinstance(raw, dict):
            diagnostics.append(ParseDiagnostic(
                code="issue_not_object",
                message="问题条目必须是 JSON 对象",
                index=index,
            ))
            continue
        if not any(str(raw.get(key) or "").strip() for key in ("title", "description", "evidence")):
            diagnostics.append(ParseDiagnostic(
                code="issue_missing_identity",
                message="问题条目缺少 title、description 或 evidence",
                index=index,
            ))
            continue
        raw_severity = str(raw.get("severity") or "").strip()
        if raw_severity and raw_severity.lower() not in _SEVERITY_ALIASES and raw_severity not in ALLOWED_SEVERITY:
            diagnostics.append(ParseDiagnostic(
                code="issue_invalid_severity",
                message=f"无法识别的严重度: {raw_severity[:40]}",
                index=index,
            ))
            # v1 旧调用可能仅提供 description，没有标题和结构化严重度。
            # 为保证主流程可继续，仅对这类旧格式使用中等级回退，同时保留诊断。
            if (
                not str(raw.get("title") or "").strip()
                and not str(raw.get("evidence") or "").strip()
                and str(raw.get("issue_type") or "") == "其他"
            ):
                legacy_raw = dict(raw)
                legacy_raw["severity"] = "中"
                issues.append(_normalize_issue(legacy_raw))
            continue
        issues.append(_normalize_issue(raw))

    if issues_raw and not issues:
        raise ResultParseError("AI 返回的 issues 全部为无效条目")

    return ReviewResult(
        summary=str(data.get("summary") or "")[:2000],
        score=max(0, min(100, _coerce_int(data.get("score"), 0))),
        issues=issues,
        diagnostics=diagnostics,
        input_issue_count=len(issues_raw),
        invalid_issue_count=len(diagnostics),
    )
