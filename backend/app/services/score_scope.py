"""按问题集评分的报告（代码审查、安全审计、圆桌讨论）的评分范围判定。

规则：分数始终由已复核问题集按 severity-deduction-v1 得出；覆盖范围只决定
分数的“适用范围”，不再决定是否隐藏分数。
- complete：执行成功且覆盖完整，分数代表整份提交；
- partial：只完成了部分文件 / 发言，分数只代表已审范围，报告必须列出未覆盖部分；
- historical：早期任务没有覆盖账本，按已保存问题集复算；
- 只有“没有任何已审文件或有效发言”时才不出分，并给出原因，绝不用 0 占位。
"""

from __future__ import annotations

from typing import Any

from app.ai.scoring import compute_score_breakdown

_COMPLETE = {"complete", "completed", "verified"}
_REPORTABLE = {"success", "partial", "failed", "cancelled", "expired", "stopped"}
SCOPE_LABELS = {
    "complete": "完整覆盖",
    "partial": "仅覆盖已审范围",
    "historical": "历史任务按已保存问题集复算",
}
NO_SCOPE_REASON = "没有任何已完成审查的文件或有效发言，按规则不出分"
HISTORICAL_NO_FILES_REASON = "历史任务既无覆盖记录也无提交文件，无法确认评分依据，不出分"


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def issue_score_scope(task: Any) -> dict[str, Any]:
    """返回 ``{"available", "scope", "label", "reason", "reviewed_files", "total_files"}``。"""
    coverage = task.coverage if isinstance(getattr(task, "coverage", None), dict) else {}
    status = str(getattr(task, "status", "") or "")
    total_files = _int(getattr(task, "total_files", 0))
    reviewed_files = max(_int(coverage.get("completed_files")), _int(getattr(task, "processed_files", 0)))
    base = {"reviewed_files": reviewed_files, "total_files": total_files}

    def result(scope: str | None, reason: str | None = None) -> dict[str, Any]:
        return {
            **base,
            "available": scope is not None,
            "scope": scope,
            "label": SCOPE_LABELS.get(scope or "", "不出分"),
            "reason": reason,
        }

    if status not in _REPORTABLE:
        return result(None, "任务尚未结束，评分在终态后生成")
    if coverage.get("score_unavailable_reason") == "ungraded_findings":
        return result(None, "存在未分级问题，无法按严重度规则计分")
    coverage_status = str(coverage.get("coverage_status") or "").strip().lower()
    verification_status = str(coverage.get("verification_status") or "complete").strip().lower()
    if status == "success" and coverage_status in _COMPLETE and verification_status in _COMPLETE:
        if coverage.get("score_available") is False:
            # 完整执行却显式声明不可评分（如评分来源回执缺失），保留该结论。
            return result(None, str(coverage.get("score_unavailable_reason") or "评分来源回执声明不可评分"))
        return result("complete")
    has_speeches = _int(coverage.get("valid_speeches")) > 0 and coverage.get("extraction_status") == "success"
    if reviewed_files > 0 or has_speeches or _int(getattr(task, "total_issues", 0)) > 0:
        if not coverage and status == "success":
            return result("historical")
        return result("partial")
    if not coverage and status == "success" and total_files > 0:
        # 早期成功任务没有覆盖账本，但提交过文件：按已保存问题集复算并标注历史口径。
        return result("historical")
    if not coverage and status == "success":
        return result(None, HISTORICAL_NO_FILES_REASON)
    return result(None, NO_SCOPE_REASON)


def issue_rule_score(task: Any) -> int:
    """按任务已记录的四级问题计数复算 severity-deduction-v1。"""
    counts = {
        "严重": _int(getattr(task, "severe_issues", 0)),
        "高": _int(getattr(task, "high_issues", 0)),
        "中": _int(getattr(task, "medium_issues", 0)),
        "低": _int(getattr(task, "low_issues", 0)),
    }
    return int(compute_score_breakdown(counts)["score"])


def stored_score_is_rule_result(task: Any) -> bool:
    """只有带评分版本的记录分才是规则产物；旧失败任务的 0 是占位值。"""
    return bool(getattr(task, "score_version", None)) and getattr(task, "score", None) is not None
