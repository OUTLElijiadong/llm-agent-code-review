"""Versioned review scoring based on severity deductions."""

from __future__ import annotations

SCORING_VERSION = "severity-deduction-v1"
BASE_SCORE = 100
DEDUCT = {"严重": 15, "高": 8, "中": 3, "低": 1}


def score_risk_level(score: int | float | None) -> str:
    """将综合评分映射为所有报告出口共用的四档风险等级。"""

    if score is None:
        return "未评估"
    value = float(score)
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


CVSS_SCORING_VERSION = "cvss-weighted-v1"
CVSS_WEIGHT = 0.7
QUALITY_WEIGHT = 0.3


def compute_composite_score_breakdown(
    severity_count: dict,
    findings: object,
    *,
    quality_score: int | float | None = None,
) -> dict:
    """CVSS 漏洞严重度占 70% 的平台分；保留旧算法与未评估范围。

    单项 CVSS 只从合法 3.1 向量复算。以最高基础分避免大量低风险发现
    稀释高风险发现；问题数量仍体现在质量项。缺失向量不假定为安全：
    全缺失时沿用旧规则，部分缺失时综合分不能高于原规则分。沙箱传入
    的实测检查分也是上限，防止没有发现漏洞时掩盖失败的检查。
    """
    import math
    from collections.abc import Mapping
    from decimal import ROUND_HALF_UP, Decimal

    from app.ai.cvss import CVSS_STANDARD, CVSS_STANDARD_URL, CVSS_VERSION, calculate_cvss_score

    legacy = compute_score_breakdown(severity_count)
    quality = legacy["score"] if quality_score is None else quality_score
    if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not math.isfinite(quality):
        raise ValueError("质量或测试规则分必须是 0 至 100 的有限数值")
    if not 0 <= quality <= 100:
        raise ValueError("质量或测试规则分必须在 0 至 100 之间")

    def field(row: object, name: str) -> object:
        return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)

    rows = list(findings or [])
    active = [row for row in rows if field(row, "status") != "refuted"]
    scores = [calculate_cvss_score(field(row, "cvss_vector")) for row in active]
    valid = [value for value in scores if value is not None]
    missing = len(active) - len(valid)
    cvss = {
        "version": CVSS_VERSION,
        "score": None,
        "max_base_score": max(valid) if valid else None,
        "scored_findings": len(valid),
        "unscored_findings": missing,
        "weight": CVSS_WEIGHT if valid else 0,
        "source": "validated_vector",
    }
    result = {
        **legacy,
        "policy_version": CVSS_SCORING_VERSION,
        "cvss_standard": CVSS_STANDARD,
        "cvss_standard_url": CVSS_STANDARD_URL,
        "legacy_rule": dict(legacy),
        "components": {
            "cvss": cvss,
            "quality": {"score": quality, "weight": QUALITY_WEIGHT if valid else 1.0},
        },
        "cvss_status": "partial" if missing else "complete",
        "formula": "平台综合分 = 70% × (100 − 10 × 最高 CVSS 3.1 基础分) + 30% × 原规则分",
        "coverage_cap_applied": False,
    }
    if not valid:
        result.update({
            "score": int(Decimal(str(quality)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            "raw_score": quality,
            "risk_level": score_risk_level(quality),
            "cvss_status": "unavailable" if active else "not_applicable",
            "cvss_reason": (
                "发现未提供可复算的 CVSS 向量，暂用原规则分，CVSS 70% 权重未生效"
                if active else "已审范围内没有可评分漏洞，保留原质量或测试规则分；不代表未测范围安全"
            ),
        })
        return result

    cvss["score"] = round(100 - 10 * max(valid), 2)
    raw = round(CVSS_WEIGHT * cvss["score"] + QUALITY_WEIGHT * quality, 4)
    # 缺失向量、实测失败均不能被较低的已知 CVSS 严重度掩盖。
    cap = quality if missing or quality_score is not None else BASE_SCORE
    capped = min(raw, cap)
    score = max(0, min(BASE_SCORE, int(Decimal(str(capped)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
    result.update({
        "version": CVSS_SCORING_VERSION,
        "raw_score": raw,
        "score": score,
        "total_deduction": BASE_SCORE - score,
        "risk_level": score_risk_level(score),
        "coverage_cap_applied": capped < raw,
        "cvss_reason": (
            "部分发现缺少有效向量，综合分受原规则分上限约束"
            if missing else "CVSS 由有效向量复算，综合分是平台规则，不是 CVSS 官方项目评级"
        ),
    })
    if capped < raw:
        result["score_cap"] = quality
        result["score_cap_reason"] = "未评分发现或实测检查分限制"
    return result


def uses_cvss_policy(task: object) -> bool:
    """识别新版评分策略（包括向量缺失回退），不改变历史记录。"""
    from collections.abc import Mapping

    def field(name: str) -> object:
        return task.get(name) if isinstance(task, Mapping) else getattr(task, name, None)

    breakdown = field("score_breakdown")
    return field("score_version") == CVSS_SCORING_VERSION or (
        isinstance(breakdown, Mapping) and breakdown.get("policy_version") == CVSS_SCORING_VERSION
    )
