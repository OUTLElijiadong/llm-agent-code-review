"""Agent 治理策略引擎。

提供 ABAC 风格的策略匹配、默认风险判断和 fail-closed 决策落库能力。
"""
import json
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_governance import PolicyDecisionLog, PolicyRule
from app.services import agent_supervisor_service

ALLOW = "allow"
DENY = "deny"
ESCALATE = "escalate"

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CRITICAL = "critical"

_HIGH_RISK_ACTION_TOKENS = (
    "permission",
    "role",
    "delete",
    "drop",
    "truncate",
    "production_config",
    "prod_config",
    "agent.toggle",
)
_WRITE_SHELL_TOKENS = (
    "shell.write",
    "shell.exec.write",
    "rm ",
    "sudo",
    "chmod",
    "chown",
    "mv ",
    "cp ",
    "dd ",
)


@dataclass
class PolicyDecision:
    """策略决策结果。"""

    subject: str
    action: str
    resource: str
    decision: str
    risk_level: str
    risk_score: float
    reason: str
    matched_rule_id: Optional[int] = None
    log_id: Optional[int] = None


def _matches(pattern: str, value: str) -> bool:
    """判断策略字段是否匹配。

    Args:
        pattern: 策略中的匹配表达式，支持 ``*``、精确匹配和前缀通配。
        value: 待匹配的实际值。

    Returns:
        bool: 匹配返回 True，否则返回 False。
    """
    pattern = pattern or "*"
    value = value or ""
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def _risk_score(risk_level: str) -> float:
    """把风险等级映射为风险分。

    Args:
        risk_level: 风险等级。

    Returns:
        float: 0 到 1 的风险分。
    """
    return {
        LOW: 0.2,
        MEDIUM: 0.45,
        HIGH: 0.78,
        CRITICAL: 0.95,
    }.get(risk_level, 0.5)


def infer_default_decision(action: str, resource: str = "", context: Optional[dict] = None) -> PolicyDecision:
    """在未命中策略规则时推导默认决策。

    Args:
        action: 动作编码。
        resource: 资源编码。
        context: 策略上下文。

    Returns:
        PolicyDecision: 默认策略决策。
    """
    context = context or {}
    declared_risk = context.get("_supervisor_declared_risk")
    review = agent_supervisor_service.review_action(
        action,
        resource,
        declared_risk=declared_risk if isinstance(declared_risk, str) else None,
        context=context,
    )
    return PolicyDecision(
        subject=str(context.get("subject", "agent:unknown")),
        action=action,
        resource=resource,
        decision=review.decision,
        risk_level=review.risk_level,
        risk_score=_risk_score(review.risk_level),
        reason=f"监督子 Agent: {review.reason}",
    )


def evaluate(
    db: Session,
    *,
    subject: str,
    action: str,
    resource: str = "*",
    context: Optional[dict] = None,
    persist: bool = True,
) -> PolicyDecision:
    """评估一次 Agent 动作策略。

    Args:
        db: 数据库会话。
        subject: 主体，如 ``agent:review``。
        action: 动作编码。
        resource: 资源编码。
        context: 决策上下文。
        persist: 是否写入策略决策日志。

    Returns:
        PolicyDecision: 决策结果。
    """
    context = dict(context or {})
    context["subject"] = subject
    try:
        rules = (
            db.query(PolicyRule)
            .filter(PolicyRule.enabled == 1)
            .order_by(PolicyRule.priority.asc(), PolicyRule.id.asc())
            .all()
        )
        matched = None
        for rule in rules:
            if not _matches(rule.subject, subject):
                continue
            if not _matches(rule.action, action):
                continue
            if not _matches(rule.resource, resource):
                continue
            matched = rule
            break

        if matched:
            if matched.effect not in {ALLOW, DENY, ESCALATE} or matched.risk_level not in {
                LOW, MEDIUM, HIGH, CRITICAL,
            }:
                decision = PolicyDecision(
                    subject=subject,
                    action=action,
                    resource=resource,
                    decision=DENY,
                    risk_level=CRITICAL,
                    risk_score=_risk_score(CRITICAL),
                    reason=f"策略 {matched.name} 含无效决策值，已阻断",
                    matched_rule_id=matched.id,
                )
            else:
                review = agent_supervisor_service.review_action(
                    action,
                    resource,
                    declared_risk=matched.risk_level,
                    context=context,
                )
                if matched.effect == DENY:
                    effective_decision = DENY
                    effective_risk = matched.risk_level
                    reason = f"命中拒绝策略: {matched.name}"
                elif matched.effect == ESCALATE or review.decision == ESCALATE:
                    effective_decision = ESCALATE
                    effective_risk = max(
                        (matched.risk_level, review.risk_level),
                        key=lambda level: {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}.get(level, 3),
                    )
                    reason = f"监督子 Agent: {review.reason}; 策略: {matched.name}"
                else:
                    effective_decision = ALLOW
                    effective_risk = max(
                        (matched.risk_level, review.risk_level),
                        key=lambda level: {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}.get(level, 3),
                    )
                    reason = f"监督子 Agent 与策略复核通过: {matched.name}"
                decision = PolicyDecision(
                    subject=subject,
                    action=action,
                    resource=resource,
                    decision=effective_decision,
                    risk_level=effective_risk,
                    risk_score=_risk_score(effective_risk),
                    reason=reason,
                    matched_rule_id=matched.id,
                )
        else:
            decision = infer_default_decision(action, resource, context)
            decision.subject = subject

    except Exception as exc:  # noqa: BLE001 - 策略失败必须 fail closed
        decision = PolicyDecision(
            subject=subject,
            action=action,
            resource=resource,
            decision=DENY,
            risk_level=CRITICAL,
            risk_score=_risk_score(CRITICAL),
            reason=f"策略引擎异常，阻断优先: {exc}",
        )

    if persist:
        row = PolicyDecisionLog(
            subject=decision.subject,
            action=decision.action,
            resource=decision.resource,
            decision=decision.decision,
            risk_level=decision.risk_level,
            risk_score=decision.risk_score,
            reason=decision.reason,
            matched_rule_id=decision.matched_rule_id,
            context_json=json.dumps(context, ensure_ascii=False),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        decision.log_id = row.id

    return decision
