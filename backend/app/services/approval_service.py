"""Agent 治理审批服务。"""

import hashlib
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
        owner = _request_payload(item)["owner_user_id"]
        if isinstance(owner, bool) or not isinstance(owner, (int, str)):
            return -1
        owner_id = int(owner)
        return owner_id if owner_id > 0 else -1
    except (KeyError, TypeError, ValueError):
        return -1


def session_resume_required_item_ids(db: Session, items: list[ApprovalItem]) -> set[int]:
    """Return approval IDs bound to persisted Responses runs with one query."""
    run_to_item_ids: dict[str, set[int]] = {}
    for item in items:
        if not (item.action or "").strip().lower().startswith("responses."):
            continue
        payload = _request_payload(item)
        candidate_run_ids = {str(payload.get("run_id") or "").strip()}
        if (item.resource or "").startswith("response_run:"):
            candidate_run_ids.add(item.resource.removeprefix("response_run:").strip())
        for run_id in candidate_run_ids - {""}:
            run_to_item_ids.setdefault(run_id, set()).add(item.id)
    if not run_to_item_ids:
        return set()

    from app.models.agent_response_run import AgentResponseRun

    persisted_run_ids = {
        run_id for (run_id,) in db.query(AgentResponseRun.run_id)
        .filter(AgentResponseRun.run_id.in_(run_to_item_ids))
        .all()
    }
    return {
        item_id
        for run_id in persisted_run_ids
        for item_id in run_to_item_ids[run_id]
    }


def source_traces_for_actor(
    db: Session,
    actor: User,
    items: list[ApprovalItem],
) -> dict[int, dict[str, Optional[str]]]:
    """Return verified Responses source links with one owner-scoped run query."""
    candidates: dict[int, tuple[ApprovalItem, str, str, str]] = {}
    run_ids: set[str] = set()
    results: dict[int, dict[str, Optional[str]]] = {}
    for item in items:
        if not (item.action or "").strip().lower().startswith("responses."):
            continue
        payload = _request_payload(item)
        raw_run_id = payload.get("run_id")
        raw_call_id = payload.get("call_id")
        raw_tool_name = payload.get("tool")
        run_id = raw_run_id.strip() if isinstance(raw_run_id, str) else ""
        call_id = raw_call_id.strip() if isinstance(raw_call_id, str) else ""
        tool_name = raw_tool_name.strip() if isinstance(raw_tool_name, str) else ""
        resource = str(item.resource or "")
        expected_request_id = hashlib.sha256(f"responses:{run_id}:{call_id}".encode("utf-8")).hexdigest()
        if (
            _responses_owner_id(item) != int(actor.id)
            or not run_id
            or len(run_id) > 80
            or not call_id
            or len(call_id) > 160
            or not tool_name
            or len(tool_name) > 120
            or item.action != f"responses.{tool_name}"
            or resource != f"response_run:{run_id}"
            or item.copilot_request_id != expected_request_id
        ):
            results[item.id] = {"source_trace_status": "unavailable"}
            continue
        candidates[item.id] = (item, run_id, call_id, tool_name)
        run_ids.add(run_id)

    if not run_ids:
        return results

    from app.models.agent_response_run import AgentResponseRun

    runs = {
        str(run.run_id): run
        for run in db.query(AgentResponseRun).filter(
            AgentResponseRun.run_id.in_(run_ids),
            AgentResponseRun.user_id == int(actor.id),
            AgentResponseRun.surface == "admin",
        ).all()
    }
    for item_id, (item, run_id, call_id, tool_name) in candidates.items():
        payload = _request_payload(item)
        run = runs.get(run_id)
        try:
            checkpoint = json.loads(run.checkpoint_json or "{}") if run is not None else {}
        except (TypeError, json.JSONDecodeError):
            checkpoint = {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        pending = checkpoint.get("pending")
        pending = pending if isinstance(pending, dict) else {}
        call = pending.get("call") if isinstance(pending.get("call"), dict) else {}
        call_arguments = call.get("arguments")
        request_arguments = payload.get("arguments")
        if isinstance(call_arguments, dict) and isinstance(request_arguments, dict):
            # Reuse the exact persistence normalizer used when _approval stores the
            # request. Raw checkpoint arguments can contain secrets or values that
            # are intentionally replaced by an audit fingerprint.
            from app.services.agent_responses_service import _persisted_tool_arguments

            persisted_arguments = _persisted_tool_arguments(tool_name, call_arguments)
            try:
                arguments_match = json.dumps(
                    request_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ) == json.dumps(
                    persisted_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                )
            except (TypeError, ValueError):
                arguments_match = False
        else:
            arguments_match = False
        if (
            run is None
            or not str(run.session_key or "").strip()
            or run.status != "waiting_approval"
            or str(checkpoint.get("run_id") or "") != run_id
            or checkpoint.get("status") != "waiting_approval"
            or pending.get("kind") != "approval"
            or str(pending.get("approval_id") or "") != str(item_id)
            or str(call.get("call_id") or "") != call_id
            or str(call.get("name") or "") != tool_name
            or not arguments_match
        ):
            results[item_id] = {"source_trace_status": "unavailable"}
            continue
        results[item_id] = {
            "source_trace_status": "verified",
            "source_run_id": str(run.run_id),
            "source_session_id": str(run.session_key),
            "source_tool_name": tool_name,
        }
    return results


def requires_session_resume(db: Session, item: ApprovalItem) -> bool:
    """Check whether a Responses approval is bound to a persisted chat run."""
    return item.id in session_resume_required_item_ids(db, [item])


def _can_access(db: Session, actor: Optional[User], item: ApprovalItem) -> bool:
    """私人会话归属独立于管理权限，超级管理员也必须是同一账号。"""
    if actor is None:
        return False
    owner_id = _responses_owner_id(item)
    if owner_id is not None and owner_id != actor.id:
        return False
    if item.action == "knowledge.activate":
        from app.models.agent_governance import AgentKnowledgeDoc
        from app.services.agent_knowledge_service import _document_access_clause

        doc_id = _request_payload(item).get("doc_id")
        if isinstance(doc_id, bool) or not isinstance(doc_id, (int, str)):
            return False
        try:
            doc_id = int(doc_id)
        except ValueError:
            return False
        if not db.query(AgentKnowledgeDoc.id).filter(
            AgentKnowledgeDoc.id == doc_id, _document_access_clause(actor.id)
        ).first():
            return False
    return is_unique_super_admin(db, actor) or not requires_super_admin(db, item)


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
        detail=f"审批状态: {item.status}",
    )
    return item


def list_items(
    db: Session,
    status: str = "",
    limit: int = 1000,
    *,
    actor: Optional[User] = None,
    exclude_actions: tuple[str, ...] = (),
) -> list[ApprovalItem]:
    """查询审批事项列表。

    Args:
        db: 数据库会话。
        status: 可选状态过滤。
        limit: 最大返回条数。
        actor: 当前管理员；缺失或非唯一超级管理员时隐藏敏感审批。
        exclude_actions: 由专用审批工作流处理、应从通用审批列表中排除的动作。

    Returns:
        list[ApprovalItem]: 审批事项列表。
    """
    q = db.query(ApprovalItem)
    if status:
        q = q.filter(ApprovalItem.status == status)
    if exclude_actions:
        q = q.filter(~ApprovalItem.action.in_(exclude_actions))
    limit = max(0, min(limit, 1000))
    if limit == 0:
        return []
    # Applying SQL LIMIT before classification could hide older program-content
    # approvals when newer infrastructure approvals fill the page.
    rows: list[ApprovalItem] = []
    for item in q.order_by(ApprovalItem.id.desc()).yield_per(100):
        if _can_access(db, actor, item):
            rows.append(item)
            if len(rows) >= limit:
                break
    return rows


def count_items(
    db: Session,
    status: str = "",
    *,
    actor: Optional[User] = None,
    exclude_actions: tuple[str, ...] = (),
) -> int:
    """Count items visible to the same actor and filters as ``list_items``."""
    q = db.query(ApprovalItem)
    if status:
        q = q.filter(ApprovalItem.status == status)
    if exclude_actions:
        q = q.filter(~ApprovalItem.action.in_(exclude_actions))
    return sum(1 for item in q.yield_per(100) if _can_access(db, actor, item))


def decide_item(
    db: Session,
    admin: User,
    item_id: int,
    approve: bool,
    note: str = "",
    *,
    expected_action: Optional[str] = None,
) -> ApprovalItem:
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
    if expected_action is not None:
        if item.action != expected_action:
            raise ForbiddenError("该接口只能处理对应类型的审批事项", code=40300)
    if item.action == "agent_package.publish":
        from app.services import agent_studio_service
        from app.services.rbac_service import is_admin_user

        if not is_admin_user(db, admin.id):
            raise ForbiddenError("仅管理员可处理 Agent 发布审批", code=40300)
        agent_studio_service.assert_release_approval_target(db, item)
        request = agent_studio_service._load(item.request_json, {})
        if int(request.get("owner_id") or 0) == int(admin.id):
            raise ForbiddenError("Agent 发布审批必须由非申请人管理员处理", code=40300)
    if not _can_access(db, admin, item):
        raise ForbiddenError("无权处理该审批；私人会话仅限本人，服务器操作仅限超级管理员", code=40322)
    if (
        (item.action or "").strip().lower().startswith("responses.")
        and requires_session_resume(db, item)
    ):
        raise ValidationError(
            "这是绑定小菱原会话的工具审批，请回到该会话批准或驳回；通用审批中心不能续跑会话",
            code=40001,
        )
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
        detail=f"审批状态: {item.status}",
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

            agent_knowledge_service.activate_document(db, int(doc_id), user_id=item.decided_by, commit=False)
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
