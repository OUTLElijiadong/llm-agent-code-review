"""8 维度评分：由已保存的问题集（类型 x 严重度）在读取时派生。

报告详情页的「8 维度评分雷达」读取 ``stats.dim_scores``，但标准审查（review_service）和圆桌收尾
（discussion_orchestrator._finalize_review）都从未写出过这个字段，所以雷达在所有非领域报告上都显示
「暂无 8 维度数据」。本模块给出两条路径共用的唯一算法：

* 维度归属沿用前端 ``constants/dim.ts`` 既有的 issue_type -> 维度映射（含历史同义写法）；
* 维度分与整体严重度扣分、逐文件分使用同一条规则：``100 − 15×严重 − 8×高 − 3×中 − 1×低``，
  只统计该维度已记录且未被证伪的问题，不含 CVSS 加权；
* 没有记录问题的维度是「无数据」，既不是 0 分也不是满分；真实扣到 0 的维度保留 0；
* 存在未分级问题的维度同样「无数据」，与整体评分遇到未分级问题时不出分的规则一致。

只在读取时由 ReviewIssue 派生，不回写数据库，不需要迁移，历史已完成的任务（含圆桌）读取时即可得到。
各维度分是类别层面的质量提示，不能相加或平均得到整体评分。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.ai.scoring import DEDUCT, compute_score_breakdown

DIMENSION_SCORE_VERSION = "dimension-severity-deduction-v1"

DIMENSION_KEYS: tuple[str, ...] = (
    "style", "naming", "comment", "maintain", "performance", "exception", "logic", "security",
)
DIMENSION_LABELS: dict[str, str] = {
    "style": "代码规范",
    "naming": "命名规范",
    "comment": "注释完整性",
    "maintain": "可维护性",
    "performance": "性能问题",
    "exception": "异常处理",
    "logic": "潜在 Bug",
    "security": "安全漏洞",
}
# 必须与 frontend/src/constants/dim.ts 的 DIM_KEYS + DIM_ALIAS 完全一致（有测试逐项比对）。
ISSUE_TYPE_TO_DIMENSION: dict[str, str] = {
    **{key: key for key in DIMENSION_KEYS},
    "maintainability": "maintain",
    "bug": "logic",
    "perf": "performance",
    "代码规范": "style",
    "命名规范": "naming",
    "注释完整性": "comment",
    "可维护性": "maintain",
    "性能问题": "performance",
    "异常处理": "exception",
    "潜在Bug": "logic",
    "安全漏洞": "security",
}
DIMENSION_BASIS = (
    "各维度分按该维度已记录且未证伪问题的严重度扣分：100 − 15×严重 − 8×高 − 3×中 − 1×低"
    "（与逐文件分同一规则，不含 CVSS 加权）。没有记录问题的维度为“无数据”，不是 0 分也不是满分；"
    "各维度分不能相加或平均得到整体评分。"
)
_NO_ISSUES_REASON = "本任务没有记录该维度的问题，缺少评分依据（不代表满分）"
_UNGRADED_REASON = "该维度存在未分级问题，无法按严重度规则计分"
_SEVERITIES: tuple[str, ...] = tuple(DEDUCT)


def dimension_of(issue_type: object) -> str | None:
    """把问题类型归到 8 个维度之一；“其他”等不在维度内的类型返回 None。"""
    return ISSUE_TYPE_TO_DIMENSION.get(str(issue_type or "").strip())


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def compute_dimension_scores(
    rows: Iterable[tuple[Any, Any, Any]],
    *,
    available: bool = True,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """由 ``(issue_type, severity, count)`` 汇总行派生 8 维度评分。

    Args:
        rows: 已排除证伪问题的汇总行；count 小于等于 0 的行被忽略。
        available: 整体评分是否可用；不可用时所有维度都标为无数据并写明原因。
        unavailable_reason: 整体评分不可用的原因，写入每个维度的说明。

    Returns:
        含 ``items``（固定 8 项，顺序同 ``DIMENSION_KEYS``）、``scores``（只含有数据的维度，
        真实 0 分保留）、``scored_count`` / ``no_data_count`` / ``unmapped_issue_count`` 的字典。
    """
    counts: dict[str, dict[str, int]] = {key: dict.fromkeys(_SEVERITIES, 0) for key in DIMENSION_KEYS}
    ungraded: dict[str, int] = dict.fromkeys(DIMENSION_KEYS, 0)
    unmapped = 0
    for issue_type, severity, raw_count in rows:
        count = _count(raw_count)
        if count <= 0:
            continue
        key = dimension_of(issue_type)
        if key is None:
            unmapped += count
        elif str(severity or "").strip() in DEDUCT:
            counts[key][str(severity).strip()] += count
        else:
            ungraded[key] += count

    reason_when_hidden = str(unavailable_reason or "").strip() or "整体评分不可用，维度评分不展示"
    items: list[dict[str, Any]] = []
    scores: dict[str, int] = {}
    for key in DIMENSION_KEYS:
        severity_counts = counts[key]
        issue_count = sum(severity_counts.values()) + ungraded[key]
        item: dict[str, Any] = {
            "key": key,
            "label": DIMENSION_LABELS[key],
            "score": None,
            "status": "no_data",
            "issue_count": issue_count,
            "severity_counts": dict(severity_counts),
            "total_deduction": 0,
            "reason": "",
        }
        if not available:
            item["reason"] = reason_when_hidden
        elif ungraded[key]:
            item["reason"] = _UNGRADED_REASON
        elif issue_count == 0:
            item["reason"] = _NO_ISSUES_REASON
        else:
            breakdown = compute_score_breakdown(severity_counts)
            item.update(score=int(breakdown["score"]), status="scored",
                        total_deduction=int(breakdown["total_deduction"]))
            scores[key] = item["score"]
        items.append(item)

    return {
        "version": DIMENSION_SCORE_VERSION,
        "basis": DIMENSION_BASIS,
        "available": bool(available),
        "items": items,
        "scores": scores,
        "scored_count": len(scores),
        "no_data_count": len(DIMENSION_KEYS) - len(scores),
        "unmapped_issue_count": unmapped,
    }
