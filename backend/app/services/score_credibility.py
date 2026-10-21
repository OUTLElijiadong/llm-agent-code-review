"""报告分数规则复算；不表示对问题真实性做独立复核。

规则评分之后、报告展示之前，按评分规则从原始输入（问题严重度计数 / 沙箱
检查项回执）独立重算一次，与任务记录分比对：
- 一致 → ``trusted``，仅表示算术复算一致；
- 不一致 → ``rescored``，报告使用重算分并保留原值；
- 规则输入缺失 → ``unavailable``，不出分也不用 0 占位。
"""

from __future__ import annotations

from typing import Any

from app.ai.scoring import SCORING_VERSION, compute_score_breakdown

SCORE_AUDIT_VERSION = "xiaoling-score-audit-v1"
_REVIEWER = "规则复算"


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


def review_type_of(task: Any) -> str:
    return str(getattr(task, "review_type", "") or "")


def _scope_note(scope: dict[str, Any]) -> str:
    if scope.get("scope") == "partial":
        return "；分数仅代表已审范围，未覆盖部分见覆盖记录"
    if scope.get("scope") == "historical":
        return "；历史任务无覆盖账本，按已保存问题集复算"
    return ""


def _pentest_rule_score(coverage: dict, displayed_score: int | None) -> int | None:
    from app.services.pentest_service import _SEVERITY_DEDUCTION

    counts = coverage.get("confirmed_counts") if isinstance(coverage.get("confirmed_counts"), dict) else None
    if counts is None:
        # 早期记录没有确认发现计数回执，只能核对取值范围。
        return displayed_score
    deduction = sum(_SEVERITY_DEDUCTION.get(str(sev), 0) * max(0, int(num or 0)) for sev, num in counts.items())
    return max(0, 100 - deduction)


def audit_report_score(
    task: Any,
    displayed_score: int | None,
    severity_count: dict[str, int],
    issue_count: int,
    score_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回评分复算结果；``final_score`` 是报告应展示的分数。"""
    stored = _int_or_none(getattr(task, "score", None))
    if (
        not getattr(task, "score_version", None)
        and str(getattr(task, "status", "success") or "success") != "success"
        and review_type_of(task) not in {"sandbox_test", "pentest"}
    ):
        # 未成功任务且无评分版本的记录分是旧版占位（失败任务写 0），不作为比对对象。
        stored = None
    coverage = task.coverage if isinstance(getattr(task, "coverage", None), dict) else {}
    review_type = str(getattr(task, "review_type", "") or "")
    scope = dict(score_scope or {})
    base = {
        "version": SCORE_AUDIT_VERSION, "reviewer": _REVIEWER, "stored_score": stored,
        "scope": scope.get("scope"), "scope_label": scope.get("label"),
        "verification_scope": "score_only", "independent_review": False,
    }

    if displayed_score is None:
        reason = (
            scope.get("reason") or coverage.get("score_unavailable_reason") or "覆盖或评分依据不足，按规则不出分"
        )
        return {**base, "status": "unavailable", "rule_score": None, "final_score": None, "reason": str(reason)}

    if review_type == "sandbox_test":
        rule = "沙箱验证分 = 100 × 已执行检查项得分之和 ÷ 已执行检查项数"
        rule_score = _sandbox_rule_score(coverage)
    elif review_type == "pentest":
        rule = "渗透已测范围分 = 100 − 40×严重 − 25×高 − 12×中 − 5×低（仅计已确认发现）"
        rule_score = _pentest_rule_score(coverage, displayed_score)
    elif issue_count > 0 or (scope.get("scope") == "partial" and stored is None):
        rule = f"{SCORING_VERSION}: 100 − 15×严重 − 8×高 − 3×中 − 1×低"
        rule_score = int(compute_score_breakdown(severity_count)["score"])
    else:
        rule = "无问题且记录分来自规则评分的任务保留记录分"
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
            "reason": "规则复算与记录分一致；问题真实性未经本环节独立复核" + _scope_note(scope),
        }
    return {
        **base, "status": "rescored", "rule": rule, "rule_score": rule_score, "final_score": rule_score,
        "rejected_score": stored if stored != rule_score else displayed_score,
        "reason": "记录分与规则复算不一致，报告采用规则校正分；问题真实性未经本环节独立复核" + _scope_note(scope),
    }
