"""反馈聚合服务 — Agent 自进化 L0

把躺在 review_issue 里的处理结果(fixed=真阳性 / ignored=疑似假阳性)
聚合成可消费的学习信号:按问题类型(进而映射到规则类型)统计采纳率、
假阳性率、样本量,并给出「跨任务/跨用户」去偏计数用于防翻车双门槛。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.review_issue import ReviewIssue

# 问题类型(中文枚举,见 result_parser.ALLOWED_TYPES) → 规则类型(review_rule.rule_type)
ISSUE_TYPE_TO_RULE_TYPE: dict[str, str] = {
    "代码规范": "style",
    "命名规范": "style",
    "潜在Bug": "correctness",
    "安全漏洞": "security",
    "性能问题": "performance",
    "异常处理": "robustness",
    "可维护性": "maintainability",
    "注释完整性": "documentation",
    "其他": "other",
}

# 已决状态:进入学习信号的两类
_DECIDED = ("fixed", "ignored")


def _cutoff(window_days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=window_days)


def aggregate_by_issue_type(db: Session, window_days: int = 90) -> list[dict]:
    """按问题类型聚合反馈信号

    仅统计已决(fixed/ignored)且在时间窗内处理的问题,实现分布漂移的
    滑动窗口治理。采纳率/假阳性率以「已决数」为分母,避免未处理问题稀释信号。

    Args:
        db: 数据库会话
        window_days: 滑动窗口天数,只看近 N 天处理的反馈

    Returns:
        list[dict]: 每个问题类型一项,字段:
            issue_type / rule_type / fixed / ignored / decided /
            acceptance_rate / false_positive_rate /
            distinct_tasks / distinct_ignored_tasks / distinct_ignored_users
        按假阳性率降序排列,便于优先发现噪声规则。
    """
    cutoff = _cutoff(window_days)
    rows = (
        db.query(
            ReviewIssue.issue_type,
            ReviewIssue.status,
            ReviewIssue.task_id,
            ReviewIssue.handled_by,
        )
        .filter(
            ReviewIssue.status.in_(_DECIDED),
            ReviewIssue.handled_at.isnot(None),
            ReviewIssue.handled_at >= cutoff,
        )
        .all()
    )

    buckets: dict[str, dict] = {}
    for issue_type, status, task_id, handled_by in rows:
        b = buckets.setdefault(issue_type or "其他", {
            "fixed": 0, "ignored": 0,
            "tasks": set(), "ignored_tasks": set(), "ignored_users": set(),
        })
        b["tasks"].add(task_id)
        if status == "fixed":
            b["fixed"] += 1
        elif status == "ignored":
            b["ignored"] += 1
            b["ignored_tasks"].add(task_id)
            if handled_by is not None:
                b["ignored_users"].add(handled_by)

    result: list[dict] = []
    for issue_type, b in buckets.items():
        decided = b["fixed"] + b["ignored"]
        result.append({
            "issue_type": issue_type,
            "rule_type": ISSUE_TYPE_TO_RULE_TYPE.get(issue_type, "other"),
            "fixed": b["fixed"],
            "ignored": b["ignored"],
            "decided": decided,
            "acceptance_rate": round(b["fixed"] / decided, 4) if decided else 0.0,
            "false_positive_rate": round(b["ignored"] / decided, 4) if decided else 0.0,
            "distinct_tasks": len(b["tasks"]),
            "distinct_ignored_tasks": len(b["ignored_tasks"]),
            "distinct_ignored_users": len(b["ignored_users"]),
        })
    result.sort(key=lambda x: x["false_positive_rate"], reverse=True)
    return result


def summary(db: Session, window_days: int = 90) -> dict:
    """反馈信号总览 — 供进化中心看板与 EvolutionAgent 消费

    Returns:
        dict: window_days / total_fixed / total_ignored / total_decided /
              overall_acceptance_rate / overall_false_positive_rate / by_issue_type
    """
    by_type = aggregate_by_issue_type(db, window_days)
    total_fixed = sum(t["fixed"] for t in by_type)
    total_ignored = sum(t["ignored"] for t in by_type)
    total_decided = total_fixed + total_ignored
    return {
        "window_days": window_days,
        "total_fixed": total_fixed,
        "total_ignored": total_ignored,
        "total_decided": total_decided,
        "overall_acceptance_rate": (
            round(total_fixed / total_decided, 4) if total_decided else 0.0
        ),
        "overall_false_positive_rate": (
            round(total_ignored / total_decided, 4) if total_decided else 0.0
        ),
        "by_issue_type": by_type,
    }
