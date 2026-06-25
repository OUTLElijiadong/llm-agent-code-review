"""Agent 工具网关。

统一承接 Agent 工具调用，执行策略评估、审批升级、工具日志和审计。
"""
import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models.agent_governance import AgentToolPermission, PolicyDecisionLog, ToolCallLog
from app.models.user import User
from app.services import approval_service, policy_engine


@dataclass
class ToolGatewayResult:
    """工具网关执行结果。"""

    success: bool
    status: str
    decision: str
    risk_level: str
    data: object = None
    error: Optional[str] = None
    approval_id: Optional[int] = None
    log_id: Optional[int] = None


def execute(
    db: Session,
    *,
    agent_code: str,
    tool_code: str,
    action: str,
    resource: str = "",
    handler: Optional[Callable[[], object]] = None,
    input_summary: str = "",
    actor: Optional[User] = None,
    context: Optional[dict] = None,
) -> ToolGatewayResult:
    """执行一次受治理的工具调用。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        tool_code: 工具编码。
        action: 动作编码。
        resource: 资源编码。
        handler: 实际工具执行函数；为空时只记录通过。
        input_summary: 输入摘要，避免落明文敏感内容。
        actor: 触发用户。
        context: 策略上下文。

    Returns:
        ToolGatewayResult: 工具调用结果。
    """
    t0 = time.time()
    subject = f"agent:{agent_code}"
    decision = policy_engine.evaluate(
        db,
        subject=subject,
        action=action,
        resource=resource or "*",
        context=context or {},
    )
    decision = _apply_tool_permission(
        db,
        agent_code=agent_code,
        tool_code=tool_code,
        action=action,
        resource=resource or "*",
        subject=subject,
        decision=decision,
        context=context or {},
    )

    approval_id = None
    if decision.decision == policy_engine.ESCALATE:
        approval = approval_service.create_or_auto_decide(
            db,
            title=f"{agent_code} 请求执行 {action}",
            action=action,
            resource=resource or "*",
            risk_level=decision.risk_level,
            decision=decision.decision,
            reason=decision.reason,
            agent_code=agent_code,
            request={"tool_code": tool_code, "input_summary": input_summary, "context": context or {}},
            actor=actor,
        )
        approval_id = approval.id
        log = _write_log(
            db,
            agent_code=agent_code,
            tool_code=tool_code,
            action=action,
            resource=resource,
            status="escalated",
            risk_level=decision.risk_level,
            decision=decision.decision,
            input_summary=input_summary,
            output_summary="已升级审批",
            error="",
            duration_ms=_elapsed_ms(t0),
            policy_decision_id=decision.log_id,
            approval_id=approval_id,
        )
        return ToolGatewayResult(
            success=False,
            status="escalated",
            decision=decision.decision,
            risk_level=decision.risk_level,
            error="动作已升级审批",
            approval_id=approval_id,
            log_id=log.id,
        )

    if decision.decision == policy_engine.DENY:
        log = _write_log(
            db,
            agent_code=agent_code,
            tool_code=tool_code,
            action=action,
            resource=resource,
            status="denied",
            risk_level=decision.risk_level,
            decision=decision.decision,
            input_summary=input_summary,
            output_summary="",
            error=decision.reason,
            duration_ms=_elapsed_ms(t0),
            policy_decision_id=decision.log_id,
            approval_id=None,
        )
        return ToolGatewayResult(
            success=False,
            status="denied",
            decision=decision.decision,
            risk_level=decision.risk_level,
            error=decision.reason,
            log_id=log.id,
        )

    try:
        data = handler() if handler else {"ok": True}
        log = _write_log(
            db,
            agent_code=agent_code,
            tool_code=tool_code,
            action=action,
            resource=resource,
            status="success",
            risk_level=decision.risk_level,
            decision=decision.decision,
            input_summary=input_summary,
            output_summary=str(data)[:1000],
            error="",
            duration_ms=_elapsed_ms(t0),
            policy_decision_id=decision.log_id,
            approval_id=None,
        )
        return ToolGatewayResult(
            success=True,
            status="success",
            decision=decision.decision,
            risk_level=decision.risk_level,
            data=data,
            log_id=log.id,
        )
    except Exception as exc:  # noqa: BLE001 - 记录工具异常并返回给调用方
        log = _write_log(
            db,
            agent_code=agent_code,
            tool_code=tool_code,
            action=action,
            resource=resource,
            status="failed",
            risk_level=decision.risk_level,
            decision=decision.decision,
            input_summary=input_summary,
            output_summary="",
            error=str(exc),
            duration_ms=_elapsed_ms(t0),
            policy_decision_id=decision.log_id,
            approval_id=None,
        )
        return ToolGatewayResult(
            success=False,
            status="failed",
            decision=decision.decision,
            risk_level=decision.risk_level,
            error=str(exc),
            log_id=log.id,
        )


def _elapsed_ms(start: float) -> int:
    """计算毫秒耗时。

    Args:
        start: 起始时间戳。

    Returns:
        int: 毫秒耗时。
    """
    return int((time.time() - start) * 1000)


def _permission_risk_score(risk_level: str) -> float:
    """把工具权限风险等级映射为风险分。

    Args:
        risk_level: 风险等级。

    Returns:
        float: 风险分。
    """
    return {
        policy_engine.LOW: 0.2,
        policy_engine.MEDIUM: 0.45,
        policy_engine.HIGH: 0.78,
        policy_engine.CRITICAL: 0.95,
    }.get(risk_level, 0.5)


def _apply_tool_permission(
    db: Session,
    *,
    agent_code: str,
    tool_code: str,
    action: str,
    resource: str,
    subject: str,
    decision: policy_engine.PolicyDecision,
    context: dict,
) -> policy_engine.PolicyDecision:
    """应用 Agent 工具权限覆盖策略决策。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        tool_code: 工具编码。
        action: 动作编码。
        resource: 资源编码。
        subject: 策略主体。
        decision: 原策略决策。
        context: 策略上下文。

    Returns:
        policy_engine.PolicyDecision: 应用工具权限后的决策。
    """
    permission = (
        db.query(AgentToolPermission)
        .filter(
            AgentToolPermission.agent_code == agent_code,
            AgentToolPermission.tool_code == tool_code,
            AgentToolPermission.enabled == 1,
        )
        .first()
    )
    if not permission or permission.permission == policy_engine.ALLOW:
        return decision

    decision.decision = permission.permission
    decision.risk_level = permission.risk_level
    decision.risk_score = _permission_risk_score(permission.risk_level)
    decision.reason = f"命中工具权限配置: {permission.tool_code} -> {permission.permission}"
    row = PolicyDecisionLog(
        subject=subject,
        action=action,
        resource=resource,
        decision=decision.decision,
        risk_level=decision.risk_level,
        risk_score=decision.risk_score,
        reason=decision.reason,
        context_json=json.dumps(context, ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    decision.log_id = row.id
    return decision


def _write_log(
    db: Session,
    *,
    agent_code: str,
    tool_code: str,
    action: str,
    resource: str,
    status: str,
    risk_level: str,
    decision: str,
    input_summary: str,
    output_summary: str,
    error: str,
    duration_ms: int,
    policy_decision_id: Optional[int],
    approval_id: Optional[int],
) -> ToolCallLog:
    """写入工具调用日志。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        tool_code: 工具编码。
        action: 动作编码。
        resource: 资源编码。
        status: 调用状态。
        risk_level: 风险等级。
        decision: 策略决策。
        input_summary: 输入摘要。
        output_summary: 输出摘要。
        error: 错误信息。
        duration_ms: 毫秒耗时。
        policy_decision_id: 策略决策日志 ID。
        approval_id: 审批事项 ID。

    Returns:
        ToolCallLog: 持久化后的工具调用日志。
    """
    log = ToolCallLog(
        agent_code=agent_code,
        tool_code=tool_code,
        action=action,
        resource=resource or "",
        status=status,
        risk_level=risk_level,
        decision=decision,
        input_summary=input_summary[:4000] if input_summary else None,
        output_summary=output_summary[:4000] if output_summary else None,
        error=error[:4000] if error else None,
        duration_ms=duration_ms,
        policy_decision_id=policy_decision_id,
        approval_id=approval_id,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
