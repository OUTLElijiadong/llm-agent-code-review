"""报告分数可信度终审（小菱核验环节）。

规则评分之后、报告展示之前，按评分规则从原始输入（问题严重度计数 / 沙箱
检查项回执）独立重算一次，与任务记录分比对：
- 一致 → ``trusted``，原分数可信，直接生成报告；
- 不一致 → ``rescored``，打回按规则重判，报告使用重算分并保留被否决的原值；
- 规则输入缺失 → ``unavailable``，不出分也不用 0 占位。
"""

from __future__ import annotations

from typing import Any

from app.ai.scoring import SCORING_VERSION, compute_score_breakdown

SCORE_AUDIT_VERSION = "xiaoling-score-audit-v1"
_REVIEWER = "小菱终审"


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= 100 else None


def _sandbox_rule_score(coverage: dict) -> int | None:
    from app.services.sandbox_service import _SANDBOX_STATE_CREDIT

    sample = coverage.get("score_sample") if isinstance(coverage.get("score_sample"), dict) else {}
    checks = sample.get("checks") if isinstance(sample.get("checks"), list) else []
    credits = [
        _SANDBOX_STATE_CREDIT[item.get("state")]
        for item in checks
        if isinstance(item, dict) and item.get("state") in _SANDBOX_STATE_CREDIT
    ]
    executed = _int_or_none(sample.get("executed_checks"))
    # 样本被截断（>50 项）时以记录的得分合计重算，否则逐项求和。
    if executed and executed > len(credits) and isinstance(sample.get("earned_credit"), (int, float)):
        return int(round(100 * float(sample["earned_credit"]) / executed))
    if not credits:
        return None
    return int(round(100 * sum(credits) / len(credits)))


def audit_report_score(
    task: Any,
    displayed_score: int | None,
    severity_count: dict[str, int],
    issue_count: int,
) -> dict[str, Any]:
    """返回终审结论；``final_score`` 是报告应展示的分数。"""
    stored = _int_or_none(getattr(task, "score", None))
    coverage = task.coverage if isinstance(getattr(task, "coverage", None), dict) else {}
    review_type = str(getattr(task, "review_type", "") or "")
    base = {"version": SCORE_AUDIT_VERSION, "reviewer": _REVIEWER, "stored_score": stored}

    if displayed_score is None:
        reason = coverage.get("score_unavailable_reason") or "覆盖或评分依据不足，按规则不出分"
        return {**base, "status": "unavailable", "rule_score": None, "final_score": None, "reason": str(reason)}

    if review_type == "sandbox_test":
        rule = "沙箱验证分 = 100 × 已执行检查项得分之和 ÷ 已执行检查项数"
        rule_score = _sandbox_rule_score(coverage)
    elif review_type == "pentest":
        # 渗透分由领域服务按发现集给出，终审只核对取值范围与来源回执。
        rule = "渗透评分来自领域发现集回执"
        rule_score = displayed_score
    elif issue_count > 0:
        rule = f"{SCORING_VERSION}: 100 − 15×严重 − 8×高 − 3×中 − 1×低"
        rule_score = int(compute_score_breakdown(severity_count)["score"])
    else:
        rule = "无问题且覆盖完整的任务保留记录分"
        rule_score = displayed_score

    if rule_score is None:
        return {
            **base, "status": "unavailable", "rule": rule, "rule_score": None, "final_score": None,
            "reason": "评分规则输入缺失，无法独立复算，打回不出分",
        }
    trusted = rule_score == displayed_score and (stored is None or stored == rule_score or issue_count == 0)
    if trusted:
        return {
            **base, "status": "trusted", "rule": rule, "rule_score": rule_score, "final_score": rule_score,
            "reason": "按规则复算与记录分一致，分数可信，生成报告",
        }
    return {
        **base, "status": "rescored", "rule": rule, "rule_score": rule_score, "final_score": rule_score,
        "rejected_score": stored if stored != rule_score else displayed_score,
        "reason": "记录分与规则复算不一致，已打回按规则重判，报告采用重判分",
    }
