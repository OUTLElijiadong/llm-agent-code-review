"""
审查结果解析模块: 解析LLM输出的JSON为结构化ReviewResult
"""
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from app.ai.exceptions import ResultParseError

ALLOWED_TYPES = {
    "代码规范", "潜在Bug", "安全漏洞", "性能问题",
    "异常处理", "命名规范", "可维护性", "注释完整性", "其他",
}
ALLOWED_SEVERITY = {"严重", "高", "中", "低"}

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


@dataclass
class Issue:
    """解析后的问题数据"""
    line_number: int = 0
    end_line: Optional[int] = None
    issue_type: str = "其他"
    severity: str = "中"
    title: Optional[str] = None
    description: str = ""
    suggestion: Optional[str] = None
    fixed_code: Optional[str] = None


@dataclass
class ReviewResult:
    """解析后的审查结果"""
    summary: str = ""
    score: int = 0
    issues: list[Issue] = field(default_factory=list)


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


def _normalize_issue(raw: dict) -> Issue:
    """规范化单个问题条目,处理非法枚举值

    Args:
        raw: 原始问题字典

    Returns:
        Issue: 规范化后的问题对象
    """
    issue_type = raw.get("issue_type") or "其他"
    if issue_type not in ALLOWED_TYPES:
        issue_type = "其他"
    severity = raw.get("severity") or "中"
    if severity not in ALLOWED_SEVERITY:
        severity = "中"
    return Issue(
        line_number=_coerce_int(raw.get("line_number"), 0),
        end_line=_coerce_int(raw.get("end_line"), 0) or None,
        issue_type=issue_type,
        severity=severity,
        title=(raw.get("title") or "")[:200] or None,
        description=str(raw.get("description") or ""),
        suggestion=str(raw.get("suggestion") or "") or None,
        fixed_code=str(raw.get("fixed_code") or "") or None,
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

    issues_raw = data.get("issues") or []
    if not isinstance(issues_raw, list):
        issues_raw = []

    return ReviewResult(
        summary=str(data.get("summary") or "")[:2000],
        score=max(0, min(100, _coerce_int(data.get("score"), 0))),
        issues=[_normalize_issue(it) for it in issues_raw if isinstance(it, dict)],
    )
