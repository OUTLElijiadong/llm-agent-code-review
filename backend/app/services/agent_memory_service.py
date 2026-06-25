"""Agent 独立记忆服务。"""
from sqlalchemy.orm import Session

from app.models.agent_governance import AgentMemory


def list_memory(db: Session, agent_code: str = "", limit: int = 100) -> list[AgentMemory]:
    """查询 Agent 记忆列表。

    Args:
        db: 数据库会话。
        agent_code: 可选 Agent 编码。
        limit: 最大返回条数。

    Returns:
        list[AgentMemory]: 记忆列表。
    """
    q = db.query(AgentMemory).filter(AgentMemory.status == "active")
    if agent_code:
        q = q.filter(AgentMemory.agent_code == agent_code)
    return q.order_by(AgentMemory.weight.desc(), AgentMemory.id.desc()).limit(limit).all()


def add_memory(
    db: Session,
    *,
    agent_code: str,
    title: str,
    content: str,
    memory_type: str = "long_term",
    weight: float = 1.0,
    source_ref: str = "",
) -> AgentMemory:
    """新增 Agent 记忆。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        title: 记忆标题。
        content: 记忆内容。
        memory_type: 记忆类型。
        weight: 权重。
        source_ref: 来源引用。

    Returns:
        AgentMemory: 新增的记忆。
    """
    item = AgentMemory(
        agent_code=agent_code,
        title=title[:200],
        content=content,
        memory_type=memory_type,
        weight=weight,
        source_ref=source_ref or None,
        status="active",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
