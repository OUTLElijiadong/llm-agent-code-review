"""Current-authorization queries for tenant-scoped enterprise Agent teams."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, PaymentRequiredError, ValidationError
from app.models.billing_ledger import BillingLimit, BillingWallet
from app.models.enterprise_agent_team import (
    EnterpriseAgentTeam,
    EnterpriseAgentTeamEvent,
    EnterpriseAgentTeamMember,
    EnterpriseAgentTeamTask,
)
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.project import Project
from app.services import enterprise_chat_service, enterprise_org_service

# security_sentinel's existing Agent Mesh handler reads persisted project/file/task
# records. Enterprise team plans intentionally accept only caller-supplied text and
# reject those resource identifiers, so exposing it here would queue a task that can
# never execute under the advertised contract.
_EXECUTABLE_AGENT_CODES = frozenset({"language_detector", "code_reviewer", "security_sentinel"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _contract_snapshot(code: str) -> tuple[Any, dict[str, Any]]:
    from app.agents.contracts import CONTRACTS
    from app.services.agent_mesh_dispatcher import dispatch_state

    if code not in _EXECUTABLE_AGENT_CODES:
        raise ValidationError("企业团队当前只开放语言识别、代码质量审查和项目安全审计 Agent")
    contract = CONTRACTS.get(code)
    if (
        contract is None
        or contract.execution_mode != "runtime"
        or contract.protected
        or dispatch_state(f"agent:{code}") != "executable"
    ):
        raise ValidationError("所选注册 Agent 当前没有受支持的运行时执行器")
    snapshot = {
        "agent_code": code,
        "contract": contract.governance_config(),
        "execution_mode": "read_only_project_scan" if code == "security_sentinel" else "model_only",
        "tools": [],
        "external_effects": False,
    }
    return contract, snapshot


def _validate_agent_input(code: str, value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Agent 任务输入必须是对象")
    if code == "language_detector":
        allowed = {"project_name", "description"}
        if not isinstance(value.get("project_name"), str) or not value["project_name"].strip():
            raise ValidationError("语言识别任务需要 project_name")
        if any(not isinstance(item, str) for item in value.values()):
            raise ValidationError("语言识别输入只接受文本字段")
    elif code == "security_sentinel":
        allowed = {"scan_mode", "top_n", "trace_dataflow"}
        scan_mode = value.get("scan_mode")
        if scan_mode is not None and (
            not isinstance(scan_mode, str) or scan_mode not in {"full", "static_full", "triage"}
        ):
            raise ValidationError("安全扫描 scan_mode 只能是 full、static_full 或 triage")
        top_n = value.get("top_n")
        if top_n is not None and (type(top_n) is not int or not 1 <= top_n <= 200):
            raise ValidationError("安全扫描 top_n 必须是 1 到 200 的整数")
        trace_dataflow = value.get("trace_dataflow")
        if trace_dataflow is not None and type(trace_dataflow) is not bool:
            raise ValidationError("安全扫描 trace_dataflow 必须是布尔值")
    else:
        allowed = {"code", "language", "file_name", "line_offset", "rules"}
        code_text = value.get("code")
        if not isinstance(code_text, str) or not code_text.strip():
            raise ValidationError("代码审查和安全分析任务需要明确的 code 文本")
        if len(code_text.encode("utf-8")) > 48_000:
            raise ValidationError("单个团队任务代码输入不能超过 48 KB")
        if "line_offset" in value and (type(value["line_offset"]) is not int or value["line_offset"] < 0):
            raise ValidationError("line_offset 必须为非负整数")
        if any(key in value and not isinstance(value[key], str) for key in ("language", "file_name")):
            raise ValidationError("代码任务语言和文件名必须是文本")
        if "rules" in value and not isinstance(value["rules"], (str, list)):
            raise ValidationError("代码审查规则必须是文本或数组")
    if set(value) - allowed:
        raise ValidationError("企业注册 Agent 当前不接受项目 ID、URL、工具参数或其他越界输入")
    encoded = _canonical_json(value)
    if len(encoded.encode("utf-8")) > 52_000:
        raise ValidationError("单个团队任务输入超过大小上限")
    return value


def _require_project_scope_for_agents(project_id: int | None, agent_codes: set[str]) -> None:
    if "security_sentinel" in agent_codes and project_id is None:
        raise ValidationError("企业安全哨兵必须绑定已授权的企业项目范围")


def _team_budget(db: Session, *, enterprise_id: int, group_id: int | None, user_id: int) -> int:
    from app.services import billing_ledger_service as ledger

    wallet = (db.query(BillingWallet).filter_by(owner_type="enterprise", owner_id=int(enterprise_id))
              .populate_existing().with_for_update().first())
    if wallet is None:
        raise PaymentRequiredError("企业积分账本不存在，不能创建 Agent 团队")
    now = enterprise_org_service._now()
    limits = (
        db.query(BillingLimit)
        .filter(
            BillingLimit.enterprise_id == int(enterprise_id),
            BillingLimit.category == "normal",
            BillingLimit.period_start <= now,
            BillingLimit.period_end > now,
            (BillingLimit.group_id.is_(None) | (BillingLimit.group_id == group_id)),
            (BillingLimit.user_id.is_(None) | (BillingLimit.user_id == int(user_id))),
        )
        .order_by(BillingLimit.id)
        .populate_existing()
        .with_for_update()
        .all()
    )
    if not limits:
        raise PaymentRequiredError("企业当前没有适用的月度积分额度")
    remaining = min(max(0, int(row.limit_milli) - int(row.used_milli) - int(row.reserved_milli)) for row in limits)
    return min(
        int(settings.enterprise_agent_team_max_budget_milli), remaining,
        int(ledger.available_milli(db, int(wallet.id), lock=True)),
    )


def create_team_from_assistant(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    state: EnterpriseAssistantRuntimeState,
    call_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Persist an idempotent low-risk team plan from one live enterprise Xiaoling call."""
    from pydantic import ValidationError as PydanticValidationError

    from app.schemas.enterprise_agent_team import EnterpriseAgentTeamCreatePlan
    from app.services import billing_ledger_service as ledger

    try:
        plan_in = EnterpriseAgentTeamCreatePlan.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError("企业团队计划格式无效") from exc
    if not call_id or len(call_id) > 120:
        raise ValidationError("企业团队工具调用标识无效")
    if int(state.enterprise_id) != int(enterprise_id) or int(state.initiator_user_id) != int(user_id):
        raise ForbiddenError("企业团队只能由当前企业小菱运行的原发起人创建")
    if state.stop_requested or state.status not in {"running", "approving"}:
        raise ForbiddenError("企业小菱运行已停止，不能继续创建团队")

    agent_codes = {str(item.agent_code) for item in plan_in.members}
    _require_project_scope_for_agents(plan_in.project_id, agent_codes)
    if "security_sentinel" in agent_codes:
        from app.core.permission_codes import PermissionCode
        from app.services.rbac_service import check_permission

        if not check_permission(db, int(user_id), PermissionCode.SECURITY_SCAN):
            raise ForbiddenError("当前账号没有 security:scan 权限，不能创建项目安全审计任务")

    run = (
        db.query(EnterpriseAssistantRun)
        .filter_by(
            id=int(state.assistant_run_id),
            enterprise_id=int(enterprise_id),
            conversation_id=int(state.conversation_id),
            initiator_user_id=int(user_id),
            source_message_id=int(state.source_message_id),
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if run is None or run.status not in {"running", "awaiting_approval"}:
        raise ForbiddenError("企业小菱父运行已结束，不能创建新团队")
    enterprise_org_service.require_enterprise_member(db, int(enterprise_id), int(user_id), lock=True)
    room = enterprise_chat_service.get_conversation(
        db,
        int(enterprise_id),
        int(user_id),
        int(state.conversation_id),
    )
    if (
        room.group_id != state.group_id
        or room.kind not in {"company", "group", "direct"}
        or (
            room.kind == "group"
            and room.group_id is not None
            and not enterprise_org_service.require_group_member(
                db,
                int(enterprise_id),
                int(room.group_id),
                int(user_id),
                allow_admin=False,
                lock=True,
            )
        )
    ):
        raise ForbiddenError("当前会话或小组权限已变化，不能创建团队")

    project_group_id = None
    if plan_in.project_id is not None:
        from app.services.enterprise_project_policy import require_enterprise_project_access

        require_enterprise_project_access(
            db,
            int(enterprise_id),
            int(plan_in.project_id),
            int(user_id),
            "execute",
        )
        binding = (
            db.query(EnterpriseProject)
            .filter_by(
                enterprise_id=int(enterprise_id),
                project_id=int(plan_in.project_id),
                status="active",
            )
            .first()
        )
        if binding is None:
            raise ForbiddenError("企业项目绑定已变化，不能创建项目范围团队")
        project_group_id = int(binding.group_id)
        if room.kind == "group" and int(room.group_id or 0) != project_group_id:
            raise ForbiddenError("小组会话只能绑定本小组负责的企业项目")

    member_by_key = {}
    snapshot_by_key = {}
    for item in plan_in.members:
        _contract, snapshot = _contract_snapshot(item.agent_code)
        member_by_key[item.member_key] = item
        snapshot_by_key[item.member_key] = snapshot
    tasks = []
    for task in plan_in.tasks:
        item = member_by_key[task.member_key]
        task_input = _validate_agent_input(item.agent_code, task.input)
        tasks.append(
            {
                "task_key": task.task_key,
                "member_key": task.member_key,
                "title": task.title,
                "instructions": task.instructions,
                "depends_on": task.depends_on,
                "input": task_input,
            }
        )
    plan_data = plan_in.model_dump(mode="json")
    plan_digest = _digest(plan_data)
    parent_permission = json.loads(state.permission_snapshot_json or "{}")
    if not isinstance(parent_permission, dict):
        raise ForbiddenError("企业小菱权限快照无效")

    existing = (
        db.query(EnterpriseAgentTeam)
        .filter_by(
            assistant_run_id=int(run.id),
            source_tool_call_id=call_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if existing is not None:
        try:
            existing_plan = json.loads(existing.plan_snapshot_json or "{}")
        except (TypeError, ValueError):
            existing_plan = {}
        if existing_plan.get("source_plan_sha256") != plan_digest:
            raise ConflictError("同一企业团队工具调用 ID 不能重放不同计划")
        return {
            "team_id": int(existing.id),
            "status": str(existing.status),
            "task_count": len(tasks),
            "budget_cap_milli": existing_plan["max_team_credits_milli"],
            "replayed": True,
        }

    from app.services import enterprise_assistant_billing_group as billing_group

    billing_group_id = billing_group.validate(db, run)
    budget = _team_budget(
        db,
        enterprise_id=int(enterprise_id),
        group_id=billing_group_id,
        user_id=int(user_id),
    )
    if budget <= 0:
        raise PaymentRequiredError("企业积分或当前月度额度不足，未创建 Agent 团队")
    runtime_plan = {
        "project_id": int(plan_in.project_id) if plan_in.project_id is not None else None,
        "allowed_models": list(ledger.billable_model_names()),
        "max_calls_per_task": int(settings.enterprise_agent_team_max_calls_per_task),
        "max_input_tokens_per_call": int(settings.enterprise_agent_team_max_input_tokens_per_call),
        "max_output_tokens_per_call": int(settings.enterprise_agent_team_max_output_tokens_per_call),
        "max_team_credits_milli": int(budget),
        "risk_level": "low",
        "requires_approval": False,
        "source_plan_sha256": plan_digest,
    }
    team = EnterpriseAgentTeam(
        assistant_run_id=int(run.id),
        enterprise_id=int(enterprise_id),
        conversation_id=int(room.id),
        project_id=int(plan_in.project_id) if plan_in.project_id is not None else None,
        group_id=room.group_id,
        initiator_user_id=int(user_id),
        source_message_id=int(run.source_message_id),
        source_tool_call_id=call_id,
        title=plan_in.title,
        objective=plan_in.objective,
        status="queued",
        payer_owner_type="enterprise",
        payer_owner_id=int(enterprise_id),
        trace_id=f"entteam_{uuid.uuid4().hex}",
        request_key_prefix=f"ent-team-{uuid.uuid4().hex}",
        parent_lease_generation=int(state.lease_generation),
        lease_generation=0,
        plan_sha256=_digest(runtime_plan),
        plan_snapshot_json=_canonical_json(runtime_plan),
        permission_snapshot_sha256=str(state.permission_snapshot_sha256),
        permission_snapshot_json=_canonical_json(parent_permission),
        result_json="{}",
        error_json="{}",
    )
    db.add(team)
    db.flush()
    members = {}
    for member_plan in plan_in.members:
        capabilities = snapshot_by_key[member_plan.member_key]
        row = EnterpriseAgentTeamMember(
            team_id=int(team.id),
            member_key=member_plan.member_key,
            display_name=member_plan.display_name,
            address=f"agent:{member_plan.agent_code}",
            kind="registered",
            role=member_plan.role,
            registered_agent_version=_digest(capabilities)[:64],
            snapshot_sha256=_digest(capabilities),
            capabilities_json=_canonical_json(capabilities),
            status="created",
        )
        db.add(row)
        db.flush()
        members[member_plan.member_key] = row
    for task_plan in tasks:
        db.add(
            EnterpriseAgentTeamTask(
                team_id=int(team.id),
                member_id=int(members[task_plan["member_key"]].id),
                task_key=task_plan["task_key"],
                title=task_plan["title"],
                instructions=task_plan["instructions"],
                dependency_keys_json=_canonical_json(task_plan["depends_on"]),
                input_json=_canonical_json(task_plan["input"]),
                status="queued",
                priority=0,
                attempt_count=0,
                max_attempts=2,
                lease_generation=0,
                result_json="{}",
                artifacts_json="[]",
                errors_json="[]",
            )
        )
    db.flush()
    db.add(
        EnterpriseAgentTeamEvent(
            team_id=int(team.id),
            enterprise_id=int(enterprise_id),
            conversation_id=int(room.id),
            actor_user_id=int(user_id),
            correlation_id=call_id,
            event_type="team.created",
            from_status=None,
            to_status="queued",
            actor_address="enterprise_xiaoling",
            detail_json=_canonical_json(
                {
                    "member_count": len(member_by_key),
                    "task_count": len(tasks),
                    "budget_cap_milli": int(budget),
                    "risk_level": "low",
                }
            ),
        )
    )
    db.flush()
    return {
        "team_id": int(team.id),
        "status": str(team.status),
        "task_count": len(tasks),
        "budget_cap_milli": int(budget),
        "replayed": False,
    }


def _timestamp(value) -> str | None:
    return value.isoformat() if value is not None else None


def require_team_conversation_access(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    team_id: int,
) -> tuple[EnterpriseAgentTeam, Any]:
    """Require live membership and access to the exact tenant conversation that owns the team."""

    enterprise_org_service.require_enterprise_member(
        db,
        int(enterprise_id),
        int(user_id),
        lock=True,
    )
    team = (
        db.query(EnterpriseAgentTeam)
        .filter(
            EnterpriseAgentTeam.id == int(team_id),
            EnterpriseAgentTeam.enterprise_id == int(enterprise_id),
        )
        .populate_existing()
        .first()
    )
    if team is None:
        raise NotFoundError("团队不存在")
    conversation = enterprise_chat_service.get_conversation(
        db,
        int(enterprise_id),
        int(user_id),
        int(team.conversation_id),
    )
    if (
        int(conversation.enterprise_id) != int(team.enterprise_id)
        or int(conversation.id) != int(team.conversation_id)
        or conversation.group_id != team.group_id
    ):
        raise NotFoundError("团队会话范围已变化")
    return team, conversation


def _can_view_all_team_results_in_conversation(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    enterprise_role: str,
    conversation: Any,
) -> bool:
    """Return whether the actor may read every team result in this room.

    Company admins retain room access only where the chat service grants it.
    Group leaders get result access only in their current group. Other members
    must have initiated the team themselves.
    """
    if str(enterprise_role) in {"owner", "admin"}:
        return True
    if conversation.kind == "group" and conversation.group_id is not None:
        group_membership = enterprise_org_service.require_group_member(
            db,
            int(enterprise_id),
            int(conversation.group_id),
            int(user_id),
            allow_admin=False,
            lock=True,
        )
        return str(group_membership.role) == "leader"
    return False


def get_team_detail(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    team_id: int,
    expected_conversation_id: int | None = None,
) -> dict[str, Any]:
    """Return task results only inside the caller's current result-visibility scope."""
    team, room = require_team_conversation_access(
        db,
        enterprise_id=int(enterprise_id),
        user_id=int(user_id),
        team_id=int(team_id),
    )
    if expected_conversation_id is not None and int(team.conversation_id) != int(expected_conversation_id):
        raise NotFoundError("团队不存在")
    member = enterprise_org_service.require_enterprise_member(
        db,
        int(enterprise_id),
        int(user_id),
        lock=True,
    )
    can_view_room_results = _can_view_all_team_results_in_conversation(
        db,
        enterprise_id=int(enterprise_id),
        user_id=int(user_id),
        enterprise_role=str(member.role),
        conversation=room,
    )
    if not can_view_room_results and int(team.initiator_user_id) != int(user_id):
        # Match list filtering and conceal the existence of a hidden team ID.
        raise NotFoundError("团队不存在")
    if team.project_id is not None:
        from app.services.enterprise_project_policy import require_enterprise_project_access

        try:
            require_enterprise_project_access(
                db,
                int(enterprise_id),
                int(team.project_id),
                int(user_id),
                "view",
            )
        except (ForbiddenError, NotFoundError) as exc:
            raise NotFoundError("团队不存在") from exc

    members = {
        int(row.id): row
        for row in db.query(EnterpriseAgentTeamMember)
        .filter_by(
            team_id=int(team.id),
        )
        .all()
    }
    tasks = (
        db.query(EnterpriseAgentTeamTask)
        .filter_by(team_id=int(team.id))
        .order_by(
            EnterpriseAgentTeamTask.id.asc(),
        )
        .all()
    )
    items = []
    for task in tasks:
        member = members.get(int(task.member_id))
        try:
            result = json.loads(task.result_json or "{}")
        except (TypeError, ValueError):
            result = {"status": "unavailable", "summary": "任务结果快照无效"}
        if not isinstance(result, dict):
            result = {"status": "unavailable", "summary": "任务结果快照格式无效"}
        try:
            errors = json.loads(task.errors_json or "[]")
        except (TypeError, ValueError):
            errors = []
        items.append(
            {
                "task_id": int(task.id),
                "task_key": str(task.task_key),
                "title": str(task.title),
                "agent_code": str(member.address).split(":", 1)[-1] if member else "",
                "agent_name": str(member.display_name) if member else "",
                "status": str(task.status),
                "attempt_count": int(task.attempt_count),
                "result": result,
                "errors": errors if isinstance(errors, list) else [],
                "started_at": _timestamp(task.started_at),
                "completed_at": _timestamp(task.completed_at),
            }
        )
    return {
        "team_id": int(team.id),
        "enterprise_id": int(team.enterprise_id),
        "conversation_id": int(team.conversation_id),
        "group_id": team.group_id,
        "project_id": int(team.project_id) if team.project_id is not None else None,
        "title": str(team.title),
        "objective": str(team.objective),
        "status": str(team.status),
        "created_at": _timestamp(team.create_time),
        "started_at": _timestamp(team.started_at),
        "completed_at": _timestamp(team.completed_at),
        "tasks": items,
    }


def list_team_summaries(
    db: Session,
    *,
    enterprise_id: int,
    user_id: int,
    group_id: int | None = None,
    conversation_id: int | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List minimal summaries under current company/group/chat visibility.

    Company administrators may inspect tenant-wide operational metadata. Group
    leaders see metadata for their active groups. Other members see only teams
    they initiated, including when filtering by a room they can read. A title is
    included only when the caller can currently read
    the source conversation; result bodies and immutable snapshots are never
    part of this listing.
    """

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("团队列表 limit 必须为 1 到 100")
    if before_id is not None and (type(before_id) is not int or before_id <= 0):
        raise ValidationError("团队列表 before_id 无效")
    if conversation_id is not None and (type(conversation_id) is not int or conversation_id <= 0):
        raise ValidationError("conversation_id 无效")
    if group_id is not None and (type(group_id) is not int or group_id <= 0):
        raise ValidationError("group_id 无效")

    enterprise_id, user_id = int(enterprise_id), int(user_id)
    member = enterprise_org_service.require_enterprise_member(db, enterprise_id, user_id, lock=True)
    query = db.query(EnterpriseAgentTeam).filter(EnterpriseAgentTeam.enterprise_id == enterprise_id)
    visible_group_ids: set[int] = set()
    conversation = None

    if conversation_id is not None:
        conversation = enterprise_chat_service.get_conversation(
            db,
            enterprise_id,
            user_id,
            conversation_id,
        )
        if group_id is not None and (conversation.kind != "group" or int(conversation.group_id or 0) != int(group_id)):
            raise ValidationError("group_id 与 conversation_id 不属于同一小组会话")
        query = query.filter(
            EnterpriseAgentTeam.conversation_id == int(conversation.id),
            EnterpriseAgentTeam.group_id == conversation.group_id,
        )
        can_view_room_results = _can_view_all_team_results_in_conversation(
            db,
            enterprise_id=enterprise_id,
            user_id=user_id,
            enterprise_role=str(member.role),
            conversation=conversation,
        )
        if not can_view_room_results:
            query = query.filter(EnterpriseAgentTeam.initiator_user_id == user_id)
    elif group_id is not None:
        group_membership = enterprise_org_service.require_group_member(
            db,
            enterprise_id,
            int(group_id),
            user_id,
            allow_admin=True,
            lock=True,
        )
        if member.role in {"owner", "admin"}:
            visible_group_ids.add(int(group_id))
            query = query.filter(EnterpriseAgentTeam.group_id == int(group_id))
        else:
            group_role = str(group_membership.role)
            if group_role == "leader":
                visible_group_ids.add(int(group_id))
                query = query.filter(EnterpriseAgentTeam.group_id == int(group_id))
            else:
                query = query.filter(
                    EnterpriseAgentTeam.group_id == int(group_id),
                    EnterpriseAgentTeam.initiator_user_id == user_id,
                )
    elif member.role in {"owner", "admin"}:
        pass
    else:
        visible_group_ids = {
            int(group_id_value)
            for (group_id_value,) in db.query(EnterpriseGroupMember.group_id)
            .join(
                EnterpriseGroup,
                (EnterpriseGroup.id == EnterpriseGroupMember.group_id)
                & (EnterpriseGroup.enterprise_id == EnterpriseGroupMember.enterprise_id),
            )
            .filter(
                EnterpriseGroupMember.enterprise_id == enterprise_id,
                EnterpriseGroupMember.user_id == user_id,
                EnterpriseGroupMember.role == "leader",
                EnterpriseGroupMember.status == "active",
                EnterpriseGroup.status == "active",
            )
            .populate_existing()
            .with_for_update()
            .all()
        }
        query = query.filter(
            or_(
                EnterpriseAgentTeam.initiator_user_id == user_id,
                EnterpriseAgentTeam.group_id.in_(visible_group_ids) if visible_group_ids else False,
            )
        )

    visible_projects = (
        db.query(EnterpriseProject.project_id)
        .join(
            EnterpriseGroup,
            (EnterpriseGroup.id == EnterpriseProject.group_id)
            & (EnterpriseGroup.enterprise_id == EnterpriseProject.enterprise_id),
        )
        .join(Project, Project.id == EnterpriseProject.project_id)
        .filter(
            EnterpriseProject.enterprise_id == enterprise_id,
            EnterpriseProject.status == "active",
            EnterpriseGroup.status == "active",
            Project.status.notin_(("deleted", "quarantined")),
        )
    )
    if member.role not in {"owner", "admin"}:
        current_group_ids = db.query(EnterpriseGroupMember.group_id).filter(
            EnterpriseGroupMember.enterprise_id == enterprise_id,
            EnterpriseGroupMember.user_id == user_id,
            EnterpriseGroupMember.status == "active",
        )
        current_grant_project_ids = db.query(EnterpriseProjectGrant.project_id).filter(
            EnterpriseProjectGrant.enterprise_id == enterprise_id,
            EnterpriseProjectGrant.user_id == user_id,
            EnterpriseProjectGrant.status == "active",
            EnterpriseProjectGrant.role.in_(("viewer", "reviewer", "editor")),
        )
        visible_projects = visible_projects.filter(
            or_(
                EnterpriseProject.group_id.in_(current_group_ids),
                EnterpriseProject.project_id.in_(current_grant_project_ids),
            )
        )
    query = query.filter(
        or_(
            EnterpriseAgentTeam.project_id.is_(None),
            EnterpriseAgentTeam.project_id.in_(visible_projects),
        )
    )
    if before_id is not None:
        query = query.filter(EnterpriseAgentTeam.id < int(before_id))
    candidates = query.order_by(EnterpriseAgentTeam.id.desc()).limit(limit + 1).all()
    has_more = len(candidates) > limit
    rows = candidates[:limit]
    team_ids = [int(row.id) for row in rows]

    status_counts: dict[int, dict[str, int]] = {team_id: {} for team_id in team_ids}
    if team_ids:
        totals = (
            db.query(
                EnterpriseAgentTeamTask.team_id,
                EnterpriseAgentTeamTask.status,
                func.count(EnterpriseAgentTeamTask.id),
            )
            .filter(EnterpriseAgentTeamTask.team_id.in_(team_ids))
            .group_by(
                EnterpriseAgentTeamTask.team_id,
                EnterpriseAgentTeamTask.status,
            )
            .all()
        )
        for team_id_value, task_status, count in totals:
            status_counts[int(team_id_value)][str(task_status)] = int(count)

    if conversation is not None:
        readable_conversation_ids = {int(conversation.id)}
    else:
        readable_conversation_ids = {
            int(item.id) for item in enterprise_chat_service.list_conversations(db, enterprise_id, user_id)
        }

    items = []
    for row in rows:
        counts = status_counts[int(row.id)]
        readable = int(row.conversation_id) in readable_conversation_ids
        items.append(
            {
                "team_id": int(row.id),
                "enterprise_id": int(row.enterprise_id),
                "conversation_id": int(row.conversation_id),
                "group_id": int(row.group_id) if row.group_id is not None else None,
                "project_id": int(row.project_id) if row.project_id is not None else None,
                "initiator_user_id": int(row.initiator_user_id),
                "title": row.title if readable else None,
                "status": row.status,
                "task_count": sum(counts.values()),
                "completed_task_count": counts.get("completed", 0),
                "running_task_count": counts.get("running", 0),
                "blocked_task_count": counts.get("blocked", 0),
                "created_at": _timestamp(row.create_time),
                "started_at": _timestamp(row.started_at),
                "completed_at": _timestamp(row.completed_at),
                "can_read_conversation": readable,
            }
        )
    return {
        "items": items,
        "has_more": has_more,
        "next_before_id": int(rows[-1].id) if has_more and rows else None,
    }
