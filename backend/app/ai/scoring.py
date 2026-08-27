"""Versioned review scoring based on severity deductions."""

from __future__ import annotations

SCORING_VERSION = "severity-deduction-v1"
BASE_SCORE = 100
DEDUCT = {"严重": 15, "高": 8, "中": 3, "低": 1}


def score_risk_level(score: int | float | None) -> str:
    """将综合评分映射为所有报告出口共用的四档风险等级。"""

    value = float(score or 0)
    if value >= 80:
        return "低风险"
    if value >= 60:
        return "中风险"
    if value >= 40:
        return "高风险"
    return "极高风险"


def compute_score_breakdown(severity_count: dict) -> dict:
    """Return the score and a complete, stable explanation of every deduction."""
    counts = {severity: max(0, int(severity_count.get(severity, 0) or 0)) for severity in DEDUCT}
    deductions = {severity: DEDUCT[severity] * counts[severity] for severity in DEDUCT}
    total_deduction = sum(deductions.values())
    raw_score = BASE_SCORE - total_deduction
    score = max(0, min(BASE_SCORE, raw_score))
    return {
        "version": SCORING_VERSION,
        "base_score": BASE_SCORE,
        "weights": dict(DEDUCT),
        "counts": counts,
        "deductions": deductions,
        "total_deduction": total_deduction,
        "raw_score": raw_score,
        "score": score,
        "risk_level": score_risk_level(score),
    }


def compute_score(severity_count: dict) -> int:
    """根据各严重级别问题数量计算综合评分

    Args:
        severity_count: dict如 {"严重": 1, "高": 3, "中": 5, "低": 3}

    Returns:
        int: 0-100的整数评分
    """
    return int(compute_score_breakdown(severity_count)["score"])
