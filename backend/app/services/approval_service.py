"""Agent 治理审批服务。"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.super_admin import SERVER_OPS_PERMISSION_PREFIX, is_unique_super_admin
from app.models.agent_governance import ApprovalItem
from app.models.user import User
from app.services import audit_service

AUTO_APPROVABLE_RISKS = {"low", "medium"}

_SUPER_ADMIN_ACTION_PREFIXES = (
    "operations.",
    "server_ops:",
    "server_ops.",
    "shell.",
    "deploy.",
    "responses.admin_execute_operation",
    "responses.mcp_",
)
_SUPER_ADMIN_ACTIONS = {
    "ops_execute",
    "production_config.update",
    "prod_config.update",
    "restart_service",
}
_SUPER_ADMIN_RESOURCE_PREFIXES = (
    "server_ops:",
    "production:",
    "host:",
    "infrastructure:",
    "system:",
    "mcp:",
)
_SUPER_ADMIN_RESOURCES = {
    "production",
    "production_config",
    "infrastructure",
    "server",
    "host",
}


def _utcnow() -> datetime:
    """获取 UTC 当前时间。

    Returns:
        datetime: 带时区的 UTC 时间。
    """
    return datetime.now(timezone.utc)


def _request_payload(item: ApprovalItem) -> dict:
    """Return a safely parsed approval payload for sensitivity classification."""

    if not item.request_json:
        return {}
    if isinstance(item.request_json, dict):
        return item.request_json
    try:
        payload = json.loads(item.request_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def requires_super_admin(db: Session, item: ApprovalItem) -> bool:
    """Classify approvals that can affect host or global infrastructure.

    The persisted action/resource pair is authoritative. Responses capability
    approvals carry a generic action, so their requested capability is also
    resolved against the execution-time permission registry.
    """

    action = (item.action or "").strip().lower()
    resource = (item.resource or "").strip().lower()
    if action in _SUPER_ADMIN_ACTIONS or action.startswith(_SUPER_ADMIN_ACTION_PREFIXES):
        return True
    if resource in _SUPER_ADMIN_RESOURCES or resource.startswith(_SUPER_ADMIN_RESOURCE_PREFIXES):
        return True
    if action != "responses.admin_execute_capability":
        return False

    arguments = _request_payload(item).get("arguments")
    if not isinstance(arguments, dict):
        return True
    capability = str(arguments.get("capability") or "")
    if not capability:
        return True
    from app.services.admin_capability_registry import CAPABILITY_BY_CODE

    spec = CAPABILITY_BY_CODE.get(capability)
    if spec is None:
        return True
    if (spec.permission or "").startswith(SERVER_OPS_PERMISSION_PREFIX):
        return True
    if capability not in {"jobs.run", "jobs.update"}:
        return False

    params = arguments.get("params")
    if not isinstance(params, dict):
        return True
    try:
        job_id = int(params["job_id"])
    except (KeyError, TypeError, ValueError):
        return True
    from app.models.agent_governance import AgentJob
    from app.services import scheduler_service

    job = db.get(AgentJob, job_id)
    return job is None or scheduler_service.requires_super_admin(job.job_type)


def _responses_owner_id(item: ApprovalItem) -> Optional[int]:
    """Return the owner of a Responses approval, failing closed on malformed data."""

    if not (item.action or "").strip().lower().startswith("responses."):
        return None
    try:
        return int(_request_payload(item)["owner_user_id"])
    except (KeyError, TypeError, ValueError):
        return -1


def _ordinary_admin_can_access(db: Session, actor: Optional[User], item: ApprovalItem) -> bool:
    """Apply sensitive-action and Responses-owner boundaries for a normal admin."""

    if actor is None or requires_super_admin(db, item):
        return False
    owner_id = _responses_owner_id(item)
    return owner_id is None or owner_id == actor.id


def create_or_auto_decide(
    db: Session,
    *,
    title: str,
    action: str,
    resource: str,
    risk_level: str,
    decision: str,
    reason: str,
    agent_code: str = "",
    request: Optional[dict] = None,
    actor: Optional[User] = None,
    copilot_request_id: str = "",
) -> ApprovalItem:
    """创建审批事项并按风险自动决策。

    Args:
        db: 数据库会话。
        title: 审批标题。
        action: 动作编码。
        resource: 资源编码。
        risk_level: 风险等级。
        decision: 策略决策。
        reason: 决策原因。
        agent_code: 关联 Agent 编码。
        request: 请求上下文。
        actor: 触发用户。

    Returns:
        ApprovalItem: 审批事项。
    """
    item = ApprovalItem(
        title=title,
        agent_code=agent_code or None,
        action=action,
        resource=resource,
        risk_level=risk_level,
        status="pending",
        decision=None,
        decision_reason=reason,
        request_json=json.dumps(request or {}, ensure_ascii=False),
        copilot_request_id=copilot_request_id or None,
    )
    if not requires_super_admin(db, item) and risk_level in AUTO_APPROVABLE_RISKS and decision == "allow":
        item.status = "auto_approved"
        item.decision = decision
        item.decided_by = actor.id if actor else None
        item.decided_at = _utcnow()
    db.add(item)
    db.commit()
    db.refresh(item)
    audit_service.log(
        db,
        actor,
        "agent_approval",
        target_type="approval",
        target_id=item.id,
        detail=f"{item.status}: {title}",
    )
    return item


def list_items(
    db: Session,
    status: str = "",
    limit: int = 100,
    *,
    actor: Optional[User] = None,
) -> list[ApprovalItem]:
    """查询审批事项列表。

    Args:
        db: 数据库会话。
        status: 可选状态过滤。
        limit: 最大返回条数。
        actor: 当前管理员；缺失或非唯一超级管理员时隐藏敏感审批。

    Returns:
        list[ApprovalItem]: 审批事项列表。
    """
    q = db.query(ApprovalItem)
    if status:
        q = q.filter(ApprovalItem.status == status)
    limit = max(0, min(limit, 1000))
    if limit == 0:
        return []
    if is_unique_super_admin(db, actor):
        return q.order_by(ApprovalItem.id.desc()).limit(limit).all()

    # Applying SQL LIMIT before classification could hide older program-content
    # approvals when newer infrastructure approvals fill the page.
    rows: list[ApprovalItem] = []
    for item in q.order_by(ApprovalItem.id.desc()).yield_per(100):
        if _ordinary_admin_can_access(db, actor, item):
            rows.append(item)
            if len(rows) >= limit:
                break
    return rows


def decide_item(db: Session, admin: User, item_id: int, approve: bool, note: str = "") -> ApprovalItem:
    """人工审批或拒绝一个审批事项。

    Args:
        db: 数据库会话。
        admin: 管理员用户。
        item_id: 审批事项 ID。
        approve: True 表示通过，False 表示拒绝。
        note: 处理说明。

    Returns:
        ApprovalItem: 更新后的审批事项。

    Raises:
        NotFoundError: 审批事项不存在。
        ValidationError: 审批事项已终结。
    """
    item = db.query(ApprovalItem).filter(ApprovalItem.id == item_id).with_for_update().first()
    if not item:
        raise NotFoundError("审批事项不存在", code=40400)
    if not is_unique_super_admin(db, admin) and not _ordinary_admin_can_access(db, admin, item):
        raise ForbiddenError("无权处理该审批；服务器与全局基础设施审批仅限超级管理员 admin", code=40322)
    if item.status in ("approved", "rejected", "auto_approved"):
        if item.action == "agent_package.publish" and (
            (approve and item.status == "approved") or (not approve and item.status == "rejected")
        ):
            return item
        raise ValidationError("审批事项已处理", code=40001)

    item.status = "approved" if approve else "rejected"
    item.decision = "allow" if approve else "deny"
    item.decision_reason = note or ("管理员审批通过" if approve else "管理员审批拒绝")
    item.decided_by = admin.id
    item.decided_at = _utcnow()
    try:
        if approve:
            _apply_approval_side_effect(db, item)
        elif item.action == "agent_package.publish":
            from app.services import agent_studio_service

            agent_studio_service.reject_for_approval(db, item)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(item)
    if approve and item.action == "agent_package.publish":
        from app.services.declarative_agent_runtime import publish_catalog_invalidation

        publish_catalog_invalidation("publish", item.agent_code or "")
    audit_service.log(
        db,
        admin,
        "agent_approval",
        target_type="approval",
        target_id=item.id,
        detail=f"{item.status}: {item.title}",
    )
    return item


def _apply_approval_side_effect(db: Session, item: ApprovalItem) -> None:
    """应用审批通过后的治理副作用。

    Args:
        db: 数据库会话。
        item: 审批事项。

    Returns:
        None。
    """
    payload = {}
    if item.request_json:
        try:
            payload = json.loads(item.request_json)
        except json.JSONDecodeError:
            payload = {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else payload
    if item.action == "agent_package.publish":
        from app.services import agent_studio_service

        agent_studio_service.publish_for_approval(db, item)
        return
    if item.action == "knowledge.activate":
        doc_id = payload.get("doc_id")
        if doc_id:
            from app.services import agent_knowledge_service

            agent_knowledge_service.activate_document(db, int(doc_id), commit=False)
        return
    if item.action == "user.set_role":
        from app.services import user_service

        user_service.set_role(
            db,
            int(context["user_id"]),
            str(context["role"]),
            admin_id=int(item.decided_by or 0),
            commit=False,
        )
        return
    if item.action == "user.delete":
        from app.services import user_service

        user_service.delete_user(
            db,
            int(context["user_id"]),
            int(item.decided_by or 0),
            commit=False,
        )
        return
    if item.action == "agent.toggle":
        from app.services import agent_governance_service

        enable = bool(context["enable"])
        agent_governance_service.update_profile(
            db,
            str(context["agent_code"]),
            {"is_enabled": 1 if enable else 0, "status": "idle" if enable else "disabled"},
            commit=False,
        )
