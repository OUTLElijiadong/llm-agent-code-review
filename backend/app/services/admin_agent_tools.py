"""管理员 AI 代管后台工具。

让管理员的聊天 Agent 能帮管理员管理后台:
- 只读类(查用户/角色/治理概览/告警/服务器状态)直接执行;
- 写操作(设角色/删用户/启停 Agent)经聊天确认后直接执行，并写入工具调用日志与审计。

所有工具仅管理员可用(is_admin_user 校验),非管理员调用返回权限错误。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult
from app.agents.event_bus import emit_event
from app.agents.events import AgentEventType
from app.core.permission_codes import PermissionCode
from app.models.agent_governance import ApprovalItem
from app.models.custom_agent import CustomAgent, CustomAgentRelease, CustomAgentVersion, CustomSkill, CustomSkillVersion
from app.models.user import User
from app.services import (
    agent_governance_service,
    approval_service,
    audit_service,
    observability_service,
    rbac_service,
    user_service,
)
from app.services.system_service import system_status


def _is_admin(db: Session, user: Optional[User]) -> bool:
    if user is None:
        return False
    return rbac_service.is_admin_user(db, user.id)


def _deny() -> AgentResult:
    return AgentResult(success=False, error="仅管理员可使用后台代管工具")


def _emit_admin_write_event(
    type_: AgentEventType,
    user: User,
    context: dict[str, Any],
    *,
    action: str,
    resource: str,
    message: str,
) -> None:
    request_id = str(context.get("copilot_request_id") or "unknown")
    emit_event(
        type_,
        agent="manager",
        trace_id=f"trc_copilot_{request_id[:12]}",
        parent="admin_copilot",
        message=message,
        payload={"operation": action, "resource": resource},
        user_id=user.id,
    )


# ──────────────────────────── 只读工具(直接执行)────────────────────────────

def admin_list_users(db: Session, user: Optional[User], keyword: str = "",
                     role: str = "", page: int = 1, page_size: int = 20,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询用户列表(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    data = user_service.list_users(db, keyword=keyword, role=role, page=page, page_size=page_size)
    items = [
        {
            "id": u.id, "username": u.username, "nickname": u.nickname,
            "role": u.role, "status": u.status,
        }
        for u in data.get("items", [])
    ]
    return AgentResult(success=True, data={"total": data.get("total", 0), "users": items})


def admin_list_roles(db: Session, user: Optional[User],
                     ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询角色列表(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    roles = rbac_service.list_roles(db)
    return AgentResult(success=True, data=[
        {"code": r.code, "name": r.name, "description": r.description} for r in roles
    ])


def admin_governance_overview(db: Session, user: Optional[User],
                              ctx: Optional[AgentContext] = None) -> AgentResult:
    """Agent 治理概览(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    return AgentResult(success=True, data=observability_service.overview(db))


def admin_list_agents(db: Session, user: Optional[User],
                      ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询 Agent 配置档案(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    profiles = agent_governance_service.list_profiles(db)
    return AgentResult(success=True, data=[
        {"agent_code": p.code, "name": p.name, "status": p.status, "is_enabled": p.is_enabled}
        for p in profiles
    ])


def admin_list_approvals(db: Session, user: Optional[User], status: str = "pending",
                         ctx: Optional[AgentContext] = None) -> AgentResult:
    """查询审批事项(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    items = approval_service.list_items(db, status=status)
    return AgentResult(success=True, data=[
        {
            "id": i.id,
            "title": i.title,
            "action": i.action,
            "resource": i.resource,
            "status": i.status,
            "risk_level": i.risk_level,
            "decision": i.decision,
            "decision_reason": i.decision_reason,
            "request": _load_json(i.request_json, {}),
        }
        for i in items
    ])


def _load_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _version_authoring(version: Optional[CustomAgentVersion]) -> Optional[dict[str, Any]]:
    if version is None:
        return None
    return {
        "version_id": version.id,
        "version_number": version.version_number,
        "prompt": version.prompt,
        "review_focus": version.review_focus,
        "model_config": _load_json(version.model_config_json, {}),
        "input_schema": _load_json(version.input_schema_json, {}),
        "output_schema": _load_json(version.output_schema_json, {}),
        "checksum": version.checksum,
        "status": version.status,
        "original_author_id": version.original_author_id,
        "revised_by": version.revised_by,
        "revision_note": version.revision_note,
    }


def _delegate_target_detail(db: Session, agent_code: str) -> Optional[dict[str, Any]]:
    """Freeze the custom delegate target that will be used after approval."""
    target = db.query(CustomAgent).filter(CustomAgent.code == agent_code).first()
    if target is None:
        return None
    version = (
        db.get(CustomAgentVersion, target.current_published_version_id)
        if target.current_published_version_id
        else None
    )
    release = (
        db.query(CustomAgentRelease)
        .filter(
            CustomAgentRelease.agent_id == target.id,
            CustomAgentRelease.agent_version_id == target.current_published_version_id,
        )
        .order_by(CustomAgentRelease.id.desc())
        .first()
        if target.current_published_version_id
        else None
    )
    return {
        "id": target.id,
        "code": target.code,
        "name": target.name,
        "description": target.description or "",
        "status": target.status,
        "is_enabled": bool(target.is_enabled),
        "current_published_version_id": target.current_published_version_id,
        "current_published_version": _version_authoring(version),
        "release": {
            "id": release.id,
            "status": release.status,
            "package_checksum": release.package_checksum,
        } if release else None,
    }


def _skill_dependency_detail(
    db: Session,
    dependency: dict[str, Any],
    *,
    path: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Return the complete bounded dependency closure shown in release approval."""
    skill_version_id = int(dependency.get("skill_version_id") or 0)
    skill_version = db.get(CustomSkillVersion, skill_version_id)
    skill = db.get(CustomSkill, skill_version.skill_id) if skill_version else None
    capabilities = _load_json(skill_version.requested_capabilities_json, []) if skill_version else []
    definition = _load_json(skill_version.definition_json, {}) if skill_version else {}
    detail = {
        **dependency,
        "skill_name": skill.name if skill else None,
        "skill_description": skill.description if skill else None,
        "definition": definition,
        "requested_capabilities": capabilities,
        "test_evidence": _load_json(skill_version.test_evidence_json, {}) if skill_version else {},
        "status": skill_version.status if skill_version else "missing",
    }
    if skill_version is None:
        return detail
    if skill_version_id in path or len(path) >= 8:
        detail["dependency_cycle_or_depth_limit"] = True
        return detail

    next_path = (*path, skill_version_id)
    if skill_version.skill_type == "sequence_workflow":
        detail["transitive_dependencies"] = [
            _skill_dependency_detail(db, step, path=next_path)
            for step in definition.get("steps", [])
            if isinstance(step, dict)
        ]
    elif skill_version.skill_type == "agent_delegate":
        detail["delegate_target"] = _delegate_target_detail(
            db,
            str(definition.get("agent_code") or ""),
        )
    return detail


def _release_approval_detail(db: Session, row: ApprovalItem) -> dict[str, Any]:
    request = _load_json(row.request_json, {})
    version = db.get(CustomAgentVersion, int(request.get("agent_version_id") or 0))
    agent = db.get(CustomAgent, version.agent_id) if version else None
    previous = (
        db.get(CustomAgentVersion, agent.current_published_version_id)
        if agent and agent.current_published_version_id
        else None
    )
    from app.services import agent_studio_service

    manifest = agent_studio_service._manifest(db, version) if version else {"skills": []}
    dependencies: list[dict[str, Any]] = []
    requested_capabilities: set[str] = set()
    for dependency in manifest.get("skills", []):
        detail = _skill_dependency_detail(db, dependency)
        requested_capabilities.update(str(item) for item in detail.get("requested_capabilities", []))
        dependencies.append(detail)
    current_authoring = _version_authoring(version)
    previous_authoring = _version_authoring(previous)
    return {
        "id": row.id,
        "title": row.title,
        "status": row.status,
        "resource": row.resource,
        "risk_level": row.risk_level,
        "decision": row.decision,
        "decision_reason": row.decision_reason,
        "request": request,
        "agent": {
            "id": agent.id,
            "code": agent.code,
            "name": agent.name,
            "description": agent.description or "",
            "owner_id": agent.owner_id,
            "status": agent.status,
            "is_enabled": bool(agent.is_enabled),
        } if agent else None,
        "current_authoring": current_authoring,
        "previous_authoring": previous_authoring,
        "changes": {
            "kind": "initial" if previous is None and version is not None else "update",
            "prompt_changed": bool(version and (previous is None or previous.prompt != version.prompt)),
            "review_focus_changed": bool(
                version and (previous is None or previous.review_focus != version.review_focus)
            ),
            "model_config_changed": bool(
                version and (previous is None or previous.model_config_json != version.model_config_json)
            ),
            "from_version": previous.version_number if previous else None,
            "to_version": version.version_number if version else None,
        },
        "test_evidence": _load_json(version.test_evidence_json, {}) if version else {},
        "test_evidence_kind": "static_contract",
        "dependencies": dependencies,
        "risk": {
            "level": row.risk_level,
            "requested_capabilities": sorted(requested_capabilities),
        },
    }


def _release_target_snapshot(db: Session, row: ApprovalItem) -> str:
    # 快照绑定管理员实际看到的完整发布包：Agent 编写内容、
    # binding.config、Skill 定义/能力/测试证据以及前一发布版本。
    payload = {
        "approval_update_time": row.update_time.isoformat() if row.update_time else None,
        "package": _release_approval_detail(db, row),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def admin_list_agent_release_approvals(
    db: Session,
    user: Optional[User],
    approval_id: Optional[int] = None,
    status: str = "pending",
    limit: int = 50,
    ctx: Optional[AgentContext] = None,
) -> AgentResult:
    """返回发布审批的完整可处理内容，而非只返回标题。"""
    if not _is_admin(db, user):
        return _deny()
    query = db.query(ApprovalItem).filter(ApprovalItem.action == "agent_package.publish")
    if approval_id is not None:
        query = query.filter(ApprovalItem.id == approval_id)
    elif status:
        query = query.filter(ApprovalItem.status == status)
    rows = query.order_by(ApprovalItem.id.desc()).limit(max(1, min(limit, 100))).all()
    if approval_id is not None and not rows:
        return AgentResult(success=False, error=f"Agent 发布审批 {approval_id} 不存在")
    return AgentResult(success=True, data=[_release_approval_detail(db, row) for row in rows])


def preview_agent_release_decision(
    db: Session,
    user: Optional[User],
    *,
    approval_id: int,
    decision: str,
    note: str = "",
) -> AgentResult:
    if not _is_admin(db, user):
        return _deny()
    if decision not in {"approve", "reject"}:
        return AgentResult(success=False, error="decision 必须是 approve 或 reject")
    permission = PermissionCode.AGENT_ASSET_PUBLISH if decision == "approve" else PermissionCode.AGENT_ASSET_APPROVE
    if not rbac_service.check_permission(db, user.id, permission):
        return AgentResult(success=False, error="当前管理员没有处理该发布审批的权限")
    row = db.get(ApprovalItem, approval_id)
    if row is None or row.action != "agent_package.publish":
        return AgentResult(success=False, error=f"Agent 发布审批 {approval_id} 不存在")
    if row.status != "pending":
        return AgentResult(success=False, error=f"Agent 发布审批 {approval_id} 当前状态为 {row.status}，不能重复处理")
    return AgentResult(success=True, data={
        "decision": decision,
        "note": note,
        "target_snapshot": _release_target_snapshot(db, row),
        "approval": _release_approval_detail(db, row),
    })


def admin_decide_agent_release(
    db: Session,
    user: Optional[User],
    *,
    approval_id: int,
    decision: str,
    note: str = "",
    expected_snapshot: str,
) -> AgentResult:
    """按审批时展示的目标快照批准或驳回发布。"""
    if not _is_admin(db, user):
        return _deny()
    if decision not in {"approve", "reject"}:
        return AgentResult(success=False, error="decision 必须是 approve 或 reject")
    permission = PermissionCode.AGENT_ASSET_PUBLISH if decision == "approve" else PermissionCode.AGENT_ASSET_APPROVE
    if not rbac_service.check_permission(db, user.id, permission):
        return AgentResult(success=False, error="当前管理员没有处理该发布审批的权限")
    row = (
        db.query(ApprovalItem)
        .filter(ApprovalItem.id == approval_id, ApprovalItem.action == "agent_package.publish")
        .with_for_update()
        .first()
    )
    if row is None:
        db.rollback()
        return AgentResult(success=False, error=f"Agent 发布审批 {approval_id} 不存在")
    if row.status != "pending":
        db.rollback()
        return AgentResult(success=False, error=f"Agent 发布审批 {approval_id} 当前状态为 {row.status}，不能重复处理")
    actual_snapshot = _release_target_snapshot(db, row)
    if not expected_snapshot or actual_snapshot != expected_snapshot:
        db.rollback()
        return AgentResult(success=False, error="发布审批内容在确认后已变化，已阻止执行，请重新查看并批准")
    try:
        row = approval_service.decide_item(
            db,
            user,
            approval_id,
            approve=decision == "approve",
            note=note,
        )
        release = (
            db.query(CustomAgentRelease)
            .filter(CustomAgentRelease.approval_id == approval_id)
            .first()
        ) if decision == "approve" else None
        return AgentResult(success=True, data={
            "approval_id": row.id,
            "status": row.status,
            "decision": decision,
            "release_id": release.id if release else None,
        })
    except Exception as exc:
        db.rollback()
        return AgentResult(success=False, error=str(exc))


def admin_system_status(db: Session, user: Optional[User],
                        ctx: Optional[AgentContext] = None) -> AgentResult:
    """服务器运行状态(管理员只读)。"""
    if not _is_admin(db, user):
        return _deny()
    return AgentResult(success=True, data=system_status())


# ──────────────────────── 写操作(Responses 审批后执行)───────────────────────


def _canonical_user_ids(user_ids: Sequence[int]) -> list[int]:
    values = [int(value) for value in user_ids]
    if not values or len(values) > 200:
        raise ValueError("user_ids 数量必须在 1 到 200 之间")
    if any(value <= 0 for value in values):
        raise ValueError("user_ids 只能包含正整数")
    if len(set(values)) != len(values):
        raise ValueError("user_ids 不能重复")
    return sorted(values)


def _serialize_user_target(target: User) -> dict[str, Any]:
    return {
        "id": target.id,
        "username": target.username,
        "nickname": target.nickname or "",
        "role": target.role,
        "status": target.status,
        "token_version": int(target.token_version or 0),
        "update_time": target.update_time.isoformat() if target.update_time else None,
    }


def _user_target_snapshot(targets: Sequence[User]) -> str:
    payload = [_serialize_user_target(target) for target in sorted(targets, key=lambda item: item.id)]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_delete_targets(
    db: Session,
    user: User,
    user_ids: Sequence[int],
    targets: Sequence[User],
) -> Optional[str]:
    found = {target.id for target in targets if target.status != -1}
    missing = [user_id for user_id in user_ids if user_id not in found]
    if missing:
        return f"以下用户不存在或已删除: {missing}"
    if user.id in found:
        return "不能删除当前登录的管理员账号"
    active_admin_ids = {
        row[0]
        for row in (
            db.query(User.id)
            .filter(User.role.in_(["admin", "super_admin"]), User.status == 1)
            .with_for_update()
            .all()
        )
    }
    selected_active_admins = active_admin_ids & found
    if selected_active_admins and not (active_admin_ids - found):
        return "批量操作会删除最后一个可用管理员账号"
    return None


def preview_delete_users(
    db: Session,
    user: Optional[User],
    user_ids: Sequence[int],
) -> AgentResult:
    """审批前解析精确 ID、验证保护规则并返回完整目标快照。"""
    if not _is_admin(db, user):
        return _deny()
    try:
        canonical_ids = _canonical_user_ids(user_ids)
    except (TypeError, ValueError) as exc:
        return AgentResult(success=False, error=str(exc))
    targets = (
        db.query(User)
        .filter(User.id.in_(canonical_ids))
        .order_by(User.id.asc())
        .all()
    )
    validation_error = _validate_delete_targets(db, user, canonical_ids, targets)
    if validation_error:
        return AgentResult(success=False, error=validation_error)
    serialized = [_serialize_user_target(target) for target in targets]
    return AgentResult(success=True, data={
        "count": len(serialized),
        "user_ids": canonical_ids,
        "targets": serialized,
        "target_snapshot": _user_target_snapshot(targets),
    })


def admin_delete_users(
    db: Session,
    user: Optional[User],
    user_ids: Sequence[int],
    *,
    expected_snapshot: str,
    ctx: Optional[AgentContext] = None,
    context: Optional[dict[str, Any]] = None,
) -> AgentResult:
    """批准后锁定全部目标，复核快照并在一个事务中软删除。"""
    if not _is_admin(db, user):
        return _deny()
    try:
        canonical_ids = _canonical_user_ids(user_ids)
    except (TypeError, ValueError) as exc:
        return AgentResult(success=False, error=str(exc))
    targets = (
        db.query(User)
        .filter(User.id.in_(canonical_ids))
        .order_by(User.id.asc())
        .with_for_update()
        .all()
    )
    validation_error = _validate_delete_targets(db, user, canonical_ids, targets)
    if validation_error:
        db.rollback()
        return AgentResult(success=False, error=validation_error)
    actual_snapshot = _user_target_snapshot(targets)
    if not expected_snapshot or actual_snapshot != expected_snapshot:
        db.rollback()
        return AgentResult(success=False, error="用户目标在确认后已变化，已阻止批量删除，请重新查询并批准")

    event_context = {
        **(context or {}),
        "user_ids": canonical_ids,
        "target_snapshot": actual_snapshot,
    }
    title = f"批量软删除 {len(canonical_ids)} 个用户（ID {canonical_ids[0]} 至 {canonical_ids[-1]}）"
    action = "user.delete_batch"
    resource = f"users:{hashlib.sha256(','.join(map(str, canonical_ids)).encode()).hexdigest()[:24]}"
    reserved = _execute_confirmed_write(
        db,
        user,
        title=title,
        action=action,
        resource=resource,
        context=event_context,
    )
    if not reserved.success or (reserved.data or {}).get("duplicate"):
        return reserved
    try:
        serialized_targets = [_serialize_user_target(target) for target in targets]
        for target in targets:
            target.status = -1
            target.token_version = int(target.token_version or 0) + 1
            audit_service.log(
                db,
                user,
                "admin_copilot.delete_user",
                target_type="user",
                target_id=str(target.id),
                detail=f"批量删除用户「{target.username}」；batch={resource}",
                commit=False,
            )
        call = reserved.data["call"]
        call.status = "success"
        call.output_summary = f"已原子软删除 {len(targets)} 个账号"
        tool_call_id = call.id
        db.commit()
    except Exception as exc:
        return _fail_admin_write(
            db,
            user,
            event_context,
            action=action,
            resource=resource,
            title=title,
            exc=exc,
        )
    _complete_admin_write(user, event_context, action=action, resource=resource, title=title)
    return AgentResult(success=True, data={
        "tool_call_id": tool_call_id,
        "title": title,
        "deleted_count": len(targets),
        "user_ids": canonical_ids,
        "targets": serialized_targets,
    })

def _execute_confirmed_write(
    db: Session,
    user: User,
    *,
    title: str,
    action: str,
    resource: str,
    context: dict[str, Any],
) -> AgentResult:
    """在数据库唯一请求日志预占成功后执行，避免多 worker 重复写入。"""
    request_id = str(context.get("copilot_request_id") or "")
    if not request_id:
        return AgentResult(success=False, error="缺少确认请求标识")
    _emit_admin_write_event(
        AgentEventType.DISPATCH, user, context,
        action=action, resource=resource, message=f"管理操作已确认：{title}",
    )
    from app.models.agent_governance import ToolCallLog
    call = ToolCallLog(
        agent_code="manager", tool_code="admin_copilot", action=action, resource=resource,
        status="pending", risk_level="critical" if action.startswith("user.delete") else "high",
        decision="confirmed", input_summary=title, copilot_request_id=request_id,
    )
    db.add(call)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        from app.models.agent_governance import ToolCallLog
        existing = db.query(ToolCallLog).filter(ToolCallLog.copilot_request_id == request_id).first()
        if not existing:
            raise
        _emit_admin_write_event(
            AgentEventType.COMPLETE, user, context,
            action=action, resource=resource, message=f"管理操作已执行过：{title}",
        )
        return AgentResult(success=True, data={
            "duplicate": True, "tool_call_id": existing.id, "title": existing.input_summary,
        })
    _emit_admin_write_event(
        AgentEventType.PROGRESS, user, context,
        action=action, resource=resource, message=f"正在执行管理操作：{title}",
    )
    return AgentResult(success=True, data={"tool_call_id": call.id, "title": title, "call": call})


def _complete_admin_write(
    user: User,
    context: dict[str, Any],
    *,
    action: str,
    resource: str,
    title: str,
) -> None:
    _emit_admin_write_event(
        AgentEventType.COMPLETE, user, context,
        action=action, resource=resource, message=f"管理操作完成：{title}",
    )


def _fail_admin_write(
    db: Session,
    user: User,
    context: dict[str, Any],
    *,
    action: str,
    resource: str,
    title: str,
    exc: Exception,
) -> AgentResult:
    db.rollback()
    _emit_admin_write_event(
        AgentEventType.FAILED, user, context,
        action=action, resource=resource, message=f"管理操作失败：{title}",
    )
    return AgentResult(success=False, error=str(exc))


def admin_set_user_role(db: Session, user: Optional[User], user_id: int, role: str,
                        ctx: Optional[AgentContext] = None,
                        context: Optional[dict[str, Any]] = None) -> AgentResult:
    """确认后直接修改用户角色，并写入审计。"""
    if not _is_admin(db, user):
        return _deny()
    if role not in ("admin", "user", "reviewer"):
        return AgentResult(success=False, error=f"非法角色: {role}")
    target = db.get(User, user_id)
    if not target:
        return AgentResult(success=False, error=f"用户 {user_id} 不存在")
    event_context = {**(context or {}), "user_id": user_id, "role": role}
    title = f"将用户「{target.username}」角色改为 {role}"
    action = "user.set_role"
    resource = f"user:{user_id}"
    reserved = _execute_confirmed_write(
        db, user,
        title=title, action=action, resource=resource, context=event_context,
    )
    if not reserved.success or (reserved.data or {}).get("duplicate"):
        return reserved
    try:
        user_service.set_role(db, user_id, role, admin_id=user.id, commit=False)
        audit_service.log(
            db,
            user,
            "admin_copilot.set_user_role",
            target_type="user",
            target_id=str(user_id),
            detail=title,
            commit=False,
        )
        reserved.data["call"].status = "success"
        reserved.data["call"].output_summary = "角色已更新"
        db.commit()
    except Exception as exc:
        return _fail_admin_write(db, user, event_context, action=action, resource=resource, title=title, exc=exc)
    _complete_admin_write(user, event_context, action=action, resource=resource, title=title)
    return reserved


def admin_delete_user(db: Session, user: Optional[User], user_id: int,
                      ctx: Optional[AgentContext] = None,
                      context: Optional[dict[str, Any]] = None) -> AgentResult:
    """确认后直接软删除用户，并写入审计。"""
    if not _is_admin(db, user):
        return _deny()
    target = db.get(User, user_id)
    if not target:
        return AgentResult(success=False, error=f"用户 {user_id} 不存在")
    event_context = {**(context or {}), "user_id": user_id}
    title = f"删除用户「{target.username}」"
    action = "user.delete"
    resource = f"user:{user_id}"
    reserved = _execute_confirmed_write(
        db, user,
        title=title, action=action, resource=resource, context=event_context,
    )
    if not reserved.success or (reserved.data or {}).get("duplicate"):
        return reserved
    try:
        user_service.delete_user(db, user_id, admin_id=user.id, commit=False)
        audit_service.log(
            db,
            user,
            "admin_copilot.delete_user",
            target_type="user",
            target_id=str(user_id),
            detail=title,
            commit=False,
        )
        reserved.data["call"].status = "success"
        reserved.data["call"].output_summary = "账号已软删除"
        db.commit()
    except Exception as exc:
        return _fail_admin_write(db, user, event_context, action=action, resource=resource, title=title, exc=exc)
    _complete_admin_write(user, event_context, action=action, resource=resource, title=title)
    return reserved


def admin_toggle_agent(db: Session, user: Optional[User], agent_code: str, enable: bool,
                       ctx: Optional[AgentContext] = None,
                       context: Optional[dict[str, Any]] = None) -> AgentResult:
    """确认后直接启停 Agent，并写入审计。"""
    if not _is_admin(db, user):
        return _deny()
    action_text = "启用" if enable else "停用"
    try:
        agent_governance_service.get_profile(db, agent_code)
    except Exception as exc:
        return AgentResult(success=False, error=str(exc))
    event_context = {**(context or {}), "agent_code": agent_code, "enable": enable}
    title = f"{action_text} Agent「{agent_code}」"
    action = "agent.toggle"
    resource = f"agent:{agent_code}"
    reserved = _execute_confirmed_write(
        db, user,
        title=title, action=action, resource=resource, context=event_context,
    )
    if not reserved.success or (reserved.data or {}).get("duplicate"):
        return reserved
    try:
        agent_governance_service.update_profile(
            db,
            agent_code,
            {"is_enabled": 1 if enable else 0, "status": "idle" if enable else "disabled"},
            commit=False,
        )
        audit_service.log(
            db,
            user,
            "admin_copilot.toggle_agent",
            target_type="agent",
            target_id=agent_code,
            detail=title,
            commit=False,
        )
        reserved.data["call"].status = "success"
        reserved.data["call"].output_summary = "Agent 状态已更新"
        db.commit()
    except Exception as exc:
        return _fail_admin_write(db, user, event_context, action=action, resource=resource, title=title, exc=exc)
    _complete_admin_write(user, event_context, action=action, resource=resource, title=title)
    return reserved
