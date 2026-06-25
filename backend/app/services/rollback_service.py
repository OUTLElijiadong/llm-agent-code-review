"""Agent artifact 版本与回滚服务。"""
import json

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.models.agent_governance import AgentArtifactVersion, PolicyRule


def create_version(
    db: Session,
    *,
    agent_code: str,
    artifact_type: str,
    version: str,
    content: str,
    snapshot: str = "",
    status: str = "draft",
) -> AgentArtifactVersion:
    """创建 Agent artifact 版本。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        artifact_type: artifact 类型。
        version: 版本号。
        content: 版本内容。
        snapshot: 回滚快照。
        status: 版本状态。

    Returns:
        AgentArtifactVersion: 新增版本。
    """
    row = AgentArtifactVersion(
        agent_code=agent_code,
        artifact_type=artifact_type,
        version=version,
        content=content,
        snapshot=snapshot or None,
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def rollback_version(db: Session, version_id: int) -> AgentArtifactVersion:
    """回滚一个 Agent artifact 版本。

    Args:
        db: 数据库会话。
        version_id: 版本 ID。

    Returns:
        AgentArtifactVersion: 回滚后的版本记录。

    Raises:
        NotFoundError: 版本不存在。
        ValidationError: 版本没有快照。
    """
    row = db.get(AgentArtifactVersion, version_id)
    if not row:
        raise NotFoundError("版本不存在", code=40400)
    if not row.snapshot:
        raise ValidationError("该版本没有可回滚快照", code=40001)
    _apply_artifact_snapshot(db, row)
    row.content = row.snapshot
    row.status = "rolled_back"
    db.commit()
    db.refresh(row)
    return row


def _apply_artifact_snapshot(db: Session, row: AgentArtifactVersion) -> None:
    """将 artifact 快照恢复到对应治理配置。

    Args:
        db: 数据库会话。
        row: 版本记录。

    Returns:
        None。
    """
    if row.artifact_type != "policy":
        return
    try:
        payload = json.loads(row.snapshot or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationError("策略版本快照不是合法 JSON", code=40001) from exc
    rule_id = payload.get("rule_id")
    rule = db.get(PolicyRule, rule_id) if rule_id else None
    if not rule:
        rule = PolicyRule(rule_code=payload.get("rule_code") or row.version, name=payload.get("name") or row.version)
        db.add(rule)
    rule.rule_code = payload.get("rule_code") or rule.rule_code
    rule.name = payload.get("name") or rule.name
    rule.subject = payload.get("subject") or "*"
    rule.action = payload.get("action") or "*"
    rule.resource = payload.get("resource") or "*"
    rule.effect = payload.get("effect") or "allow"
    rule.risk_level = payload.get("risk_level") or "low"
    rule.condition_json = payload.get("condition_json") or "{}"
    rule.priority = int(payload.get("priority") or 100)
    rule.enabled = int(payload.get("enabled") if payload.get("enabled") is not None else 1)


def list_versions(
    db: Session,
    agent_code: str = "",
    artifact_type: str = "",
    limit: int = 100,
) -> list[AgentArtifactVersion]:
    """查询 Agent artifact 版本。

    Args:
        db: 数据库会话。
        agent_code: 可选 Agent 编码。
        artifact_type: 可选 artifact 类型。
        limit: 最大返回条数。

    Returns:
        list[AgentArtifactVersion]: 版本列表。
    """
    q = db.query(AgentArtifactVersion)
    if agent_code:
        q = q.filter(AgentArtifactVersion.agent_code == agent_code)
    if artifact_type:
        q = q.filter(AgentArtifactVersion.artifact_type == artifact_type)
    return q.order_by(AgentArtifactVersion.id.desc()).limit(limit).all()
