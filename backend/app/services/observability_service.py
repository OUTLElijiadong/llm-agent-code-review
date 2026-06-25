"""Agent 治理观测服务。"""
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.agent_governance import (
    AgentAlert,
    AgentJobRun,
    AgentRewardEvent,
    ApprovalItem,
    PolicyDecisionLog,
    ToolCallLog,
)


def overview(db: Session) -> dict:
    """聚合治理观测指标。

    Args:
        db: 数据库会话。

    Returns:
        dict: 观测指标摘要。
    """
    tool_status = (
        db.query(ToolCallLog.status, func.count(ToolCallLog.id))
        .group_by(ToolCallLog.status)
        .all()
    )
    approval_status = (
        db.query(ApprovalItem.status, func.count(ApprovalItem.id))
        .group_by(ApprovalItem.status)
        .all()
    )
    risk_distribution = (
        db.query(PolicyDecisionLog.risk_level, func.count(PolicyDecisionLog.id))
        .group_by(PolicyDecisionLog.risk_level)
        .all()
    )
    return {
        "tool_status": [{"status": status or "unknown", "count": count} for status, count in tool_status],
        "approval_status": [{"status": status or "unknown", "count": count} for status, count in approval_status],
        "risk_distribution": [{"risk_level": risk or "unknown", "count": count} for risk, count in risk_distribution],
        "open_alerts": db.query(AgentAlert).filter(AgentAlert.status == "open").count(),
        "job_runs": db.query(AgentJobRun).count(),
        "reward_score_total": float(db.query(func.coalesce(func.sum(AgentRewardEvent.score), 0.0)).scalar() or 0.0),
    }


def list_alerts(db: Session, status: str = "open", limit: int = 100) -> list[AgentAlert]:
    """查询治理告警。

    Args:
        db: 数据库会话。
        status: 告警状态。
        limit: 最大返回条数。

    Returns:
        list[AgentAlert]: 告警列表。
    """
    q = db.query(AgentAlert)
    if status:
        q = q.filter(AgentAlert.status == status)
    return q.order_by(AgentAlert.id.desc()).limit(limit).all()


def resolve_alert(db: Session, alert_id: int, admin_id: int, note: str = "") -> AgentAlert:
    """关闭治理告警。

    Args:
        db: 数据库会话。
        alert_id: 告警 ID。
        admin_id: 处理人 ID。
        note: 处理说明。

    Returns:
        AgentAlert: 关闭后的告警。

    Raises:
        NotFoundError: 告警不存在。
    """
    alert = db.get(AgentAlert, alert_id)
    if not alert:
        raise NotFoundError("治理告警不存在", code=40400)
    alert.status = "resolved"
    alert.resolved_by = admin_id
    alert.resolved_at = datetime.now(timezone.utc)
    if note:
        alert.detail_json = note
    db.commit()
    db.refresh(alert)
    return alert
