"""Agent 反思与奖惩服务。"""
import json
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_governance import AgentProfile, AgentReflection, AgentRewardEvent


def record_reflection(
    db: Session,
    *,
    agent_code: str,
    summary: str,
    lesson: str = "",
    task_ref: str = "",
    risk_score: float = 0.0,
    reward_score: float = 0.0,
) -> AgentReflection:
    """记录 Agent 自我反思。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        summary: 反思摘要。
        lesson: 经验沉淀。
        task_ref: 任务引用。
        risk_score: 风险分。
        reward_score: 奖惩分。

    Returns:
        AgentReflection: 反思记录。
    """
    row = AgentReflection(
        agent_code=agent_code,
        task_ref=task_ref or None,
        summary=summary,
        lesson=lesson or None,
        risk_score=risk_score,
        reward_score=reward_score,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_reward(
    db: Session,
    *,
    agent_code: str,
    event_type: str,
    score: float,
    reason: str,
    impact: Optional[dict] = None,
) -> AgentRewardEvent:
    """记录奖励或惩罚，并调整 Agent 调度参数。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        event_type: reward 或 penalty。
        score: 分数。
        reason: 原因。
        impact: 影响详情。

    Returns:
        AgentRewardEvent: 奖惩事件。
    """
    row = AgentRewardEvent(
        agent_code=agent_code,
        event_type=event_type,
        score=score,
        reason=reason,
        impact_json=json.dumps(impact or {}, ensure_ascii=False),
    )
    db.add(row)
    profile = db.query(AgentProfile).filter(AgentProfile.code == agent_code).first()
    if profile:
        if score > 0:
            profile.priority = min(100, (profile.priority or 50) + int(score))
            profile.auto_approval_threshold = max(0.5, (profile.auto_approval_threshold or 0.75) - 0.01)
        elif score < 0:
            profile.priority = max(0, (profile.priority or 50) + int(score))
            profile.auto_approval_threshold = min(0.95, (profile.auto_approval_threshold or 0.75) + 0.02)
    db.commit()
    db.refresh(row)
    return row
