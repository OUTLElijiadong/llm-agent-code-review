"""Fenced execution primitives for enterprise registered-Agent teams."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, PaymentRequiredError, ValidationError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_agent_team import (
    EnterpriseAgentTeam,
    EnterpriseAgentTeamApproval,
    EnterpriseAgentTeamEvent,
    EnterpriseAgentTeamMember,
    EnterpriseAgentTeamTask,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import billing_ledger_service as ledger
from app.services import billing_metered_call, enterprise_chat_service, enterprise_org_service
from app.services import enterprise_assistant_service as assistant_service

_LEASE_SECONDS = 300
_MAX_LEASE_SECONDS = 900
_MAX_RESULT_BYTES = 200_000
_MAX_CALL_INDEX = 100
_ACTIVE_PARENT_STATUSES = {"running", "awaiting_approval"}
_ACTIVE_RUNTIME_STATUSES = {"running", "approving"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConflictError(f"企业 Agent 团队{label}快照损坏") from exc
    if not isinstance(value, dict):
        raise ConflictError(f"企业 Agent 团队{label}快照格式无效")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _lock_live_context(
    db: Session,
    *,
    enterprise_id: int,
    task_id: int,
    lock_company: bool = True,
    require_active_billing: bool = True,
    require_project_execution: bool = True,
    require_security_scan_grant: bool = False,
) -> dict[str, Any]:
    locator = (
        db.query(EnterpriseAgentTeamTask.team_id, EnterpriseAgentTeamTask.member_id)
        .join(
            EnterpriseAgentTeam,
            EnterpriseAgentTeam.id == EnterpriseAgentTeamTask.team_id,
        )
        .filter(
            EnterpriseAgentTeamTask.id == int(task_id),
            EnterpriseAgentTeam.enterprise_id == int(enterprise_id),
        )
        .first()
    )
    if locator is None:
        raise NotFoundError("企业 Agent 团队任务不存在")
    team_id = int(locator.team_id)
    team_locator = (
        db.query(EnterpriseAgentTeam)
        .filter_by(
            id=team_id,
            enterprise_id=int(enterprise_id),
        )
        .first()
    )
    if team_locator is None:
        raise NotFoundError("企业 Agent 团队不存在")

    # Parent recovery and cancellation already lock run/state before the
    # company. Acquire the same rows first here: taking membership before the
    # company can deadlock with organization writers, while taking company
    # before run can deadlock with recovery.
    run = (
        db.query(EnterpriseAssistantRun)
        .filter_by(
            id=int(team_locator.assistant_run_id),
            enterprise_id=int(enterprise_id),
            conversation_id=int(team_locator.conversation_id),
            initiator_user_id=int(team_locator.initiator_user_id),
            source_message_id=int(team_locator.source_message_id),
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if run is None:
        raise ForbiddenError("企业 Agent 团队父运行身份不匹配")
    runtime = (
        db.query(EnterpriseAssistantRuntimeState)
        .filter_by(
            assistant_run_id=int(run.id),
            enterprise_id=int(enterprise_id),
            conversation_id=int(team_locator.conversation_id),
            initiator_user_id=int(team_locator.initiator_user_id),
            source_message_id=int(team_locator.source_message_id),
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if runtime is None:
        raise ForbiddenError("企业 Agent 团队父运行授权状态不存在")

    company = enterprise_org_service._company(db, int(enterprise_id), lock=lock_company)
    # In the real path the role snapshot locks the company. Do it before
    # membership rows, after the parent locks, to match organization writers
    # and parent recovery without weakening the existing authorization fence.
    from app.services import enterprise_assistant_runtime_service as parent_runtime

    if require_active_billing:
        parent_runtime._live_role_snapshot(db, runtime)
    else:
        parent_runtime._live_role_snapshot(db, runtime, require_billing_access=False)
    if require_active_billing and (company.status != "active" or company.billing_suspended):
        raise ForbiddenError("企业已暂停或账单冻结，禁止继续执行 Agent 团队")
    enterprise_org_service.require_enterprise_member(
        db,
        int(enterprise_id),
        int(team_locator.initiator_user_id),
        lock=True,
        require_billing_access=require_active_billing,
    )
    room = enterprise_chat_service.get_conversation(
        db,
        int(enterprise_id),
        int(team_locator.initiator_user_id),
        int(team_locator.conversation_id),
        require_billing_access=require_active_billing,
    )
    if (
        int(room.enterprise_id) != int(enterprise_id)
        or int(room.id) != int(team_locator.conversation_id)
        or room.group_id != team_locator.group_id
    ):
        raise ForbiddenError("企业 Agent 团队原会话或小组范围已变化")
    if room.kind == "group" and room.group_id is not None:
        enterprise_org_service.require_group_member(
            db,
            int(enterprise_id),
            int(room.group_id),
            int(team_locator.initiator_user_id),
            allow_admin=False,
            lock=True,
            shared_group_lock=True,
            require_billing_access=require_active_billing,
        )

    team = (
        db.query(EnterpriseAgentTeam)
        .filter_by(
            id=team_id,
            enterprise_id=int(enterprise_id),
        )
        .populate_existing()
        .with_for_update()
        .one()
    )
    if (
        team.payer_owner_type != "enterprise"
        or int(team.payer_owner_id) != int(enterprise_id)
        or team.conversation_id != room.id
        or team.group_id != room.group_id
        or team.group_id != run.group_id
        or team.group_id != runtime.group_id
        or team.permission_snapshot_sha256 != runtime.permission_snapshot_sha256
    ):
        raise ForbiddenError("企业 Agent 团队的付款方或会话快照不匹配")
    member = (
        db.query(EnterpriseAgentTeamMember)
        .filter_by(
            id=int(locator.member_id),
            team_id=team_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    task = (
        db.query(EnterpriseAgentTeamTask)
        .filter_by(
            id=int(task_id),
            team_id=team_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if task is None or member is None:
        raise NotFoundError("企业 Agent 团队任务成员不存在")
    if member.status not in {"created", "queued", "running", "completed"}:
        raise ConflictError("企业 Agent 团队成员当前不可执行")
    if require_security_scan_grant:
        if member.address != "agent:security_sentinel":
            raise ConflictError("安全扫描权限只能用于安全哨兵团队任务")
        from app.core.permission_codes import PermissionCode
        from app.services import rbac_service

        if not rbac_service.check_permission_grant(
            db,
            int(team.initiator_user_id),
            PermissionCode.SECURITY_SCAN,
        ):
            raise ForbiddenError("安全哨兵任务提交报告前已撤销 security:scan 授权")

    parent_active = runtime.status in _ACTIVE_RUNTIME_STATUSES and run.status in _ACTIVE_PARENT_STATUSES
    parent_completed = runtime.status == "completed" and run.status == "completed"
    if (
        int(runtime.lease_generation) != int(team.parent_lease_generation)
        or runtime.stop_requested
        or not (parent_active or parent_completed)
    ):
        raise ForbiddenError("企业小菱父运行已停止、过期或被新授权代次替代")
    # A team created by a valid tool call must outlive the short-lived parent
    # Responses lease. A completed parent is an immutable authority snapshot;
    # cancellation/supersession still changes its durable status/generation and
    # is checked above and again before every provider send.
    if parent_active and (runtime.lease_expires_at is None or runtime.lease_expires_at <= _now()):
        raise ForbiddenError("企业小菱父运行租约已过期")

    plan = _json_object(team.plan_snapshot_json, label="执行计划")
    permission = _json_object(team.permission_snapshot_json, label="权限")
    capabilities = _json_object(member.capabilities_json, label="Agent 能力")
    if _digest(plan) != team.plan_sha256 or _digest(permission) != team.permission_snapshot_sha256:
        raise ForbiddenError("企业 Agent 团队计划或权限快照摘要不匹配")
    plan_project_id = plan.get("project_id")
    if plan_project_id is not None and (type(plan_project_id) is not int or plan_project_id <= 0):
        raise ForbiddenError("企业 Agent 团队项目范围快照无效")
    if (int(plan_project_id) if plan_project_id is not None else None) != (
        int(team.project_id) if team.project_id is not None else None
    ):
        raise ForbiddenError("企业 Agent 团队项目范围与计划快照不一致")
    project_binding = None
    if team.project_id is not None and require_project_execution:
        from app.models.enterprise_project import EnterpriseProject
        from app.services.enterprise_project_policy import require_enterprise_project_access

        project_binding = (
            db.query(EnterpriseProject)
            .filter_by(
                enterprise_id=int(enterprise_id),
                project_id=int(team.project_id),
                status="active",
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if project_binding is None:
            raise ForbiddenError("企业 Agent 团队绑定项目已失效")
        if room.kind == "group" and int(room.group_id or 0) != int(project_binding.group_id):
            raise ForbiddenError("企业 Agent 团队项目不属于当前会话责任小组")
        require_enterprise_project_access(
            db,
            int(enterprise_id),
            int(team.project_id),
            int(team.initiator_user_id),
            "execute",
            require_billing_execution=require_active_billing,
        )
    if _digest(capabilities) != member.snapshot_sha256:
        raise ForbiddenError("企业 Agent 成员能力快照摘要不匹配")

    context = {
        "company": company,
        "room": room,
        "run": run,
        "runtime": runtime,
        "team": team,
        "member": member,
        "task": task,
        "plan": plan,
        "project_binding": project_binding,
        "capabilities": capabilities,
        "require_active_billing": require_active_billing,
        "lock_company": lock_company,
    }
    if require_active_billing:
        _require_active_billing(db, context)
    return context


def _require_active_billing(db: Session, context: dict[str, Any]) -> None:
    """Gate new team work on the current enterprise subscription and quota."""
    company, team = context["company"], context["team"]
    if company.status != "active" or company.billing_suspended:
        raise ForbiddenError("企业已暂停或账单冻结，禁止继续执行 Agent 团队")
    assistant_service._require_subscription(db, int(team.enterprise_id))
    from app.services import enterprise_assistant_billing_group as billing_group

    billing_group_id = billing_group.validate(db, context["run"], lock_company=context["lock_company"])
    assistant_service._require_active_limit(
        db,
        enterprise_id=int(team.enterprise_id),
        group_id=billing_group_id,
        user_id=int(team.initiator_user_id),
    )


def _reservation_prefix(team: EnterpriseAgentTeam, task: EnterpriseAgentTeamTask, attempt: int) -> str:
    return f"ent-team:{team.id}:{task.id}:{attempt}:"


def _reservation_key(team: EnterpriseAgentTeam, task: EnterpriseAgentTeamTask, attempt: int, call_index: int) -> str:
    return f"{_reservation_prefix(team, task, attempt)}{call_index}"


def _attempt_reservations(db: Session, team: EnterpriseAgentTeam, task: EnterpriseAgentTeamTask, attempt: int):
    from app.services import enterprise_assistant_billing_group as billing_group

    run = db.query(EnterpriseAssistantRun).filter_by(
        id=int(team.assistant_run_id), enterprise_id=int(team.enterprise_id), conversation_id=int(team.conversation_id),
    ).first()
    if run is None:
        raise ForbiddenError("企业 Agent 团队父运行不存在")
    expected_group_id = billing_group.frozen(run)
    rows = (
        db.query(BillingReservation)
        .filter(
            BillingReservation.request_key.like(f"{_reservation_prefix(team, task, attempt)}%"),
            BillingReservation.enterprise_id == team.enterprise_id,
            BillingReservation.user_id == team.initiator_user_id,
        )
        .order_by(BillingReservation.id)
        .all()
    )
    if any(row.group_id != expected_group_id for row in rows):
        raise ConflictError("企业 Agent 团队账单归属不匹配，禁止重放")
    return rows


def _has_request_reservation(reservations) -> bool:
    """A released/not-sent hold proves no request was issued for that call key."""
    return any(row.status in {"reserved", "unknown", "settled"} for row in reservations)


def _team_reservations(db: Session, team: EnterpriseAgentTeam):
    return (
        db.query(BillingReservation)
        .filter(
            BillingReservation.request_key.like(f"ent-team:{team.id}:%"),
            BillingReservation.enterprise_id == team.enterprise_id,
        )
        .order_by(BillingReservation.id)
        .with_for_update()
        .all()
    )


def _append_event(
    db: Session,
    context: Mapping[str, Any],
    *,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    correlation_id: str = "",
    detail: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
) -> None:
    team = context["team"]
    task = context.get("task")
    member = context.get("member")
    db.add(
        EnterpriseAgentTeamEvent(
            team_id=int(team.id),
            task_id=int(task.id) if task is not None else None,
            member_id=int(member.id) if member is not None else None,
            enterprise_id=int(team.enterprise_id),
            conversation_id=int(team.conversation_id),
            actor_user_id=actor_user_id,
            correlation_id=correlation_id[:120],
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            actor_address=str(member.address)[:200] if member is not None else "system",
            detail_json=_canonical_json(detail or {}),
        )
    )


def _require_approved_plan(db: Session, context: Mapping[str, Any]) -> None:
    team, task, plan, capabilities = (
        context["team"],
        context["task"],
        context["plan"],
        context["capabilities"],
    )
    risk = plan.get("risk_level", "low")
    if risk not in {"low", "medium", "high"} or type(plan.get("requires_approval", False)) is not bool:
        raise ForbiddenError("企业 Agent 团队风险计划字段无效")
    requires_approval = bool(plan.get("requires_approval")) or risk in {"medium", "high"}
    if not requires_approval:
        allowed_modes = {"model_only", "prompt_only"}
        if capabilities.get("agent_code") == "security_sentinel":
            allowed_modes.add("read_only_project_scan")
            if team.project_id is None:
                raise ForbiddenError("无审批安全扫描必须绑定企业项目")
        if (
            capabilities.get("execution_mode") not in allowed_modes
            or capabilities.get("tools", []) not in ([], None)
            or capabilities.get("external_effects", False) is not False
        ):
            raise ForbiddenError("无审批任务只能运行无工具、无外部副作用的模型步骤")
        return

    approval = (
        db.query(EnterpriseAgentTeamApproval)
        .filter_by(
            team_id=int(team.id),
            task_id=int(task.id),
            enterprise_id=int(team.enterprise_id),
            conversation_id=int(team.conversation_id),
            group_id=team.group_id,
            initiator_user_id=int(team.initiator_user_id),
            plan_sha256=str(team.plan_sha256),
            status="approved",
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if (
        approval is None
        or task.approval_sha256 != team.plan_sha256
        or task.approved_by_user_id != approval.decided_by_user_id
        or approval.decided_by_user_id in (None, team.initiator_user_id)
        or approval.requested_by_user_id == approval.decided_by_user_id
    ):
        raise ForbiddenError("企業 Agent 團隊任務尚無綁定當前計劃的獨立審批")
    approval_plan = _json_object(approval.plan_json, label="审批计划")
    if _digest(approval_plan) != approval.plan_sha256:
        raise ForbiddenError("审批计划摘要不匹配")
    if approval.required_permission_code == "enterprise_admin":
        enterprise_org_service.require_enterprise_admin(
            db,
            int(team.enterprise_id),
            int(approval.decided_by_user_id),
            lock=True,
            require_billing_access=bool(context.get("require_active_billing", True)),
        )
    elif approval.required_permission_code == "group_manager" and team.group_id is not None:
        enterprise_org_service.require_group_manager(
            db,
            int(team.enterprise_id),
            int(team.group_id),
            int(approval.decided_by_user_id),
            allow_admin=True,
            lock=True,
            require_billing_access=bool(context.get("require_active_billing", True)),
        )
    else:
        raise ForbiddenError("企业 Agent 团队审批权限类型不受支持")


def _check_lease(context: Mapping[str, Any], *, lease_token: str, lease_generation: int, now: datetime) -> None:
    team, task = context["team"], context["task"]
    if (
        team.stop_requested
        or team.status in {"cancelled", "failed", "suppressed", "completed"}
        or task.status != "running"
        or task.lease_token != lease_token
        or int(task.lease_generation) != int(lease_generation)
        or task.lease_expires_at is None
        or task.lease_expires_at <= now
    ):
        raise ConflictError("企业 Agent 团队任务租约已过期或被取消")
    _require_approved_plan_from_context(context)


def _check_team_deadline(context: Mapping[str, Any], *, now: datetime) -> None:
    deadline_at = context["team"].deadline_at
    if deadline_at is not None and deadline_at <= now:
        raise ForbiddenError("企业 Agent 团队已超过截止时间，禁止发送新的模型请求")


def _require_approved_plan_from_context(context: Mapping[str, Any]) -> None:
    # The approval row is loaded under the transaction by _verify_approval_for_live_context.
    if not context.get("plan_authorized"):
        raise ForbiddenError("企业 Agent 团队任务计划未通过当前授权检查")


def _verify_approval_for_live_context(db: Session, context: dict[str, Any]) -> None:
    _require_approved_plan(db, context)
    context["plan_authorized"] = True


def _fail_unstarted_tasks_after_deadline(
    db: Session, context: dict[str, Any], *, now: datetime,
) -> None:
    """Close work that never started while allowing valid in-flight leases to settle."""
    team = context["team"]
    rows = (
        db.query(EnterpriseAgentTeamTask)
        .filter(
            EnterpriseAgentTeamTask.team_id == int(team.id),
            EnterpriseAgentTeamTask.status.in_({"queued", "retryable"}),
        )
        .with_for_update()
        .all()
    )
    for task in rows:
        old_status = task.status
        task.status = "failed"
        task.completed_at = now
        task.lease_token = None
        task.lease_expires_at = None
        task.next_attempt_at = None
        _append_event(
            db,
            {**context, "task": task},
            event_type="task.deadline_expired",
            from_status=old_status,
            to_status="failed",
            detail={"reason": "team_deadline_expired"},
        )
    _update_team_status(db, context, now=now)


def _claim_task_in_tx(
    db: Session, *, enterprise_id: int, task_id: int, now: datetime, lease_seconds: int
) -> dict[str, Any] | None:
    context = _lock_live_context(
        db,
        enterprise_id=enterprise_id,
        task_id=task_id,
        require_active_billing=False,
    )
    team, task, member = context["team"], context["task"], context["member"]
    if team.stop_requested or team.status not in {"queued", "running"}:
        return None
    deadline_expired = team.deadline_at is not None and team.deadline_at <= now
    if task.status == "running":
        if task.lease_expires_at is not None and task.lease_expires_at > now:
            if deadline_expired:
                _fail_unstarted_tasks_after_deadline(db, context, now=now)
            return None
        reservations = _attempt_reservations(db, team, task, int(task.attempt_count))
        old = task.status
        if _has_request_reservation(reservations):
            task.status = "awaiting_reconciliation"
            task.lease_token = None
            task.lease_expires_at = None
            team.status = "awaiting_reconciliation"
            _append_event(
                db,
                context,
                event_type="task.lease_expired_with_billing",
                from_status=old,
                to_status=task.status,
                detail={"reservation_count": len(reservations)},
            )
            if deadline_expired:
                _fail_unstarted_tasks_after_deadline(db, context, now=now)
            return {"status": task.status, "team_id": int(team.id), "task_id": int(task.id)}
        task.status = (
            "failed"
            if deadline_expired or int(task.attempt_count) >= int(task.max_attempts)
            else "queued"
        )
        task.lease_token = None
        task.lease_expires_at = None
        _append_event(
            db,
            context,
            event_type="task.deadline_expired" if deadline_expired else "task.lease_expired_without_billing",
            from_status=old,
            to_status=task.status,
            detail={"reason": "team_deadline_expired"} if deadline_expired else None,
        )
        if task.status == "failed":
            if deadline_expired:
                _fail_unstarted_tasks_after_deadline(db, context, now=now)
            else:
                _update_team_status(db, context, now=now)
            return {"status": "failed", "team_id": int(team.id), "task_id": int(task.id)}
    if deadline_expired:
        _fail_unstarted_tasks_after_deadline(db, context, now=now)
        return None
    if task.status not in {"queued", "retryable"} or int(task.attempt_count) >= int(task.max_attempts):
        return None
    _require_active_billing(db, context)
    if task.next_attempt_at is not None and task.next_attempt_at > now:
        return None
    if team.deadline_at is not None and team.deadline_at <= now:
        return None
    dependency_keys = json.loads(task.dependency_keys_json or "[]")
    if (
        not isinstance(dependency_keys, list)
        or any(not isinstance(key, str) for key in dependency_keys)
        or len(set(dependency_keys)) != len(dependency_keys)
    ):
        raise ConflictError("企业 Agent 团队任务依赖快照无效")
    if dependency_keys:
        if task.task_key in dependency_keys:
            raise ConflictError("任务不能依赖自身")
        dependencies = (
            db.query(EnterpriseAgentTeamTask)
            .filter(
                EnterpriseAgentTeamTask.team_id == team.id,
                EnterpriseAgentTeamTask.task_key.in_(dependency_keys),
            )
            .all()
        )
        if len(dependencies) != len(dependency_keys):
            raise ConflictError("企业 Agent 团队任务引用了不存在的依赖")
        if any(row.status != "completed" for row in dependencies):
            return None
    _verify_approval_for_live_context(db, context)
    old_status = task.status
    task.status = "running"
    task.attempt_count = int(task.attempt_count) + 1
    task.lease_generation = int(task.lease_generation) + 1
    task.lease_token = secrets.token_urlsafe(32)
    task.lease_expires_at = now + timedelta(seconds=lease_seconds)
    task.started_at = task.started_at or now
    team.status = "running"
    team.started_at = team.started_at or now
    _append_event(
        db,
        context,
        event_type="task.claimed",
        from_status=old_status,
        to_status="running",
        correlation_id=f"lease:{task.lease_generation}",
        detail={"attempt": task.attempt_count, "lease_generation": task.lease_generation},
    )
    return {
        "status": "running",
        "team_id": int(team.id),
        "task_id": int(task.id),
        "member_id": int(member.id),
        "member_key": member.member_key,
        "member_address": member.address,
        "task_key": task.task_key,
        "title": task.title,
        "instructions": task.instructions,
        "input": json.loads(task.input_json or "{}"),
        "attempt": int(task.attempt_count),
        "lease_token": task.lease_token,
        "lease_generation": int(task.lease_generation),
        "lease_expires_at": task.lease_expires_at.isoformat(),
    }


def claim_task(
    session_factory,
    *,
    enterprise_id: int,
    task_id: int,
    now: datetime | None = None,
    lease_seconds: int = _LEASE_SECONDS,
) -> dict[str, Any] | None:
    if type(enterprise_id) is not int or enterprise_id <= 0 or type(task_id) is not int or task_id <= 0:
        raise ValidationError("企业 Agent 团队任务范围无效")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
        raise ValidationError("企业 Agent 团队租约长度无效")
    now = now or _now()
    with session_factory.begin() as db:
        return _claim_task_in_tx(db, enterprise_id=enterprise_id, task_id=task_id, now=now, lease_seconds=lease_seconds)


def claim_next_task(
    session_factory,
    *,
    enterprise_id: int,
    limit: int = 50,
    now: datetime | None = None,
    lease_seconds: int = _LEASE_SECONDS,
) -> dict[str, Any] | None:
    if type(enterprise_id) is not int or enterprise_id <= 0:
        raise ValidationError("企业 Agent 团队队列企业范围无效")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValidationError("企业 Agent 团队队列扫描数量无效")
    now = now or _now()
    with session_factory() as db:
        candidates = (
            db.query(EnterpriseAgentTeamTask.id)
            .join(
                EnterpriseAgentTeam,
                EnterpriseAgentTeam.id == EnterpriseAgentTeamTask.team_id,
            )
            .filter(
                EnterpriseAgentTeam.enterprise_id == int(enterprise_id),
                EnterpriseAgentTeam.status.in_({"queued", "running"}),
                EnterpriseAgentTeamTask.status.in_({"queued", "retryable", "running"}),
            )
            .order_by(EnterpriseAgentTeamTask.priority.desc(), EnterpriseAgentTeamTask.id.asc())
            .limit(limit)
            .all()
        )
        task_ids = [int(row.id) for row in candidates]
    for task_id in task_ids:
        result = claim_task(
            session_factory, enterprise_id=int(enterprise_id), task_id=task_id, now=now, lease_seconds=lease_seconds
        )
        if result is not None and result.get("status") == "running":
            return result
    return None


def renew_task_lease(
    session_factory,
    *,
    enterprise_id: int,
    task_id: int,
    lease_token: str,
    lease_generation: int,
    now: datetime | None = None,
    lease_seconds: int = _LEASE_SECONDS,
) -> datetime:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
        raise ValidationError("企业 Agent 团队租约长度无效")
    now = now or _now()
    with session_factory.begin() as db:
        context = _lock_live_context(db, enterprise_id=enterprise_id, task_id=task_id)
        _verify_approval_for_live_context(db, context)
        _check_lease(context, lease_token=lease_token, lease_generation=lease_generation, now=now)
        task = context["task"]
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return task.lease_expires_at


def _update_team_status(db: Session, context: dict[str, Any], *, now: datetime) -> None:
    team = context["team"]
    statuses = [row.status for row in db.query(EnterpriseAgentTeamTask.status).filter_by(team_id=team.id).all()]
    if statuses and all(status == "completed" for status in statuses):
        team.status = "completed"
        team.completed_at = now
    elif "awaiting_reconciliation" in statuses:
        team.status = "awaiting_reconciliation"
    elif statuses and all(status in {"completed", "failed", "cancelled", "suppressed"} for status in statuses):
        team.status = "failed" if "failed" in statuses else "cancelled"
        team.completed_at = now
    else:
        team.status = "running"


def _security_artifact_request_count(payload: dict[str, Any]) -> int:
    report = payload.get("report")
    compliance = report.get("compliance") if isinstance(report, dict) else None
    count = compliance.get("provider_request_count") if isinstance(compliance, dict) else None
    if type(count) is not int or count < 0:
        raise ValidationError("安全报告缺少可信的模型请求计数")
    return count


def complete_task(
    session_factory,
    *,
    enterprise_id: int,
    task_id: int,
    lease_token: str,
    lease_generation: int,
    result: dict[str, Any],
    security_artifact: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValidationError("企业 Agent 团队任务结果必须是对象")
    encoded = _canonical_json(result)
    if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise ValidationError("企业 Agent 团队任务结果超出上限")
    now = now or _now()
    with session_factory.begin() as db:
        context = _lock_live_context(
            db,
            enterprise_id=enterprise_id,
            task_id=task_id,
            require_active_billing=False,
            require_security_scan_grant=security_artifact is not None,
        )
        _verify_approval_for_live_context(db, context)
        _check_lease(context, lease_token=lease_token, lease_generation=lease_generation, now=now)
        task, team = context["task"], context["team"]
        reservations = _attempt_reservations(db, team, task, int(task.attempt_count))
        if any(row.status != "settled" for row in reservations):
            raise PaymentRequiredError("企业 Agent 团队任务用量未全部结算，结果暂不可保存")
        if security_artifact is None and not reservations:
            raise PaymentRequiredError("企业 Agent 团队任务没有可核对的模型用量，结果暂不可保存")
        if security_artifact is not None:
            request_count = _security_artifact_request_count(security_artifact)
            if request_count != len(reservations):
                raise PaymentRequiredError("安全扫描模型请求数与企业账本预占不一致")
        artifact_reference = None
        if security_artifact is not None:
            from app.services.enterprise_security_artifact_service import create_artifact_in_transaction

            artifact_reference = create_artifact_in_transaction(
                db,
                team=team,
                task=task,
                lease_generation=int(lease_generation),
                payload=security_artifact,
                expected_coverage="complete",
            )
            result = {
                "status": "completed",
                "summary": artifact_reference["summary"],
                "security_artifact_id": artifact_reference["security_artifact_id"],
                "report_sha256": artifact_reference["report_sha256"],
                "coverage_status": artifact_reference["coverage_status"],
            }
            encoded = _canonical_json(result)
            if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
                raise ValidationError("企业 Agent 团队安全报告引用超出上限")
        old = task.status
        task.result_json = encoded
        task.status = "completed"
        task.completed_at = now
        task.lease_token = None
        task.lease_expires_at = None
        context["task"] = task
        _append_event(
            db,
            context,
            event_type="task.completed",
            from_status=old,
            to_status="completed",
            correlation_id=f"lease:{lease_generation}",
            detail={
                "reservation_count": len(reservations),
                **(
                    {
                        "security_artifact_id": artifact_reference["security_artifact_id"],
                        "report_sha256": artifact_reference["report_sha256"],
                    }
                    if artifact_reference is not None
                    else {}
                ),
            },
        )
        _update_team_status(db, context, now=now)
        return {
            "team_id": int(team.id),
            "task_id": int(task.id),
            "status": task.status,
            "team_status": team.status,
            **(
                {"security_artifact_id": artifact_reference["security_artifact_id"]}
                if artifact_reference is not None
                else {}
            ),
        }


def fail_task(
    session_factory,
    *,
    enterprise_id: int,
    task_id: int,
    lease_token: str,
    lease_generation: int,
    error_code: str,
    result: dict[str, Any] | None = None,
    retryable: bool | None = None,
    security_artifact: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    error_code = (error_code or "execution_failed")[:64]
    if retryable is not None and type(retryable) is not bool:
        raise ValidationError("企业 Agent 团队失败结果的重试标记无效")
    if result is not None:
        if not isinstance(result, dict):
            raise ValidationError("企业 Agent 团队失败结果必须是对象")
        encoded_result = _canonical_json(result)
        if len(encoded_result.encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValidationError("企业 Agent 团队失败结果超出上限")
    else:
        encoded_result = None
    with session_factory.begin() as db:
        context = _lock_live_context(
            db,
            enterprise_id=enterprise_id,
            task_id=task_id,
            require_active_billing=False,
            require_project_execution=security_artifact is not None,
            require_security_scan_grant=security_artifact is not None,
        )
        _verify_approval_for_live_context(db, context)
        _check_lease(context, lease_token=lease_token, lease_generation=lease_generation, now=now)
        task, team = context["task"], context["team"]
        old = task.status
        reservations = _attempt_reservations(db, team, task, int(task.attempt_count))
        artifact_reference = None
        if security_artifact is not None:
            if retryable is not False:
                raise ConflictError("仅终态失败任务可提交安全报告制品")
            if any(row.status != "settled" for row in reservations):
                raise PaymentRequiredError("企业团队任务用量未全部结算，安全报告暂不可保存")
            if _security_artifact_request_count(security_artifact) != len(reservations):
                raise PaymentRequiredError("安全扫描模型请求数与企业账本预占不一致")
            from app.services.enterprise_security_artifact_service import create_artifact_in_transaction

            artifact_reference = create_artifact_in_transaction(
                db,
                team=team,
                task=task,
                lease_generation=int(lease_generation),
                payload=security_artifact,
                expected_coverage="non_success",
            )
            encoded_result = _canonical_json(
                {
                    "status": "failed",
                    "summary": artifact_reference["summary"],
                    "security_artifact_id": artifact_reference["security_artifact_id"],
                    "report_sha256": artifact_reference["report_sha256"],
                    "coverage_status": artifact_reference["coverage_status"],
                    "termination_reason": artifact_reference["termination_reason"],
                }
            )
            if len(encoded_result.encode("utf-8")) > _MAX_RESULT_BYTES:
                raise ValidationError("企业 Agent 团队安全报告引用超出上限")
        try:
            previous_errors = json.loads(task.errors_json or "[]")
        except (TypeError, ValueError):
            previous_errors = []
        if not isinstance(previous_errors, list):
            previous_errors = []
        task.errors_json = _canonical_json(
            [*previous_errors[-19:], {"attempt": task.attempt_count, "code": error_code}],
        )
        if encoded_result is not None:
            task.result_json = encoded_result
        if any(row.status in {"reserved", "unknown"} for row in reservations):
            task.status = "awaiting_reconciliation"
            team.status = "awaiting_reconciliation"
        elif retryable is False:
            # A structured, deterministic failure is safe to close once every issued
            # request is settled. Keep unknown/reserved requests frozen above.
            task.status = "failed"
        elif _has_request_reservation(reservations):
            # A settled request followed by an unclassified failure may have lost its
            # result; preserve the existing manual-reconciliation boundary.
            task.status = "awaiting_reconciliation"
        else:
            task.status = "retryable" if int(task.attempt_count) < int(task.max_attempts) else "failed"
        task.lease_token = None
        task.lease_expires_at = None
        context["task"] = task
        _append_event(
            db,
            context,
            event_type="task.failed",
            from_status=old,
            to_status=task.status,
            correlation_id=f"lease:{lease_generation}",
            detail={
                "error_code": error_code,
                "reservation_count": len(reservations),
                **(
                    {
                        "security_artifact_id": artifact_reference["security_artifact_id"],
                        "report_sha256": artifact_reference["report_sha256"],
                    }
                    if artifact_reference is not None
                    else {}
                ),
            },
        )
        if task.status != "awaiting_reconciliation":
            _update_team_status(db, context, now=now)
        return {
            "team_id": int(team.id),
            "task_id": int(task.id),
            "status": task.status,
            "team_status": team.status,
            **(
                {"security_artifact_id": artifact_reference["security_artifact_id"]}
                if artifact_reference is not None
                else {}
            ),
        }


def retry_task(
    session_factory,
    *,
    enterprise_id: int,
    team_id: int,
    task_id: int,
    actor_user_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    with session_factory.begin() as db:
        context = _lock_live_context(db, enterprise_id=enterprise_id, task_id=task_id)
        team, task, room, run = context["team"], context["task"], context["room"], context["run"]
        if int(team.id) != int(team_id):
            raise NotFoundError("企业 Agent 团队任务不存在")
        from app.services import enterprise_assistant_runtime_service as parent_runtime

        parent_runtime._require_run_retry_controller(db, run, int(actor_user_id), room)
        if team.stop_requested or task.status != "retryable" or int(task.attempt_count) >= int(task.max_attempts):
            raise ConflictError("企业 Agent 团队任务当前不能显式重试")
        if _has_request_reservation(_attempt_reservations(db, team, task, int(task.attempt_count))):
            raise PaymentRequiredError("该任务已有模型费用记录；人工对账前禁止重试")
        _verify_approval_for_live_context(db, context)
        old = task.status
        task.status = "queued"
        task.next_attempt_at = now
        _append_event(
            db,
            context,
            event_type="task.retry_requested",
            from_status=old,
            to_status="queued",
            actor_user_id=int(actor_user_id),
        )
        team.status = "running"
        return {"team_id": int(team.id), "task_id": int(task.id), "status": task.status}


def cancel_team(
    session_factory,
    *,
    enterprise_id: int,
    team_id: int,
    actor_user_id: int,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    with session_factory.begin() as db:
        locator = (
            db.query(EnterpriseAgentTeam)
            .filter_by(
                id=int(team_id),
                enterprise_id=int(enterprise_id),
            )
            .first()
        )
        if locator is None:
            raise NotFoundError("企业 Agent 团队不存在")
        run = (
            db.query(EnterpriseAssistantRun)
            .filter_by(
                id=int(locator.assistant_run_id),
                enterprise_id=int(enterprise_id),
                conversation_id=int(locator.conversation_id),
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if run is None:
            raise NotFoundError("企业 Agent 团队不存在")
        db.query(EnterpriseAssistantRuntimeState).filter_by(
            assistant_run_id=int(run.id),
            enterprise_id=int(enterprise_id),
            conversation_id=int(locator.conversation_id),
        ).populate_existing().with_for_update().first()
        company = enterprise_org_service._company(db, int(enterprise_id), lock=True)
        if company.status not in {"active", "suspended"}:
            raise ForbiddenError("当前企业不可管理团队")
        from app.services import enterprise_assistant_runtime_service as parent_runtime

        room = parent_runtime._run_control_room(
            db,
            int(enterprise_id),
            int(actor_user_id),
            int(locator.conversation_id),
        )
        parent_runtime._require_run_controller(db, run, int(actor_user_id), room)
        team = (
            db.query(EnterpriseAgentTeam)
            .filter_by(
                id=int(team_id),
                enterprise_id=int(enterprise_id),
            )
            .populate_existing()
            .with_for_update()
            .one()
        )
        if team.status in {"cancelled", "completed", "failed", "suppressed"}:
            return {"team_id": int(team.id), "status": team.status}
        old = team.status
        team.stop_requested = True
        team.cancel_reason = str(reason).strip()[:500]
        team.status = "cancelled"
        team.lease_generation = int(team.lease_generation) + 1
        team.lease_expires_at = None
        team.completed_at = now
        for task in db.query(EnterpriseAgentTeamTask).filter_by(team_id=int(team.id)).with_for_update().all():
            if task.status not in {"completed", "failed", "cancelled", "suppressed"}:
                old_task_status = task.status
                task.status = "cancelled"
                task.lease_generation = int(task.lease_generation) + 1
                task.lease_token = None
                task.lease_expires_at = None
                db.add(
                    EnterpriseAgentTeamEvent(
                        team_id=int(team.id),
                        task_id=int(task.id),
                        member_id=int(task.member_id),
                        enterprise_id=int(team.enterprise_id),
                        conversation_id=int(team.conversation_id),
                        actor_user_id=int(actor_user_id),
                        correlation_id=f"cancel:{team.lease_generation}",
                        event_type="task.cancelled",
                        from_status=old_task_status,
                        to_status="cancelled",
                        actor_address="system",
                        detail_json=_canonical_json({"reason": team.cancel_reason}),
                    )
                )
        db.query(EnterpriseAgentTeamApproval).filter_by(team_id=int(team.id)).filter(
            EnterpriseAgentTeamApproval.status == "pending",
        ).update({"status": "cancelled", "decision_reason": team.cancel_reason}, synchronize_session=False)
        db.add(
            EnterpriseAgentTeamEvent(
                team_id=int(team.id),
                enterprise_id=int(team.enterprise_id),
                conversation_id=int(team.conversation_id),
                actor_user_id=int(actor_user_id),
                correlation_id=f"cancel:{team.lease_generation}",
                event_type="team.cancelled",
                from_status=old,
                to_status="cancelled",
                actor_address="user",
                detail_json=_canonical_json({"reason": team.cancel_reason}),
            )
        )
        return {"team_id": int(team.id), "status": team.status}


def metered_team_call(
    *,
    session_factory,
    enterprise_id: int,
    task_id: int,
    lease_token: str,
    lease_generation: int,
    call_index: int,
    model_name: str,
    max_input_tokens: int,
    max_output_tokens: int,
    provider_identity,
    send: Callable[[], tuple[Any, Mapping[str, Any]]],
    now: datetime | None = None,
) -> Any:
    """Make one provider request against only the frozen enterprise wallet scope."""
    if type(call_index) is not int or not 1 <= call_index <= _MAX_CALL_INDEX:
        raise ValidationError("企业 Agent 团队模型调用序号无效")
    now = now or _now()
    with session_factory.begin() as db:
        context = _lock_live_context(
            db,
            enterprise_id=enterprise_id,
            task_id=task_id,
            lock_company=False,
        )
        _verify_approval_for_live_context(db, context)
        _check_lease(context, lease_token=lease_token, lease_generation=lease_generation, now=now)
        _check_team_deadline(context, now=now)
        team, task = context["team"], context["task"]
        plan = context["plan"]
        allowed_models = plan.get("allowed_models")
        max_calls = plan.get("max_calls_per_task")
        max_input = plan.get("max_input_tokens_per_call")
        max_output = plan.get("max_output_tokens_per_call")
        max_team_credits = plan.get("max_team_credits_milli")
        if (
            not isinstance(allowed_models, list)
            or not allowed_models
            or any(model not in ledger.billable_model_names() for model in allowed_models)
            or model_name not in allowed_models
            or type(max_calls) is not int
            or not 1 <= max_calls <= _MAX_CALL_INDEX
            or call_index > max_calls
            or type(max_input) is not int
            or not 1 <= max_input <= 1_000_000
            or type(max_output) is not int
            or not 1 <= max_output <= 250_000
            or type(max_team_credits) is not int
            or not 1 <= max_team_credits <= 1_000_000_000
            or type(max_input_tokens) is not int
            or not 1 <= max_input_tokens <= max_input
            or type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= max_output
        ):
            raise ForbiddenError("企业 Agent 团队计划未设置有效的模型、Token 和费用上限")
        wallet = ledger.get_or_create_wallet(db, "enterprise", int(enterprise_id))
        request_key = _reservation_key(team, task, int(task.attempt_count), call_index)
        attempt = int(task.attempt_count)
        from app.services import enterprise_assistant_billing_group as billing_group

        group_id = billing_group.validate(db, context["run"], lock_company=False)
        initiator_id = int(team.initiator_user_id)
        team_id = int(team.id)
        conversation_id = int(team.conversation_id)
        member_id = int(context["member"].id)
        actor_address = str(context["member"].address)[:200]

    def billing_event(event_type: str, *, enforce_budget: bool = False):
        def write_event(db: Session, observed_key: str) -> None:
            if enforce_budget:
                # attempt_guard holds the team row lock in this transaction, so
                # concurrent provider calls share one serialized budget check.
                reservations = _team_reservations(db, team)
                total_milli = sum(
                    int(row.actual_milli if row.status == "settled" else row.requested_milli or 0)
                    for row in reservations
                    if row.status in {"reserved", "unknown", "settled"}
                )
                if total_milli > max_team_credits:
                    raise PaymentRequiredError("企业 Agent 团队费用预算已用尽；本次请求未发送")
            db.add(
                EnterpriseAgentTeamEvent(
                    team_id=team_id,
                    task_id=int(task_id),
                    member_id=member_id,
                    enterprise_id=int(enterprise_id),
                    conversation_id=conversation_id,
                    correlation_id=hashlib.sha256(observed_key.encode("utf-8")).hexdigest()[:64],
                    event_type=event_type,
                    actor_address=actor_address,
                    detail_json=_canonical_json(
                        {
                            "call_index": call_index,
                            "reservation_key_sha256": hashlib.sha256(observed_key.encode("utf-8")).hexdigest(),
                        }
                    ),
                )
            )

        return write_event

    def attempt_guard(db: Session, observed_key: str) -> None:
        live = _lock_live_context(
            db,
            enterprise_id=enterprise_id,
            task_id=task_id,
            lock_company=False,
        )
        _verify_approval_for_live_context(db, live)
        checked_at = _now()
        _check_lease(live, lease_token=lease_token, lease_generation=lease_generation, now=checked_at)
        _check_team_deadline(live, now=checked_at)
        current_task = live["task"]
        if (
            int(current_task.attempt_count) != attempt
            or _reservation_key(live["team"], current_task, attempt, call_index) != observed_key
        ):
            raise ConflictError("企业 Agent 团队请求键已变化")
        live["task"].lease_expires_at = _now() + timedelta(seconds=_LEASE_SECONDS)

    def send_guard(send: Callable, observed_key: str):
        send_started = False
        try:
            # Keep the same authorization rows locked until this one provider
            # request returns. A cancel/revoke operation therefore either wins
            # before send (and releases the hold) or waits for the active call.
            with session_factory.begin() as db:
                # The current role snapshot holds the company row, in addition
                # to member/group/run/task rows, until the provider returns.
                # This fences organization changes but serializes same-company
                # work; narrowing that lock requires a separate safe protocol.
                live = _lock_live_context(
                    db,
                    enterprise_id=enterprise_id,
                    task_id=task_id,
                    lock_company=False,
                )
                _verify_approval_for_live_context(db, live)
                checked_at = _now()
                _check_lease(live, lease_token=lease_token, lease_generation=lease_generation, now=checked_at)
                _check_team_deadline(live, now=checked_at)
                current_task = live["task"]
                if (
                    int(current_task.attempt_count) != attempt
                    or _reservation_key(live["team"], current_task, attempt, call_index) != observed_key
                ):
                    raise ConflictError("企业 Agent 团队请求键已变化")
                reservation = (
                    db.query(BillingReservation)
                    .filter_by(
                        request_key=observed_key,
                        enterprise_id=int(enterprise_id),
                        user_id=initiator_id,
                        group_id=group_id,
                        status="reserved",
                    )
                    .first()
                )
                if reservation is None:
                    raise ConflictError("企业 Agent 团队模型预占已失效，拒绝发送")
                current_task.lease_expires_at = _now() + timedelta(seconds=_MAX_LEASE_SECONDS)
                send_started = True
                return send()
        except BaseException as exc:
            if send_started:
                raise
            try:
                with session_factory.begin() as db:
                    ledger.release_not_sent(db, observed_key)
                    billing_event("billing.not_sent")(db, observed_key)
            except Exception as release_error:  # noqa: BLE001 - preserve the hold for reconciliation
                raise ledger.BillingError(
                    "供应商请求未发出，但企业预占未能安全释放；账单已保留待人工对账",
                ) from release_error
            raise billing_metered_call.MeteredRequestNotSent(
                "企业 Agent 运行在模型请求发出前已被取消或撤权；预占已释放",
            ) from exc

    return billing_metered_call.metered_call(
        session_factory=session_factory,
        request_key=request_key,
        model_name=model_name,
        user_id=initiator_id,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        send=send,
        provider_identity=provider_identity,
        enterprise_wallet_id=int(wallet.id),
        enterprise_id=int(enterprise_id),
        group_id=group_id,
        allow_personal=False,
        usage_kind="member",
        attempt_guard=attempt_guard,
        send_guard=send_guard,
        on_reserved=billing_event("billing.reserved", enforce_budget=True),
        on_unknown=billing_event("billing.unknown"),
        on_settled=billing_event("billing.settled"),
    )
