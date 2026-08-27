"""小菱动态子 Agent 团队状态机、工作图和持久化队列服务。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.agents.contracts import CONTRACTS
from app.core.config import settings
from app.core.observability import observe_event
from app.models.agent_mesh import AgentMeshMessage, AgentMeshMessageEvent
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember, AgentTeamTask
from app.models.custom_agent import CustomAgent, CustomAgentRelease, CustomAgentVersion
from app.models.project_source_revision import ProjectSourceRevision
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn


class AgentTeamError(ValueError):
    """团队业务错误基类。"""


class AgentTeamValidationError(AgentTeamError):
    """请求或工作图无效。"""


class AgentTeamNotFoundError(AgentTeamError):
    """团队不属于当前账户或不存在。"""


class AgentTeamAccessError(AgentTeamError):
    """团队访问越权。"""


class AgentTeamStateError(AgentTeamError):
    """团队状态不允许当前操作。"""


class AgentTeamLeaseError(AgentTeamError):
    """租约不存在或已经失效。"""


_TERMINAL_TEAM = frozenset({"completed", "failed", "cancelled", "expired"})
_TERMINAL_TASK = frozenset({"completed", "failed", "blocked", "cancelled", "dead_letter", "expired"})
_TEAM_GOVERNED_RUNTIME_CODES = frozenset({"sandbox_deployer", "test_verifier", "operations"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _unjson(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        loaded = json.loads(value or "")
        return loaded
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _public(value: Any) -> Any:
    """复用 Responses 公开事件的有界脱敏，避免团队详情暴露密钥或完整内部提示词。"""

    try:
        from app.services.agent_responses_service import redact_agent_event_value

        return redact_agent_event_value(value)
    except Exception:  # pragma: no cover - 脱敏器不可用时也必须 fail-closed
        sensitive_key = re.compile(
            r"(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password|secret|private[_-]?key)"
        )
        sensitive_text = re.compile(r"(?i)(bearer\s+|sk-|ak-|token\s*[:=]\s*)[^\s,;]+")

        def fallback(item: Any, *, key: str = "", depth: int = 0) -> Any:
            if sensitive_key.search(key):
                return "[REDACTED]"
            if depth >= 5:
                return "[TRUNCATED]"
            if isinstance(item, dict):
                return {
                    str(child_key): fallback(child, key=str(child_key), depth=depth + 1)
                    for child_key, child in list(item.items())[:50]
                }
            if isinstance(item, list):
                return [fallback(child, depth=depth + 1) for child in item[:20]]
            if isinstance(item, str):
                return sensitive_text.sub("[REDACTED]", item)[:500]
            return item

        return fallback(value)


def _task_counts(tasks: list[AgentTeamTask]) -> dict[str, int]:
    return {
        "total": len(tasks),
        "completed": sum(item.status == "completed" for item in tasks),
        "running": sum(item.status == "running" for item in tasks),
        "queued": sum(item.status in {"queued", "waiting_dependency"} for item in tasks),
        "failed": sum(item.status in {"failed", "dead_letter", "expired"} for item in tasks),
        "blocked": sum(item.status == "blocked" for item in tasks),
    }


def _validate_safe_input(value: Any, *, key: str = "", depth: int = 0) -> None:
    """拒绝把宿主机路径或过深对象直接交给子 Agent。"""

    if depth > 6:
        raise AgentTeamValidationError("任务 input 嵌套层级不能超过 6")
    lowered = key.lower()
    if lowered in {"path", "file_path", "source_path", "host_path", "absolute_path"}:
        raise AgentTeamValidationError("任务 input 不得携带宿主机路径，请改用 source_revision_id 或产物引用")
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _validate_safe_input(child_value, key=str(child_key), depth=depth + 1)
        return
    if isinstance(value, list):
        for child_value in value[:100]:
            _validate_safe_input(child_value, key=key, depth=depth + 1)
        return
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("/Users/", "/home/", "/root/", "/var/", "/tmp/", "file://")):
            raise AgentTeamValidationError("任务 input 不得携带宿主机路径，请改用 source_revision_id 或产物引用")


def _dependency_context(db: Session, team: AgentTeam, task: AgentTeamTask) -> dict[str, Any]:
    """只把前置节点的脱敏结果摘要传入下一节点。"""

    wanted = {str(item) for item in _unjson(task.dependency_keys_json, [])}
    if not wanted:
        return {}
    rows = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
    by_key = {row.task_key: row for row in rows}
    return {
        key: {
            "status": by_key[key].status,
            "result": _public(_unjson(by_key[key].result_json, {})),
            "artifacts": _public(_unjson(by_key[key].artifacts_json, [])),
            "errors": _public(_unjson(by_key[key].errors_json, [])),
        }
        for key in sorted(wanted)
        if key in by_key
    }


def _ensure_session(db: Session, team_user: User, *, surface: str, session_key: str, title: str) -> None:
    """确保团队结果有合法的同账户会话收件箱。"""

    from app.models.agent_mesh import AgentMeshConversation

    row = (
        db.query(AgentMeshConversation)
        .filter(
            AgentMeshConversation.user_id == int(team_user.id),
            AgentMeshConversation.surface == surface,
            AgentMeshConversation.session_key == session_key,
        )
        .first()
    )
    if row is None:
        row = AgentMeshConversation(
            user_id=int(team_user.id),
            surface=surface,
            session_key=session_key,
            title=title.strip() or "新对话",
            status="active",
            last_seen_at=_now(),
        )
        db.add(row)
    elif row.status != "active":
        raise AgentTeamAccessError("团队所属会话已归档，不能继续调度")
    row.last_seen_at = _now()
    db.flush()


def _is_admin(db: Session, user: User) -> bool:
    if str(getattr(user, "role", "")) in {"admin", "super_admin"}:
        return True
    try:
        from app.services.rbac_service import is_admin_user

        return bool(is_admin_user(db, int(user.id)))
    except Exception:  # pragma: no cover - 测试中的轻量 User 不一定有 RBAC 表
        return False


def _assert_surface(db: Session, user: User, surface: str) -> None:
    if surface not in {"user", "admin"}:
        raise AgentTeamValidationError("surface 只能是 user 或 admin")
    if surface == "admin" and not _is_admin(db, user):
        raise AgentTeamAccessError("普通账户不能创建管理端团队")


def _validate_target(db: Session, user: User, address: str) -> tuple[str, Optional[int], Optional[int], dict[str, Any]]:
    """校验目标必须是可执行内置 Handler 或已发布自定义 Agent。"""

    from app.services.agent_mesh_dispatcher import dispatch_state

    state = dispatch_state(address)
    if address.startswith("agent:"):
        code = address.split(":", 1)[1]
        if code not in CONTRACTS:
            raise AgentTeamValidationError(f"目标 Agent {code} 不存在")
        team_governed = state == "approval_required" and code in _TEAM_GOVERNED_RUNTIME_CODES
        if state != "executable" and not team_governed:
            raise AgentTeamValidationError(f"目标 Agent {code} 当前状态为 {state}，不能作为团队成员")
        return (
            "runtime",
            None,
            None,
            {
                "dispatch_state": "team_governed" if team_governed else state,
                "code": code,
                "requires_project_scope": team_governed,
            },
        )
    if address.startswith("custom:"):
        code = address.split(":", 1)[1]
        row = (
            db.query(CustomAgent)
            .filter(CustomAgent.code == code, CustomAgent.is_enabled == 1, CustomAgent.status == "published")
            .first()
        )
        if row is None or state != "executable":
            raise AgentTeamValidationError(f"已发布 Agent {code} 不存在或不可执行")
        version_id = int(row.current_published_version_id or 0) or None
        version = (
            db.query(CustomAgentVersion)
            .filter(
                CustomAgentVersion.id == version_id,
                CustomAgentVersion.agent_id == row.id,
                CustomAgentVersion.status == "published",
            )
            .first()
            if version_id
            else None
        )
        release = (
            db.query(CustomAgentRelease)
            .filter(
                CustomAgentRelease.agent_id == row.id,
                CustomAgentRelease.agent_version_id == version_id,
                CustomAgentRelease.status == "published",
            )
            .order_by(CustomAgentRelease.id.desc())
            .first()
            if version_id
            else None
        )
        if version is None or release is None:
            raise AgentTeamValidationError(f"已发布 Agent {code} 缺少有效版本快照")
        if str(getattr(user, "role", "")) not in {"admin", "super_admin"}:
            from app.core.permission_codes import PermissionCode
            from app.services.rbac_service import check_permission

            if not check_permission(db, int(user.id), PermissionCode.CUSTOM_AGENT_INVOKE):
                raise AgentTeamAccessError("当前账户没有调用已发布自定义 Agent 的权限")
        return (
            "custom",
            int(row.id),
            version_id,
            {
                "dispatch_state": state,
                "code": code,
                "release_id": int(release.id),
                "package_checksum": release.package_checksum,
                "template_checksum": version.checksum,
            },
        )
    raise AgentTeamValidationError("成员目标只能使用 agent:<code> 或 custom:<code>")


def _topological_order(task_inputs: Iterable[Any]) -> list[str]:
    rows = list(task_inputs)
    keys = [str(item.task_key) for item in rows]
    key_set = set(keys)
    if len(keys) != len(key_set):
        raise AgentTeamValidationError("任务 task_key 不能重复")
    indegree = {key: 0 for key in keys}
    outgoing: dict[str, list[str]] = {key: [] for key in keys}
    for item in rows:
        for dependency in item.depends_on:
            if dependency not in key_set:
                raise AgentTeamValidationError(f"任务 {item.task_key} 依赖不存在: {dependency}")
            if dependency == item.task_key:
                raise AgentTeamValidationError("任务依赖图存在环")
            indegree[item.task_key] += 1
            outgoing[dependency].append(item.task_key)
    queue = [key for key in keys if indegree[key] == 0]
    ordered: list[str] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for child in outgoing[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(keys):
        raise AgentTeamValidationError("任务依赖图存在环")
    return ordered


def _validate_verification_coverage(payload: AgentTeamCreateIn) -> None:
    """工作图的每个终点都必须是验证或汇总节点。

    有限 DAG 中每个工作节点都会到达某个终点，因此这个约束能防止
    任何工作结果绕过 verifier/summarizer 就被团队宣告完成。
    """

    member_roles = {item.member_key: item.role for item in payload.members}
    outgoing = {item.task_key: [] for item in payload.tasks}
    for item in payload.tasks:
        for dependency in item.depends_on:
            outgoing[dependency].append(item.task_key)
    uncovered = [
        item.task_key
        for item in payload.tasks
        if not outgoing[item.task_key] and member_roles.get(item.member_key) not in {"verifier", "summarizer"}
    ]
    if uncovered:
        raise AgentTeamValidationError(f"最终 verifier/summarizer 必须覆盖所有工作结果，未覆盖: {', '.join(uncovered)}")


_READONLY_TEAM_MARKERS = (
    "只读",
    "只核对",
    "仅核对",
    "严禁运行新测试",
    "不得触发任何测试",
    "不执行新测试",
    "read_only",
    "read-only",
)


def _payload_is_readonly(payload: AgentTeamCreateIn) -> bool:
    """按团队目标和任务说明判定整支团队是否为只读验收。"""
    texts = [payload.objective or ""]
    texts.extend(item.instructions or "" for item in payload.tasks)
    joined = "\n".join(texts)
    return any(marker in joined for marker in _READONLY_TEAM_MARKERS)


_TEAM_TASK_CONTRACTS: dict[str, dict[str, Any]] = {
    "review_orchestrator": {
        "default_operation": "list",
        "readonly_operation": None,
        "allowed_operations": frozenset({"list", "get", "issues"}),
    },
    "test_verifier": {
        "default_operation": "run_project_tests",
        "readonly_operation": "inspect_existing_results",
        "allowed_operations": frozenset({
            "run_project_tests",
            "run_full_project_validation",
            "inspect_existing_results",
        }),
    },
    "project_analyzer": {
        "default_operation": "inspect_project",
        "readonly_operation": "inspect_project",
        "allowed_operations": frozenset({"inspect_project"}),
    },
    "code_file_manager": {
        "default_operation": "list",
        "readonly_operation": "list",
        "allowed_operations": frozenset({"list"}),
    },
    "dashboard": {
        "default_operation": "summary",
        "readonly_operation": None,
        "allowed_operations": frozenset({"summary", "risk_distribution", "score_trend"}),
    },
    # security_sentinel 不需要 operation,不声明默认值也不改写输入。
    "security_sentinel": {
        "default_operation": None,
        "readonly_operation": None,
        "allowed_operations": frozenset(),
    },
}


def _normalize_task_inputs(payload: AgentTeamCreateIn) -> AgentTeamCreateIn:
    """按通用契约表纠正团队任务的 operation。

    - 缺失 operation:补 default_operation;
    - 只读团队且 Agent 声明 readonly_operation:强制替换;
    - 非只读团队且 operation 不在 allowed_operations:回退 default_operation。
    security_sentinel 不声明 operation 契约,输入保持原样。
    """
    readonly = _payload_is_readonly(payload)
    member_by_key = {item.member_key: item for item in payload.members}
    for task in payload.tasks:
        member = member_by_key.get(task.member_key)
        if member is None or not member.address.startswith("agent:"):
            continue
        code = member.address.split(":", 1)[1]
        contract = _TEAM_TASK_CONTRACTS.get(code)
        if contract is None:
            continue
        data = dict(task.input or {})
        default_operation = contract.get("default_operation")
        readonly_operation = contract.get("readonly_operation")
        allowed_operations = contract.get("allowed_operations") or frozenset()
        operation = data.get("operation")

        if operation is None and default_operation is not None:
            data["operation"] = default_operation
            operation = default_operation

        if readonly and readonly_operation is not None:
            data["operation"] = readonly_operation
        elif operation is not None and allowed_operations and operation not in allowed_operations:
            data["operation"] = default_operation
        task.input = data
    return payload


def _normalize_readonly_task_inputs(payload: AgentTeamCreateIn) -> AgentTeamCreateIn:
    """只读团队任务输入规范化的兼容入口,非只读团队保持原行为不变。"""
    if not _payload_is_readonly(payload):
        return payload
    return _normalize_task_inputs(payload)


def _normalize_verification_graph(payload: AgentTeamCreateIn) -> AgentTeamCreateIn:
    """为模型偶发遗漏的汇总节点补齐一个确定性的 reporter 终点。"""

    members = list(payload.members)
    tasks = list(payload.tasks)
    member_roles = {item.member_key: item.role for item in members}
    verification_members = [
        item.member_key for item in members if item.role in {"verifier", "summarizer"}
    ]
    if not verification_members:
        if len(members) >= 16:
            raise AgentTeamValidationError("团队缺少 verifier/summarizer，且成员数量已达上限，无法自动补齐")
        from app.schemas.agent_team import AgentTeamMemberIn, AgentTeamTaskIn

        member_key = "auto_reporter"
        task_key = "auto_summary"
        occupied_members = set(member_roles)
        occupied_tasks = {item.task_key for item in tasks}
        suffix = 2
        while member_key in occupied_members:
            member_key = f"auto_reporter_{suffix}"
            suffix += 1
        suffix = 2
        while task_key in occupied_tasks:
            task_key = f"auto_summary_{suffix}"
            suffix += 1
        members.append(
            AgentTeamMemberIn(
                member_key=member_key,
                display_name="自动汇总 Agent",
                address="agent:reporter",
                role="summarizer",
            )
        )
        verification_members = [member_key]
    else:
        task_key = ""

    outgoing = {item.task_key: [] for item in tasks}
    for item in tasks:
        for dependency in item.depends_on:
            if dependency in outgoing:
                outgoing[dependency].append(item.task_key)
    leaves = [item.task_key for item in tasks if not outgoing[item.task_key]]
    verification_tasks = [
        item for item in tasks if item.member_key in set(verification_members)
    ]
    if not verification_tasks:
        from app.schemas.agent_team import AgentTeamTaskIn

        occupied_tasks = {item.task_key for item in tasks}
        task_key = "auto_summary"
        suffix = 2
        while task_key in occupied_tasks:
            task_key = f"auto_summary_{suffix}"
            suffix += 1
        tasks.append(
            AgentTeamTaskIn(
                task_key=task_key,
                member_key=verification_members[0],
                title="汇总全部子 Agent 结果",
                instructions="汇总前置任务的真实结果、证据和错误，输出结构化结论；不得编造未提供的信息。",
                depends_on=leaves,
                input={},
            )
        )
        verification_tasks = [tasks[-1]]
    task_member_keys = {item.task_key: item.member_key for item in tasks}
    uncovered = [
        key
        for key in leaves
        if not any(key in item.depends_on for item in verification_tasks)
        and member_roles.get(task_member_keys.get(key, ""), "") not in {"verifier", "summarizer"}
    ]
    if task_key and not any(item.task_key == task_key for item in tasks):
        from app.schemas.agent_team import AgentTeamTaskIn

        tasks.append(
            AgentTeamTaskIn(
                task_key=task_key,
                member_key=verification_members[0],
                title="汇总全部子 Agent 结果",
                instructions="汇总前置任务的真实结果、证据和错误，输出结构化结论；不得编造未提供的信息。",
                depends_on=leaves,
                input={},
            )
        )
    elif uncovered and verification_tasks:
        target = verification_tasks[-1]
        target.depends_on = list(dict.fromkeys([*target.depends_on, *uncovered]))
    return payload.model_copy(update={"members": members, "tasks": tasks})


def _validate_task_scope(db: Session, user: User, task_input: Any, address: str) -> None:
    raw = task_input.input
    project_id = raw.get("project_id")
    revision_id = raw.get("source_revision_id")
    if project_id is not None and (isinstance(project_id, bool) or not isinstance(project_id, int) or project_id <= 0):
        raise AgentTeamValidationError(f"任务 {task_input.task_key} 的 project_id 必须是正整数")
    if revision_id is not None and (
        isinstance(revision_id, bool) or not isinstance(revision_id, int) or revision_id <= 0
    ):
        raise AgentTeamValidationError(f"任务 {task_input.task_key} 的 source_revision_id 必须是正整数")
    if revision_id is not None and project_id is None:
        raise AgentTeamValidationError("源码修订必须与 project_id 同时提供")

    if project_id is not None:
        try:
            from app.services.project_member_service import require_project_access

            require_project_access(db, int(project_id), user, need_write=False)
        except Exception as exc:
            raise AgentTeamValidationError("项目不存在或不属于当前账户可见范围") from exc
    if revision_id is not None:
        revision = db.get(ProjectSourceRevision, int(revision_id))
        if revision is None or int(revision.project_id) != int(project_id):
            raise AgentTeamValidationError("源码修订不存在或不属于当前可见项目")

    if not address.startswith("agent:"):
        return
    code = address.split(":", 1)[1]
    if code == "monitor":
        window = raw.get("window_minutes")
        metrics = raw.get("metrics")
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 1440:
            raise AgentTeamValidationError(f"任务 {task_input.task_key} 的 window_minutes 必须是 1 到 1440 的整数")
        if (
            not isinstance(metrics, list)
            or not metrics
            or any(not isinstance(item, str) or not item.strip() for item in metrics)
        ):
            raise AgentTeamValidationError(f"任务 {task_input.task_key} 的 metrics 必须是非空指标名字符串列表")
        return
    if code not in _TEAM_GOVERNED_RUNTIME_CODES:
        return
    if code == "operations":
        from app.services import ops_service, rbac_service

        if raw.get("action") not in ops_service.READ_ONLY_ACTIONS:
            raise AgentTeamValidationError("子 Agent 团队只能执行运维只读动作，写操作必须回到主小菱审批")
        if not rbac_service.is_super_admin_user(db, int(user.id)):
            raise AgentTeamAccessError("仅唯一超级管理员 admin 可创建运维子 Agent 任务")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise AgentTeamValidationError(f"任务 {task_input.task_key} 的运维 params 必须是 JSON object")
        try:
            ops_service.validate_action_params(str(raw.get("action") or ""), params)
        except ValueError as exc:
            raise AgentTeamValidationError(f"任务 {task_input.task_key} 的运维参数无效：{exc}") from exc
        return
    operation = str(raw.get("operation") or ("deploy" if code == "sandbox_deployer" else "run_project_tests"))
    allowed = (
        {"deploy", "close", "extend"}
        if code == "sandbox_deployer"
        else {"run_project_tests", "run_full_project_validation", "inspect_existing_results"}
    )
    if operation not in allowed:
        raise AgentTeamValidationError(f"任务 {task_input.task_key} 的受治理操作 {operation} 不受支持")
    if (code == "test_verifier" and operation != "inspect_existing_results") or operation == "deploy":
        if project_id is None or not str(raw.get("language") or "").strip():
            raise AgentTeamValidationError(f"任务 {task_input.task_key} 必须提供 project_id 和 language")
    if code == "test_verifier" and operation == "inspect_existing_results" and project_id is None:
        raise AgentTeamValidationError(f"任务 {task_input.task_key} 必须提供 project_id")


def _event(
    db: Session,
    team: AgentTeam,
    event_type: str,
    *,
    task: Optional[AgentTeamTask] = None,
    member: Optional[AgentTeamMember] = None,
    actor_address: str = "system:agent_team",
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    message_id: Optional[str] = None,
    correlation_id: str = "",
) -> AgentTeamEvent:
    # 前端按任务/成员驱动团队工作卡片和思考城市；统一补齐上下文，避免调用点遗漏。
    event_detail = dict(detail or {})
    if task is not None:
        event_detail.setdefault("task_key", task.task_key)
    if member is not None:
        event_detail.setdefault("member_key", member.member_key)
    row = AgentTeamEvent(
        team_id=int(team.id),
        task_id=int(task.id) if task else None,
        member_id=int(member.id) if member else None,
        user_id=int(team.user_id),
        message_id=message_id,
        correlation_id=correlation_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_address=actor_address,
        trace_id=team.trace_id,
        detail_json=_json(_public(event_detail)),
    )
    db.add(row)
    return row


def _lease_fingerprint(lease_token: str) -> str:
    return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()


def attach_task_runtime_resource(
    db: Session,
    *,
    owner_user_id: int,
    team_id: int,
    task_id: int,
    lease_token: str,
    resource_type: str,
    resource_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> AgentTeamEvent:
    """把团队执行资源与当前租约原子关联；调用方决定何时提交事务。"""

    if resource_type != "sandbox_environment":
        raise AgentTeamValidationError("团队运行资源类型不受支持")
    if not resource_id or len(resource_id) > 80 or not re.fullmatch(r"[A-Za-z0-9_-]+", resource_id):
        raise AgentTeamValidationError("团队运行资源标识无效")
    team = db.query(AgentTeam).filter(AgentTeam.id == int(team_id)).with_for_update().first()
    task = (
        db.query(AgentTeamTask)
        .filter(AgentTeamTask.id == int(task_id), AgentTeamTask.team_id == int(team_id))
        .with_for_update()
        .first()
    )
    if team is None or task is None or int(team.user_id) != int(owner_user_id):
        raise AgentTeamNotFoundError("团队任务不存在或不属于沙箱创建者")
    if team.status in _TERMINAL_TEAM or task.status != "running" or not lease_token or task.lease_token != lease_token:
        raise AgentTeamLeaseError("沙箱创建前团队任务租约已失效")
    fingerprint = _lease_fingerprint(lease_token)
    existing = (
        db.query(AgentTeamEvent)
        .filter(
            AgentTeamEvent.team_id == team.id,
            AgentTeamEvent.task_id == task.id,
            AgentTeamEvent.event_type == "task.runtime_resource_attached",
        )
        .order_by(AgentTeamEvent.id.desc())
        .all()
    )
    for row in existing:
        detail = _unjson(row.detail_json, {})
        if detail.get("resource_id") == resource_id and detail.get("lease_fingerprint") == fingerprint:
            return row
    row = _event(
        db,
        team,
        "task.runtime_resource_attached",
        task=task,
        member=db.get(AgentTeamMember, task.member_id),
        actor_address=f"agent:{resource_type}",
        detail={
            "resource_type": resource_type,
            "resource_id": resource_id,
            "attempt": int(task.attempt_count or 0),
            "lease_fingerprint": fingerprint,
            "metadata": metadata or {},
        },
    )
    db.flush()
    return row


def _active_task_runtime_resources(db: Session, task_id: int) -> list[dict[str, str]]:
    rows = (
        db.query(AgentTeamEvent)
        .filter(
            AgentTeamEvent.task_id == int(task_id),
            AgentTeamEvent.event_type.in_(("task.runtime_resource_attached", "task.runtime_resource_stopped")),
        )
        .order_by(AgentTeamEvent.id.asc())
        .all()
    )
    active: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        detail = _unjson(row.detail_json, {})
        resource_type = str(detail.get("resource_type") or "")
        resource_id = str(detail.get("resource_id") or "")
        if not resource_type or not resource_id:
            continue
        key = (resource_type, resource_id)
        if row.event_type == "task.runtime_resource_attached":
            active[key] = {"resource_type": resource_type, "resource_id": resource_id}
        else:
            active.pop(key, None)
    return list(active.values())


def _cleanup_task_runtime_resources(
    db: Session,
    team: AgentTeam,
    task: AgentTeamTask,
    *,
    reason: str,
) -> bool:
    resources = _active_task_runtime_resources(db, int(task.id))
    if not resources:
        return True
    actor = db.get(User, int(team.user_id))
    all_stopped = True
    for resource in resources:
        resource_type = resource["resource_type"]
        resource_id = resource["resource_id"]
        error = ""
        stopped_state: dict[str, Any] = {}
        try:
            if actor is None:
                raise AgentTeamStateError("团队所属账户不存在，无法执行沙箱清理")
            if resource_type != "sandbox_environment":
                raise AgentTeamStateError(f"不支持清理运行资源 {resource_type}")
            from app.services import sandbox_service

            stopped_state = sandbox_service.stop_environment(db, actor, resource_id)
            stopped_status = str(stopped_state.get("status") or "").casefold()
            if stopped_status not in {"succeeded", "failed", "blocked", "stopped", "expired"}:
                raise AgentTeamStateError(f"运行资源 {resource_id} 未达到终态，当前状态 {stopped_status or 'unknown'}")
        except Exception as exc:  # noqa: BLE001 - 清理失败必须持久化且禁止新尝试
            error = str(exc)[:2000]
            all_stopped = False
            # stop_environment 可能因数据库超时把 Session 留在 failed 状态。
            # 先回滚再重读团队和任务，否则连停止失败证据都无法落库。
            db.rollback()
        db.expire_all()
        current_team = db.get(AgentTeam, int(team.id))
        current_task = db.get(AgentTeamTask, int(task.id))
        if current_team is None or current_task is None:
            return False
        _event(
            db,
            current_team,
            "task.runtime_resource_stop_failed" if error else "task.runtime_resource_stopped",
            task=current_task,
            member=db.get(AgentTeamMember, current_task.member_id),
            detail={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "reason": reason,
                "status": str(stopped_state.get("status") or "") if not error else "",
                "error": error,
            },
        )
        db.commit()
    return all_stopped


def _require_live_task_lease(
    db: Session,
    *,
    owner_user_id: int,
    team_id: int,
    task_id: int,
    lease_token: str,
) -> tuple[AgentTeam, AgentTeamTask, AgentTeamMember]:
    team = db.get(AgentTeam, int(team_id))
    task = db.get(AgentTeamTask, int(task_id))
    member = db.get(AgentTeamMember, int(task.member_id)) if task is not None else None
    if (
        team is None
        or task is None
        or member is None
        or int(task.team_id) != int(team_id)
        or int(member.team_id) != int(team_id)
        or int(team.user_id) != int(owner_user_id)
    ):
        raise AgentTeamNotFoundError("团队验证任务不存在或不属于当前账户")
    if team.status in _TERMINAL_TEAM or task.status != "running" or not lease_token or task.lease_token != lease_token:
        raise AgentTeamLeaseError("验证任务租约已失效")
    if member.address != "agent:test_verifier":
        raise AgentTeamStateError("只有测试验证 Agent 可以接管直接依赖的部署资源")
    return team, task, member


def handoff_dependency_runtime_resources(
    db: Session,
    *,
    owner_user_id: int,
    team_id: int,
    task_id: int,
    lease_token: str,
) -> list[dict[str, str]]:
    """验证任务启动新沙箱前，交接其直接 deploy 依赖占用的 Worker。

    资源只能由任务依赖图和运行时事件账本定位，不信任用户输入或结果快照中的 public_id。
    """

    team, verifier, _member = _require_live_task_lease(
        db,
        owner_user_id=owner_user_id,
        team_id=team_id,
        task_id=task_id,
        lease_token=lease_token,
    )
    verifier_input = _unjson(verifier.input_json, {})
    verifier_operation = str(verifier_input.get("operation") or "run_project_tests")
    if verifier_operation not in {"run_project_tests", "run_full_project_validation"}:
        raise AgentTeamStateError("当前任务不是可执行资源交接的验证操作")
    verifier_project_id = verifier_input.get("project_id")
    verifier_revision_id = verifier_input.get("source_revision_id")
    if not isinstance(verifier_project_id, int) or isinstance(verifier_project_id, bool):
        raise AgentTeamStateError("验证任务缺少有效 project_id")

    dependency_keys = list(dict.fromkeys(str(item) for item in _unjson(verifier.dependency_keys_json, [])))
    if not dependency_keys:
        return []
    dependency_rows = (
        db.query(AgentTeamTask)
        .filter(AgentTeamTask.team_id == int(team.id), AgentTeamTask.task_key.in_(dependency_keys))
        .all()
    )
    dependencies = {row.task_key: row for row in dependency_rows}
    if set(dependency_keys) != set(dependencies):
        raise AgentTeamStateError("验证任务的直接依赖不完整")

    from app.models.agent_capability import SandboxEnvironment

    candidates: list[tuple[AgentTeamTask, list[dict[str, str]]]] = []
    for dependency_key in dependency_keys:
        dependency = dependencies[dependency_key]
        dependency_member = db.get(AgentTeamMember, int(dependency.member_id))
        dependency_input = _unjson(dependency.input_json, {})
        is_deployment = bool(
            dependency_member is not None
            and dependency_member.address == "agent:sandbox_deployer"
            and str(dependency_input.get("operation") or "deploy") == "deploy"
        )
        if not is_deployment:
            continue
        if dependency.status != "completed":
            raise AgentTeamStateError("直接依赖的部署任务尚未完成")
        if dependency_input.get("project_id") != verifier_project_id:
            raise AgentTeamStateError("部署与验证任务的项目不一致")
        if dependency_input.get("source_revision_id") != verifier_revision_id:
            raise AgentTeamStateError("部署与验证任务的源码修订不一致")

        resources = _active_task_runtime_resources(db, int(dependency.id))
        for resource in resources:
            if resource["resource_type"] != "sandbox_environment":
                raise AgentTeamStateError("直接部署依赖包含不支持的运行资源")
            environment = (
                db.query(SandboxEnvironment).filter(SandboxEnvironment.public_id == resource["resource_id"]).first()
            )
            if environment is None:
                raise AgentTeamStateError("部署资源账本引用的沙箱不存在")
            config = _unjson(environment.agent_config_json, {})
            if not isinstance(config, dict):
                raise AgentTeamStateError("部署沙箱的代理配置格式无效")
            environment_team = config.get("agent_team")
            environment_team = environment_team if isinstance(environment_team, dict) else {}
            if int(environment.owner_id) != int(owner_user_id):
                raise AgentTeamStateError("部署沙箱所属账户不一致")
            if int(environment.project_id) != int(verifier_project_id):
                raise AgentTeamStateError("部署沙箱与验证任务的项目不一致")
            if environment.purpose != "deploy" or environment.agent_code != "sandbox_deployer":
                raise AgentTeamStateError("运行资源不是可交接的部署沙箱")
            if config.get("source_revision_id") != verifier_revision_id:
                raise AgentTeamStateError("部署沙箱与验证任务的源码修订不一致")
            if environment_team.get("team_id") != int(team.id) or environment_team.get("task_id") != int(dependency.id):
                raise AgentTeamStateError("部署沙箱的团队资源归属不一致")
        candidates.append((dependency, resources))

    released: list[dict[str, str]] = []
    for dependency, resources in candidates:
        if resources and not _cleanup_task_runtime_resources(
            db,
            team,
            dependency,
            reason="dependency_handoff_to_validation",
        ):
            raise AgentTeamStateError("依赖部署沙箱未达到终态，禁止启动验证沙箱")
        if _active_task_runtime_resources(db, int(dependency.id)):
            raise AgentTeamStateError("依赖部署沙箱的资源账本尚未关闭")
        for resource in resources:
            environment = (
                db.query(SandboxEnvironment).filter(SandboxEnvironment.public_id == resource["resource_id"]).first()
            )
            released.append(
                {
                    "public_id": resource["resource_id"],
                    "purpose": "deploy",
                    "status": str(environment.status if environment is not None else "stopped"),
                }
            )

    db.expire_all()
    _require_live_task_lease(
        db,
        owner_user_id=owner_user_id,
        team_id=team_id,
        task_id=task_id,
        lease_token=lease_token,
    )
    return released


def _cleanup_team_runtime_resources(db: Session, team: AgentTeam, *, reason: str) -> int:
    cleaned = 0
    tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
    for task in tasks:
        if not _active_task_runtime_resources(db, int(task.id)):
            continue
        if _cleanup_task_runtime_resources(db, team, task, reason=reason):
            cleaned += 1
    return cleaned


def cleanup_terminal_team_resources(db: Session, *, limit: int = 100) -> int:
    """重试取消/过期团队未完成的资源清理，不触碰正常完成团队。"""

    teams = (
        db.query(AgentTeam)
        .filter(AgentTeam.status.in_(("cancelled", "expired")))
        .order_by(AgentTeam.id.asc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    cleaned = 0
    for team in teams:
        cleaned += _cleanup_team_runtime_resources(db, team, reason=f"team_{team.status}_retry")
    return cleaned


def _team_or_raise(db: Session, user: User, team_id: int, *, lock: bool = False) -> AgentTeam:
    query = db.query(AgentTeam).filter(AgentTeam.id == int(team_id))
    if not _is_admin(db, user):
        query = query.filter(AgentTeam.user_id == int(user.id))
    row = query.with_for_update() if lock else query
    team = row.first()
    if team is None:
        raise AgentTeamNotFoundError("团队不存在或不属于当前账户")
    return team


def _serialize_member(row: AgentTeamMember) -> dict[str, Any]:
    return {
        "member_id": int(row.id),
        "member_key": row.member_key,
        "display_name": row.display_name,
        "address": row.address,
        "kind": row.kind,
        "role": row.role,
        "template_id": row.template_id,
        "template_version_id": row.template_version_id,
        "capabilities": _public(_unjson(row.capabilities_json, {})),
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def _member_release_snapshot(row: Optional[AgentTeamMember]) -> dict[str, Any]:
    if row is None or row.kind != "custom":
        return {}
    capabilities = _unjson(row.capabilities_json, {})
    return {
        "template_id": int(row.template_id) if row.template_id else None,
        "version_id": int(row.template_version_id) if row.template_version_id else None,
        "release_id": int(capabilities.get("release_id") or 0) or None,
        "package_checksum": str(capabilities.get("package_checksum") or ""),
        "template_checksum": str(capabilities.get("template_checksum") or ""),
    }


def _serialize_task(row: AgentTeamTask, member_key: str = "") -> dict[str, Any]:
    return {
        "task_id": int(row.id),
        "task_key": row.task_key,
        "member_id": int(row.member_id),
        # 前端子Agent工作卡片按 member_key 匹配成员;映射由调用方批量构建,避免 N+1
        "member_key": member_key,
        "title": row.title,
        "instructions": _public(row.instructions),
        "depends_on": _unjson(row.dependency_keys_json, []),
        "input": _public(_unjson(row.input_json, {})),
        "status": row.status,
        "priority": row.priority,
        "attempt_count": row.attempt_count,
        "max_attempts": row.max_attempts,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
        "result": _public(_unjson(row.result_json, {})),
        "artifacts": _public(_unjson(row.artifacts_json, [])),
        "errors": _public(_unjson(row.errors_json, [])),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def _serialize_event(row: AgentTeamEvent) -> dict[str, Any]:
    return {
        "event_id": int(row.id),
        "team_id": int(row.team_id),
        "task_id": row.task_id,
        "member_id": row.member_id,
        "event_type": row.event_type,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "actor_address": row.actor_address,
        "trace_id": row.trace_id,
        "message_id": row.message_id,
        "correlation_id": row.correlation_id,
        "detail": _public(_unjson(row.detail_json, {})),
        "created_at": row.create_time.isoformat() if row.create_time else None,
    }


def _serialize_message(row: AgentMeshMessage) -> dict[str, Any]:
    return {
        "ledger_id": int(row.id),
        "message_id": row.message_id,
        "trace_id": row.trace_id,
        "correlation_id": row.correlation_id,
        "causation_id": row.causation_id,
        "sent_from": row.sent_from,
        "send_to": row.send_to,
        "message_type": row.message_type,
        "subject": row.subject,
        "status": row.status,
        "payload": _public(_unjson(row.payload_json, {})),
        "context": _public(_unjson(row.context_json, {})),
        "artifacts": _public(_unjson(row.artifacts_json, [])),
        "errors": _public(_unjson(row.errors_json, [])),
        "create_time": row.create_time.isoformat() if row.create_time else None,
        "created_at": row.create_time.isoformat() if row.create_time else None,
        "update_time": row.update_time.isoformat() if row.update_time else None,
    }


def _team_message_page(
    db: Session,
    team: AgentTeam,
    *,
    before_id: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    page_size = min(max(1, int(limit)), 500)
    scoped = db.query(AgentMeshMessage).filter(
        AgentMeshMessage.user_id == int(team.user_id),
        AgentMeshMessage.trace_id == team.trace_id,
    )
    total = scoped.count()
    page_query = scoped
    if int(before_id or 0) > 0:
        page_query = page_query.filter(AgentMeshMessage.id < int(before_id))
    newest_first = page_query.order_by(AgentMeshMessage.id.desc()).limit(page_size + 1).all()
    has_more = len(newest_first) > page_size
    page_rows = newest_first[:page_size]
    next_before_id = int(page_rows[-1].id) if has_more and page_rows else None
    page_rows.reverse()
    return {
        "items": [_serialize_message(row) for row in page_rows],
        "total": total,
        "has_more": has_more,
        "next_before_id": next_before_id,
        "page_size": page_size,
    }


def _team_out(db: Session, team: AgentTeam, *, include_events: bool = False) -> dict[str, Any]:
    members = (
        db.query(AgentTeamMember).filter(AgentTeamMember.team_id == team.id).order_by(AgentTeamMember.id.asc()).all()
    )
    tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).order_by(AgentTeamTask.id.asc()).all()
    member_key_by_id = {int(item.id): item.member_key for item in members}
    output = {
        "team_id": int(team.id),
        "user_id": int(team.user_id),
        "surface": team.surface,
        "session_id": team.session_key,
        "title": team.title,
        "objective": _public(team.objective),
        "status": team.status,
        "max_active_children": team.max_active_children,
        "max_attempts": team.max_attempts,
        "priority": team.priority,
        "trace_id": team.trace_id,
        "deadline_at": team.deadline_at.isoformat() if team.deadline_at else None,
        "started_at": team.started_at.isoformat() if team.started_at else None,
        "completed_at": team.completed_at.isoformat() if team.completed_at else None,
        "summary": _public(_unjson(team.summary_json, {})),
        "error": _public(_unjson(team.error_json, {})),
        "created_at": team.create_time.isoformat() if team.create_time else None,
        "updated_at": team.update_time.isoformat() if team.update_time else None,
        "archived_at": team.archived_at.isoformat() if team.archived_at else None,
        "members": [_serialize_member(item) for item in members],
        "tasks": [_serialize_task(item, member_key_by_id.get(int(item.member_id), "")) for item in tasks],
        "counts": _task_counts(tasks),
    }
    if include_events:
        events = (
            db.query(AgentTeamEvent).filter(AgentTeamEvent.team_id == team.id).order_by(AgentTeamEvent.id.asc()).all()
        )
        output["events"] = [_serialize_event(item) for item in events]
        message_page = _team_message_page(db, team)
        output["messages"] = message_page.pop("items")
        output["message_page"] = message_page
    return output


def _create_team(db: Session, user: User, payload: AgentTeamCreateIn) -> dict[str, Any]:
    payload = _normalize_task_inputs(payload)
    payload = _normalize_verification_graph(payload)
    payload = _normalize_task_inputs(payload)
    _assert_surface(db, user, payload.surface)
    queue_limit = int(getattr(settings, "agent_team_max_queue_length", 100))
    queued_count = (
        db.query(AgentTeamTask)
        .join(AgentTeam, AgentTeamTask.team_id == AgentTeam.id)
        .filter(
            AgentTeam.user_id == int(user.id),
            AgentTeamTask.status.in_(("waiting_dependency", "queued")),
        )
        .count()
    )
    if queued_count + len(payload.tasks) > queue_limit:
        raise AgentTeamValidationError("团队任务数超过队列上限")
    member_keys = [item.member_key for item in payload.members]
    if len(member_keys) != len(set(member_keys)):
        raise AgentTeamValidationError("成员 member_key 不能重复")
    member_map = {item.member_key: item for item in payload.members}
    if not any(item.role in {"verifier", "summarizer"} for item in payload.members):
        raise AgentTeamValidationError("团队必须至少包含一个 verifier 或 summarizer 成员")
    for item in payload.tasks:
        if item.member_key not in member_map:
            raise AgentTeamValidationError(f"任务 {item.task_key} 引用不存在成员 {item.member_key}")
        _validate_safe_input(item.input)
    _topological_order(payload.tasks)
    if payload.deadline_at is not None:
        deadline = payload.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= _now():
            raise AgentTeamValidationError("团队截止时间必须晚于当前时间")

    validated_members: dict[str, tuple[str, Optional[int], Optional[int], dict[str, Any]]] = {}
    for item in payload.members:
        if item.address == "agent:operations" and payload.surface != "admin":
            raise AgentTeamAccessError("运维子 Agent 只能在管理小菱会话中创建")
        _validate_safe_input(item.capabilities)
        target = _validate_target(db, user, item.address)
        kind, template_id, version_id, _ = target
        if item.template_id is not None and item.template_id != template_id:
            raise AgentTeamValidationError(f"成员 {item.member_key} 的模板与已发布目标不匹配")
        if item.template_version_id is not None and item.template_version_id != version_id:
            raise AgentTeamValidationError(f"成员 {item.member_key} 的模板版本与已发布目标不匹配")
        validated_members[item.member_key] = target
    verification_member_keys = {item.member_key for item in payload.members if item.role in {"verifier", "summarizer"}}
    if not any(item.member_key in verification_member_keys for item in payload.tasks):
        raise AgentTeamValidationError("团队必须至少包含一个 verifier 或 summarizer 任务")
    _validate_verification_coverage(payload)
    for item in payload.tasks:
        _validate_task_scope(db, user, item, member_map[item.member_key].address)

    _ensure_session(db, user, surface=payload.surface, session_key=payload.session_id, title=payload.title)
    trace_id = f"team_{uuid.uuid4().hex}"
    effective_max_attempts = min(
        int(payload.max_attempts),
        int(getattr(settings, "agent_team_default_max_attempts", payload.max_attempts)),
    )
    team = AgentTeam(
        user_id=int(user.id),
        surface=payload.surface,
        session_key=payload.session_id,
        title=payload.title.strip(),
        objective=payload.objective.strip(),
        status="queued",
        max_active_children=min(
            int(payload.max_active_children), int(getattr(settings, "agent_team_max_active_children", 3))
        ),
        max_attempts=effective_max_attempts,
        priority=int(payload.priority),
        trace_id=trace_id,
        deadline_at=payload.deadline_at,
    )
    db.add(team)
    db.flush()
    member_rows: dict[str, AgentTeamMember] = {}
    for item in payload.members:
        kind, template_id, version_id, capabilities = validated_members[item.member_key]
        row = AgentTeamMember(
            team_id=int(team.id),
            member_key=item.member_key,
            display_name=item.display_name.strip(),
            address=item.address,
            kind=kind,
            role=item.role,
            template_id=item.template_id or template_id,
            template_version_id=item.template_version_id or version_id,
            capabilities_json=_json(
                {
                    **capabilities,
                    # 用户声明只作为审计信息保留，不能覆盖已发布版本的有效能力。
                    "requested_capabilities": _public(item.capabilities),
                }
            ),
            status="created",
        )
        db.add(row)
        db.flush()
        member_rows[item.member_key] = row
    for item in payload.tasks:
        status = "queued" if not item.depends_on else "waiting_dependency"
        row = AgentTeamTask(
            team_id=int(team.id),
            member_id=int(member_rows[item.member_key].id),
            task_key=item.task_key,
            title=item.title.strip(),
            instructions=item.instructions.strip(),
            dependency_keys_json=_json(item.depends_on),
            input_json=_json(item.input),
            status=status,
            priority=int(item.priority),
            # 团队级上限是整个工作图的硬上限，避免任务节点通过更大的
            # max_attempts 绕过团队重试预算；任务级值只能进一步收紧它。
            max_attempts=min(int(item.max_attempts), effective_max_attempts),
            next_attempt_at=_now() if status == "queued" else None,
        )
        db.add(row)
    _event(
        db,
        team,
        "team.created",
        to_status="queued",
        actor_address=f"user:{user.id}",
        detail={"task_count": len(payload.tasks), "origin_trace_id": payload.trace_id},
    )
    db.commit()
    db.refresh(team)
    return _team_out(db, team, include_events=True)


def create_team(db: Session, user: User, payload: AgentTeamCreateIn) -> dict[str, Any]:
    try:
        result = _create_team(db, user, payload)
    except Exception:
        db.rollback()
        raise
    observe_event("team_created", labels={"surface": str(payload.surface)})
    return result


def list_teams(
    db: Session,
    user: User,
    *,
    surface: str = "",
    session_id: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    query = db.query(AgentTeam)
    if not _is_admin(db, user) or session_id:
        query = query.filter(AgentTeam.user_id == int(user.id))
    if surface:
        if surface not in {"user", "admin"}:
            raise AgentTeamValidationError("surface 只能是 user 或 admin")
        query = query.filter(AgentTeam.surface == surface)
    if session_id:
        query = query.filter(AgentTeam.session_key == session_id)
    if status:
        query = query.filter(AgentTeam.status == status)
    total = query.count()
    rows = query.order_by(AgentTeam.id.desc()).offset(max(0, offset)).limit(min(max(1, limit), 100)).all()
    return {"items": [_team_out(db, row) for row in rows], "total": total}


def get_team(db: Session, user: User, team_id: int) -> dict[str, Any]:
    return _team_out(db, _team_or_raise(db, user, team_id), include_events=True)


def list_team_events(
    db: Session,
    user: User,
    team_id: int,
    *,
    after_id: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """增量读取团队事件账本,支撑前端实时可视化(思考城市/悬浮窗时间线)。

    只返回 after_id 之后的新事件,避免前端每秒轮询全量 team+events+messages。
    """
    team = _team_or_raise(db, user, team_id)
    page_size = min(max(1, int(limit)), 500)
    query = db.query(AgentTeamEvent).filter(AgentTeamEvent.team_id == int(team.id))
    if int(after_id or 0) > 0:
        query = query.filter(AgentTeamEvent.id > int(after_id))
    rows = query.order_by(AgentTeamEvent.id.asc()).limit(page_size + 1).all()
    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    items = [_serialize_event(row) for row in page_rows]
    next_after_id = int(page_rows[-1].id) if page_rows else int(after_id or 0)
    return {
        "items": items,
        "has_more": has_more,
        "next_after_id": next_after_id,
        "page_size": page_size,
        "team_status": team.status,
    }


def list_team_messages(
    db: Session,
    user: User,
    team_id: int,
    *,
    before_id: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    team = _team_or_raise(db, user, team_id)
    return _team_message_page(db, team, before_id=before_id, limit=limit)


def _promote_dependencies(db: Session, team: AgentTeam) -> None:
    tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
    by_key = {row.task_key: row for row in tasks}
    now = _now()
    changed = False
    while True:
        wave_changed = False
        for row in tasks:
            if row.status != "waiting_dependency":
                continue
            deps = _unjson(row.dependency_keys_json, [])
            dep_rows = [by_key.get(str(key)) for key in deps]
            if any(dep is None for dep in dep_rows):
                row.status = "blocked"
                row.completed_at = now
                row.next_attempt_at = None
                row.errors_json = _json([{"code": "missing_dependency", "message": "依赖任务不存在"}])
                wave_changed = True
            elif any(dep.status in {"failed", "blocked", "dead_letter", "cancelled", "expired"} for dep in dep_rows):
                previous = row.status
                row.status = "blocked"
                row.completed_at = now
                row.next_attempt_at = None
                row.errors_json = _json([{"code": "dependency_failed", "message": "前置任务失败，任务被阻断"}])
                _event(
                    db,
                    team,
                    "task.blocked",
                    task=row,
                    from_status=previous,
                    to_status="blocked",
                    detail={"depends_on": deps},
                )
                wave_changed = True
            elif all(dep.status == "completed" for dep in dep_rows):
                previous = row.status
                row.status = "queued"
                row.next_attempt_at = now
                _event(
                    db,
                    team,
                    "task.queued",
                    task=row,
                    from_status=previous,
                    to_status="queued",
                    detail={"reason": "dependencies_completed"},
                )
                wave_changed = True
        changed = changed or wave_changed
        if not wave_changed:
            break
    if changed:
        db.flush()


def _refresh_member_statuses(db: Session, team: AgentTeam, *, reclaim: bool = False) -> None:
    members = db.query(AgentTeamMember).filter(AgentTeamMember.team_id == team.id).all()
    tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
    by_member: dict[int, list[AgentTeamTask]] = {}
    for task in tasks:
        by_member.setdefault(int(task.member_id), []).append(task)
    now = _now()
    for member in members:
        member_tasks = by_member.get(int(member.id), [])
        statuses = {item.status for item in member_tasks}
        if reclaim:
            member.status = "reclaimed"
            member.completed_at = member.completed_at or now
        elif "running" in statuses:
            member.status = "running"
            member.started_at = member.started_at or now
        elif statuses & {"failed", "dead_letter", "blocked", "expired"}:
            member.status = "failed"
            member.completed_at = member.completed_at or now
        elif member_tasks and all(item.status == "completed" for item in member_tasks):
            member.status = "completed"
            member.completed_at = member.completed_at or now
        elif statuses & {"queued", "waiting_dependency"}:
            member.status = "queued"
            member.completed_at = None
        elif statuses and statuses <= {"cancelled"}:
            member.status = "reclaimed"
            member.completed_at = member.completed_at or now
        else:
            member.status = "created"


def _refresh_team_status(db: Session, team: AgentTeam) -> None:
    tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
    if not tasks or team.status in {"cancelled", "expired"}:
        return
    members = {
        int(item.id): item for item in db.query(AgentTeamMember).filter(AgentTeamMember.team_id == team.id).all()
    }
    previous = team.status
    running = [item for item in tasks if item.status == "running"]
    failed = [item for item in tasks if item.status in {"failed", "dead_letter", "blocked", "expired"}]
    verification = [
        item
        for item in tasks
        if (members.get(int(item.member_id)) and members[int(item.member_id)].role in {"verifier", "summarizer"})
    ]
    workers = [item for item in tasks if item not in verification]

    if not verification:
        team.status = "failed"
        team.completed_at = team.completed_at or _now()
        team.error_json = _json({"reason": "missing_verifier_or_summarizer"})
    elif all(item.status == "completed" for item in tasks) and all(item.status == "completed" for item in verification):
        team.status = "completed"
        team.completed_at = team.completed_at or _now()
        verification_results = [
            {"task_key": item.task_key, "result": _public(_unjson(item.result_json, {}))} for item in verification
        ]
        team.summary_json = _json(
            {
                "completed_tasks": len(tasks),
                "verification_results": verification_results,
                "final_result": verification_results[-1]["result"] if verification_results else {},
            }
        )
    elif failed and not running:
        team.status = "failed"
        team.completed_at = team.completed_at or _now()
        team.error_json = _json(
            {
                "failed_tasks": [item.task_key for item in failed],
                "errors": [_public(_unjson(item.errors_json, [])) for item in failed],
            }
        )
    elif (
        verification
        and all(item.status == "completed" for item in workers)
        and any(item.status in {"waiting_dependency", "queued", "running"} for item in verification)
    ):
        team.status = "verifying"
        team.started_at = team.started_at or _now()
    elif running:
        team.status = "running"
        team.started_at = team.started_at or _now()
    else:
        team.status = "queued"

    if previous != team.status:
        _event(
            db,
            team,
            "team.status_changed",
            from_status=previous,
            to_status=team.status,
            detail={"counts": _task_counts(tasks)},
        )


def _mesh_ready(db: Session) -> bool:
    # 复用当前事务连接；从 Engine 另取连接会在 SQLite 内存库中回滚同一底层事务。
    return bool(sa_inspect(db.connection()).has_table(AgentMeshMessage.__tablename__))


def _request_message_id(team: AgentTeam, task: AgentTeamTask) -> str:
    return f"team-{team.id}-task-{task.id}-attempt-{task.attempt_count}-request"


def _mesh_event(
    db: Session,
    *,
    team: AgentTeam,
    message_id: str,
    status: str,
    actor: str,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    db.add(
        AgentMeshMessageEvent(
            message_id=message_id,
            user_id=int(team.user_id),
            trace_id=team.trace_id,
            status=status,
            actor_address=actor,
            detail_json=_json(_public(detail or {})),
        )
    )


def _persist_task_request_message(
    db: Session,
    team: AgentTeam,
    task: AgentTeamTask,
    member: Optional[AgentTeamMember],
    *,
    lease_token: str,
    lease_expires_at: datetime,
    dependency_context: dict[str, Any],
) -> str:
    if not _mesh_ready(db):
        return ""
    message_id = _request_message_id(team, task)
    existing = db.query(AgentMeshMessage).filter(AgentMeshMessage.message_id == message_id).first()
    if existing is not None:
        return existing.message_id
    now = _now()
    raw_input = _unjson(task.input_json, {})
    context = {
        "team_id": int(team.id),
        "task_id": int(task.id),
        "member_id": int(task.member_id),
        "source_revision_id": raw_input.get("source_revision_id") if isinstance(raw_input, dict) else None,
        "run_id": team.trace_id,
        "member_snapshot": _member_release_snapshot(member),
    }
    row = AgentMeshMessage(
        message_id=message_id,
        user_id=int(team.user_id),
        schema_version="1.0",
        idempotency_key=message_id,
        trace_id=team.trace_id,
        correlation_id=f"team:{team.id}:task:{task.id}",
        causation_id="",
        sent_from=f"session:{team.surface}:{team.session_key}",
        send_to=member.address if member else "agent:orchestrator",
        message_type="task.request",
        priority="normal",
        subject=task.title[:240],
        payload_json=_json(
            _public(
                {
                    "title": task.title,
                    "instructions": task.instructions,
                    "input": raw_input,
                    "dependency_context": dependency_context,
                    "member_snapshot": _member_release_snapshot(member),
                }
            )
        ),
        context_json=_json(context),
        artifacts_json="[]",
        errors_json="[]",
        status="processing",
        requires_ack=0,
        max_attempts=1,
        attempt_count=1,
        delivered_at=now,
        acknowledged_at=now,
        processing_at=now,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
    )
    db.add(row)
    _mesh_event(db, team=team, message_id=message_id, status="queued", actor=row.sent_from)
    _mesh_event(db, team=team, message_id=message_id, status="delivered", actor=row.send_to)
    _mesh_event(db, team=team, message_id=message_id, status="processing", actor=row.send_to)
    return message_id


def _mark_task_request_terminal(
    db: Session,
    team: AgentTeam,
    task: AgentTeamTask,
    member: Optional[AgentTeamMember],
    *,
    success: bool,
    error: str,
    now: datetime,
) -> str:
    if not _mesh_ready(db):
        return ""
    message_id = _request_message_id(team, task)
    row = db.query(AgentMeshMessage).filter(AgentMeshMessage.message_id == message_id).first()
    if row is None:
        return ""
    row.status = "completed" if success else "failed"
    row.completed_at = now
    row.lease_token = None
    row.lease_expires_at = None
    row.last_error = "" if success else error[:2000]
    _mesh_event(
        db,
        team=team,
        message_id=message_id,
        status=row.status,
        actor=member.address if member else "system:agent_team",
        detail={"error": error if not success else ""},
    )
    return message_id


def _add_terminal_mesh_message(
    db: Session,
    team: AgentTeam,
    *,
    message_id: str,
    sent_from: str,
    send_to: str,
    message_type: str,
    subject: str,
    payload: dict[str, Any],
    context: dict[str, Any],
    artifacts: list[Any],
    errors: list[Any],
    success: bool,
    causation_id: str,
    now: datetime,
) -> None:
    if db.query(AgentMeshMessage).filter(AgentMeshMessage.message_id == message_id).first() is not None:
        return
    status = "completed" if success else "failed"
    # 回投给发起会话的结果必须进入 inbox 生命周期(queued→delivered→ack→completed),
    # 否则前端永远拉不到,小菱不会自动把子 Agent 结论汇报回对话。
    # 成员间移交(coordination)保持终态仅作账本证据,不触发执行。
    if send_to.startswith("session:"):
        status = "queued"
    db.add(
        AgentMeshMessage(
            message_id=message_id,
            user_id=int(team.user_id),
            schema_version="1.0",
            idempotency_key=message_id,
            trace_id=team.trace_id,
            correlation_id=f"team:{team.id}",
            causation_id=causation_id,
            sent_from=sent_from,
            send_to=send_to,
            message_type=message_type,
            priority="normal",
            subject=subject[:240],
            payload_json=_json(_public(payload)),
            context_json=_json(context),
            artifacts_json=_json(_public(artifacts)),
            errors_json=_json(_public(errors)),
            status=status,
            requires_ack=0,
            max_attempts=1,
            attempt_count=1,
            completed_at=None if status == "queued" else now,
        )
    )
    _mesh_event(db, team=team, message_id=message_id, status="queued", actor=sent_from)
    _mesh_event(db, team=team, message_id=message_id, status=status, actor=send_to)


def _persist_task_result_message(
    db: Session,
    team: AgentTeam,
    task: AgentTeamTask,
    member: Optional[AgentTeamMember],
    *,
    result: dict[str, Any],
    success: bool,
    now: datetime,
) -> None:
    """把团队结果投影回既有 Mesh 账本，供会话悬浮时间线读取。"""

    if not _mesh_ready(db):
        return
    source = member.address if member else "agent:orchestrator"
    request_id = _request_message_id(team, task)
    context = {
        "team_id": int(team.id),
        "task_id": int(task.id),
        "member_id": int(task.member_id),
        "run_id": team.trace_id,
    }
    artifacts = list((result or {}).get("artifacts") or [])
    errors = list((result or {}).get("errors") or [])
    result_id = f"team-{team.id}-task-{task.id}-attempt-{task.attempt_count}-result"
    _add_terminal_mesh_message(
        db,
        team,
        message_id=result_id,
        sent_from=source,
        send_to=f"session:{team.surface}:{team.session_key}",
        message_type="task.result" if success else "task.error",
        subject=f"{task.title}执行结果",
        payload=result or {},
        context=context,
        artifacts=artifacts,
        errors=errors,
        success=success,
        causation_id=request_id,
        now=now,
    )
    if not success:
        return
    tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
    members = {
        int(item.id): item for item in db.query(AgentTeamMember).filter(AgentTeamMember.team_id == team.id).all()
    }
    for dependent in tasks:
        if task.task_key not in set(_unjson(dependent.dependency_keys_json, [])):
            continue
        target = members.get(int(dependent.member_id))
        if target is None:
            continue
        handoff_id = f"team-{team.id}-task-{task.id}-attempt-{task.attempt_count}-to-{dependent.id}"
        _add_terminal_mesh_message(
            db,
            team,
            message_id=handoff_id,
            sent_from=source,
            send_to=target.address,
            message_type="coordination",
            subject=f"{task.title}结果移交给{dependent.title}",
            payload={
                "source_task_key": task.task_key,
                "target_task_key": dependent.task_key,
                "result": result or {},
            },
            context={**context, "task_id": int(dependent.id), "member_id": int(dependent.member_id)},
            artifacts=artifacts,
            errors=[],
            success=True,
            causation_id=result_id,
            now=now,
        )


def _task_strategy_hash(task: AgentTeamTask) -> str:
    payload = {
        "instructions": task.instructions,
        "input": _unjson(task.input_json, {}),
        "member_id": int(task.member_id),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _apply_execution_strategy(
    task: AgentTeamTask,
    member: Optional[AgentTeamMember],
    *,
    instruction: str,
    error: str,
    automatic: bool,
    mode: str = "",
) -> dict[str, Any]:
    """把改道固化到结构化输入，执行 Handler 会真正消费该字段。"""

    raw_input = _unjson(task.input_json, {})
    if not isinstance(raw_input, dict):
        raw_input = {}
    raw_input = dict(raw_input)
    previous = raw_input.get("_execution_strategy")
    previous = previous if isinstance(previous, dict) else {}
    address = member.address if member else ""
    failure = (error or "").lower()
    if not mode:
        if address.endswith(("sandbox_deployer", "test_verifier")):
            mode = "fresh_sandbox_alternate_worker"
        elif any(token in failure for token in ("timeout", "timed out", "超时", "限流", "rate")):
            mode = "fresh_session_reduced_batch"
        else:
            mode = "alternate_reasoning_with_failure_context"

    changes = ["inject_failure_context", "use_fresh_execution_session"]
    if "worker_code" in raw_input:
        raw_input["worker_code"] = ""
        changes.append("release_pinned_worker")
    if isinstance(raw_input.get("page_size"), int) and raw_input["page_size"] > 10:
        raw_input["page_size"] = max(10, raw_input["page_size"] // 2)
        changes.append("reduce_page_size")
    experience = str(raw_input.get("experience") or "").strip()
    if address.startswith("custom:") or address == "agent:code_reviewer":
        raw_input["experience"] = f"{experience}\n本次改道策略：{instruction}".strip()
        changes.append("inject_strategy_into_model_context")

    strategy = {
        "version": 1,
        "attempt": int(task.attempt_count or 0) + 1,
        "mode": mode,
        "instruction": instruction,
        "previous_error": str(_public(error))[:1000],
        "previous_mode": str(previous.get("mode") or ""),
        "automatic": bool(automatic),
        "changes": changes,
    }
    raw_input["_execution_strategy"] = strategy
    _validate_safe_input(raw_input)
    task.input_json = _json(raw_input)
    return strategy


def _record_task_strategy(
    db: Session,
    team: AgentTeam,
    task: AgentTeamTask,
    member: Optional[AgentTeamMember],
    *,
    result: dict[str, Any],
    success: bool,
    error: str,
) -> None:
    """只有持有有效租约并形成真实结果的团队任务才进入账户策略记忆。"""

    try:
        from app.services import strategy_learning_service

        raw_input = _unjson(task.input_json, {})
        project_id = raw_input.get("project_id") if isinstance(raw_input, dict) else None
        if not isinstance(project_id, int) or isinstance(project_id, bool):
            project_id = None
        address = member.address if member else "agent:orchestrator"
        agent_code = address.split(":", 1)[1] if ":" in address else address
        summary = str((result or {}).get("summary") or error or ("任务执行成功" if success else "任务执行失败"))
        with db.begin_nested():
            strategy_learning_service.record_tool_outcome(
                db,
                owner_user_id=int(team.user_id),
                project_id=project_id,
                agent_code=agent_code,
                tool_name="agent_team_task",
                arguments={
                    "tool": address,
                    "operation": task.task_key,
                    "mode": _task_strategy_hash(task),
                    "source_revision_id": raw_input.get("source_revision_id") if isinstance(raw_input, dict) else None,
                },
                outcome="success" if success else "failure",
                summary=summary,
                evidence_ref=f"agent-team:{team.id}:task:{task.id}:attempt:{task.attempt_count}",
                failure_kind="" if success else strategy_learning_service.classify_failure(error or summary),
                verified_async=True,
            )
    except Exception:
        # 策略学习是旁路；不能篡改已获得租约保护的真实任务终态。
        return


def claim_next_task(db: Session, team_id: int, *, lease_seconds: Optional[int] = None) -> Optional[dict[str, Any]]:
    team = db.query(AgentTeam).filter(AgentTeam.id == int(team_id)).with_for_update().first()
    if team is None or team.status in _TERMINAL_TEAM:
        return None
    now = _now()
    if team.deadline_at is not None:
        deadline = team.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= now:
            _expire_team(db, team, reason="deadline_reached")
            db.commit()
            _cleanup_team_runtime_resources(db, team, reason="deadline_reached")
            return None
    _promote_dependencies(db, team)
    active_count = (
        db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id, AgentTeamTask.status == "running").count()
    )
    if active_count >= int(team.max_active_children or 1):
        db.commit()
        return None
    candidate = (
        db.query(AgentTeamTask)
        .filter(AgentTeamTask.team_id == team.id, AgentTeamTask.status == "queued")
        .filter(or_(AgentTeamTask.next_attempt_at.is_(None), AgentTeamTask.next_attempt_at <= now))
        .order_by(AgentTeamTask.priority.desc(), AgentTeamTask.create_time.asc(), AgentTeamTask.id.asc())
        .first()
    )
    if candidate is None:
        _refresh_team_status(db, team)
        db.commit()
        return None
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(seconds=max(1, int(lease_seconds or settings.agent_team_task_lease_seconds)))
    result = db.execute(
        update(AgentTeamTask)
        .where(AgentTeamTask.id == candidate.id, AgentTeamTask.status == "queued")
        .values(
            status="running",
            attempt_count=AgentTeamTask.attempt_count + 1,
            lease_token=token,
            lease_expires_at=expires,
            started_at=candidate.started_at or now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.refresh(candidate)
    dependency_context = _dependency_context(db, team, candidate)
    team.started_at = team.started_at or now
    member = db.get(AgentTeamMember, candidate.member_id)
    if member:
        member.status = "running"
        member.started_at = member.started_at or now
    request_message_id = _persist_task_request_message(
        db,
        team,
        candidate,
        member,
        lease_token=token,
        lease_expires_at=expires,
        dependency_context=dependency_context,
    )
    _event(
        db,
        team,
        "task.claimed",
        task=candidate,
        member=member,
        from_status="queued",
        to_status="running",
        detail={"attempt": candidate.attempt_count},
        message_id=request_message_id or None,
        correlation_id=f"team:{team.id}:task:{candidate.id}",
    )
    _refresh_member_statuses(db, team)
    _refresh_team_status(db, team)
    db.commit()
    return {
        "team_id": int(team.id),
        "task_id": int(candidate.id),
        "task_key": candidate.task_key,
        "member_id": int(candidate.member_id),
        "address": member.address if member else "",
        "lease_token": token,
        "trace_id": team.trace_id,
        "input": _unjson(candidate.input_json, {}),
        "dependency_context": dependency_context,
        "instructions": candidate.instructions,
        "title": candidate.title,
        "attempt_count": candidate.attempt_count,
        "request_message_id": request_message_id,
        "member_snapshot": _member_release_snapshot(member),
    }


def complete_task(
    db: Session,
    team_id: int,
    task_id: int,
    *,
    lease_token: str,
    result: dict[str, Any],
    success: bool = True,
    error: str = "",
) -> dict[str, Any]:
    team = db.query(AgentTeam).filter(AgentTeam.id == int(team_id)).with_for_update().first()
    task = (
        db.query(AgentTeamTask)
        .filter(AgentTeamTask.id == int(task_id), AgentTeamTask.team_id == int(team_id))
        .with_for_update()
        .first()
    )
    if team is None or task is None:
        raise AgentTeamNotFoundError("团队任务不存在")
    if team.status in {"cancelled", "expired"}:
        raise AgentTeamLeaseError("团队已取消或过期，任务租约失效")
    if task.status != "running" or not lease_token or task.lease_token != lease_token:
        raise AgentTeamLeaseError("团队任务租约已失效")
    now = _now()
    task.lease_token = None
    task.lease_expires_at = None
    member = db.get(AgentTeamMember, task.member_id)
    normalized_result = dict(result or {})
    task.result_json = _json(normalized_result)
    task.artifacts_json = _json(normalized_result.get("artifacts") or [])
    request_message_id = _mark_task_request_terminal(
        db,
        team,
        task,
        member,
        success=success,
        error=error or str(normalized_result.get("summary") or ""),
        now=now,
    )
    if success:
        task.status = "completed"
        task.completed_at = now
        task.errors_json = "[]"
        _event(
            db,
            team,
            "task.completed",
            task=task,
            member=member,
            from_status="running",
            to_status="completed",
            detail={"result": normalized_result},
            message_id=request_message_id or None,
        )
        _persist_task_result_message(db, team, task, member, result=normalized_result, success=True, now=now)
    else:
        result_errors = list(normalized_result.get("errors") or [])
        result_errors.append(
            {
                "code": "execution_failed",
                "message": error or str(normalized_result.get("summary") or "任务执行失败"),
            }
        )
        task.errors_json = _json(result_errors)
        retryable = normalized_result.get("retryable") is not False
        if retryable and int(task.attempt_count or 0) < int(task.max_attempts or team.max_attempts or 1):
            strategy_change = str(normalized_result.get("strategy_change") or "").strip()
            if len(strategy_change) < 8:
                strategy_change = "先复核前置依赖并缩小输入范围，再执行本任务"
            execution_strategy = _apply_execution_strategy(
                task,
                member,
                instruction=strategy_change,
                error=error or str(normalized_result.get("summary") or ""),
                automatic=True,
            )
            task.instructions = (
                f"{task.instructions}\n\n[小菱自动改道第 {task.attempt_count + 1} 次] {strategy_change}"
            )[:12000]
            task.status = "queued"
            task.completed_at = None
            raw_retry_after = normalized_result.get("retry_after_seconds")
            retry_after_seconds = (
                max(0, min(int(raw_retry_after), 300))
                if isinstance(raw_retry_after, (int, float)) and not isinstance(raw_retry_after, bool)
                else 0
            )
            task.next_attempt_at = now + timedelta(seconds=retry_after_seconds)
            _event(
                db,
                team,
                "task.retry_queued",
                task=task,
                member=member,
                from_status="running",
                to_status="queued",
                detail={
                    "error": error,
                    "attempt": task.attempt_count,
                    "strategy_change": strategy_change,
                    "execution_strategy": execution_strategy,
                    "automatic": True,
                    "retry_after_seconds": retry_after_seconds,
                },
                message_id=request_message_id or None,
            )
            _persist_task_result_message(db, team, task, member, result=normalized_result, success=False, now=now)
        elif retryable:
            task.status = "dead_letter"
            task.completed_at = now
            _event(
                db,
                team,
                "task.dead_letter",
                task=task,
                member=member,
                from_status="running",
                to_status="dead_letter",
                detail={"error": error},
                message_id=request_message_id or None,
            )
            _persist_task_result_message(db, team, task, member, result=normalized_result, success=False, now=now)
        else:
            task.status = "failed"
            task.completed_at = now
            task.next_attempt_at = None
            _event(
                db,
                team,
                "task.failed",
                task=task,
                member=member,
                from_status="running",
                to_status="failed",
                detail={"error": error, "retryable": False, "next_action": normalized_result.get("next_action")},
                message_id=request_message_id or None,
            )
            _persist_task_result_message(db, team, task, member, result=normalized_result, success=False, now=now)
    _record_task_strategy(
        db,
        team,
        task,
        member,
        result=normalized_result,
        success=success,
        error=error,
    )
    _promote_dependencies(db, team)
    _refresh_member_statuses(db, team)
    _refresh_team_status(db, team)
    db.commit()
    return _team_out(db, team)


def _expire_team(db: Session, team: AgentTeam, *, reason: str) -> None:
    if team.status in _TERMINAL_TEAM:
        return
    now = _now()
    previous = team.status
    tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
    for task in tasks:
        if task.status in _TERMINAL_TASK:
            continue
        member = db.get(AgentTeamMember, task.member_id)
        task.status = "expired"
        task.completed_at = now
        task.next_attempt_at = None
        task.lease_token = None
        task.lease_expires_at = None
        _mark_task_request_terminal(
            db,
            team,
            task,
            member,
            success=False,
            error="团队已超过截止时间",
            now=now,
        )
        _event(
            db,
            team,
            "task.expired",
            task=task,
            member=member,
            from_status="running" if task.started_at else "queued",
            to_status="expired",
            detail={"reason": reason},
        )
    team.status = "expired"
    team.completed_at = now
    team.error_json = _json({"reason": reason})
    _refresh_member_statuses(db, team, reclaim=True)
    _event(
        db,
        team,
        "team.expired",
        from_status=previous,
        to_status="expired",
        detail={"reason": reason},
    )


def expire_due_teams(db: Session, *, limit: int = 100) -> int:
    now = _now()
    rows = (
        db.query(AgentTeam)
        .filter(
            AgentTeam.status.in_(("queued", "running", "verifying")),
            AgentTeam.deadline_at.is_not(None),
            AgentTeam.deadline_at <= now,
        )
        .order_by(AgentTeam.id.asc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    for team in rows:
        _expire_team(db, team, reason="deadline_reached")
    if rows:
        db.commit()
        for team in rows:
            _cleanup_team_runtime_resources(db, team, reason="deadline_reached")
    return len(rows)


def recover_expired_leases(db: Session, *, limit: int = 100) -> int:
    initial_now = _now()
    task_ids = [
        int(row[0])
        for row in (
            db.query(AgentTeamTask.id)
            .filter(
                AgentTeamTask.status == "running",
                AgentTeamTask.lease_expires_at.is_not(None),
                AgentTeamTask.lease_expires_at <= initial_now,
            )
            .order_by(AgentTeamTask.id.asc())
            .limit(max(1, min(int(limit), 500)))
            .all()
        )
    ]
    count = 0
    cleanup_retry_seconds = max(5, min(60, int(settings.agent_team_dispatch_interval_seconds) * 2))
    for task_id in task_ids:
        task = (
            db.query(AgentTeamTask)
            .filter(AgentTeamTask.id == task_id, AgentTeamTask.status == "running")
            .with_for_update()
            .first()
        )
        if task is None or task.lease_expires_at is None:
            db.rollback()
            continue
        lease_expiry = task.lease_expires_at
        if lease_expiry.tzinfo is None:
            lease_expiry = lease_expiry.replace(tzinfo=timezone.utc)
        if lease_expiry > _now():
            db.rollback()
            continue
        team = db.get(AgentTeam, task.team_id)
        if not team or team.status in _TERMINAL_TEAM:
            db.rollback()
            continue
        previous_lease_token = str(task.lease_token or "")
        cleanup_token = f"cleanup_{secrets.token_urlsafe(24)}"[:80]
        task.lease_token = cleanup_token
        task.lease_expires_at = _now() + timedelta(seconds=cleanup_retry_seconds)
        _event(
            db,
            team,
            "task.lease_cleanup_started",
            task=task,
            member=db.get(AgentTeamMember, task.member_id),
            from_status="running",
            to_status="running",
            detail={
                "reason": "lease_expired",
                "previous_lease_fingerprint": (
                    _lease_fingerprint(previous_lease_token) if previous_lease_token else ""
                ),
            },
        )
        # 先提交清理租约，使旧执行线程立即因 token 不匹配而停止；只有确认
        # 已停止全部关联沙箱后，任务才允许进入下一次 queued。
        db.commit()
        if not _cleanup_task_runtime_resources(db, team, task, reason="lease_expired"):
            db.expire_all()
            current = db.get(AgentTeamTask, task_id)
            current_team = db.get(AgentTeam, int(team.id))
            if (
                current is not None
                and current_team is not None
                and current.status == "running"
                and current.lease_token == cleanup_token
            ):
                current.lease_expires_at = _now() + timedelta(seconds=cleanup_retry_seconds)
                errors = _unjson(current.errors_json, [])
                errors = errors if isinstance(errors, list) else []
                errors.append(
                    {
                        "code": "runtime_resource_cleanup_failed",
                        "message": "租约恢复前未能确认旧沙箱已停止，将继续清理且禁止新尝试",
                    }
                )
                current.errors_json = _json(errors[-20:])
                _event(
                    db,
                    current_team,
                    "task.lease_cleanup_retry",
                    task=current,
                    member=db.get(AgentTeamMember, current.member_id),
                    from_status="running",
                    to_status="running",
                    detail={"retry_after_seconds": cleanup_retry_seconds},
                )
                db.commit()
            else:
                db.rollback()
            continue

        db.expire_all()
        task = (
            db.query(AgentTeamTask)
            .filter(AgentTeamTask.id == task_id, AgentTeamTask.team_id == team.id)
            .with_for_update()
            .first()
        )
        team = db.query(AgentTeam).filter(AgentTeam.id == team.id).with_for_update().first()
        if (
            task is None
            or team is None
            or team.status in _TERMINAL_TEAM
            or task.status != "running"
            or task.lease_token != cleanup_token
        ):
            db.rollback()
            continue
        now = _now()
        previous = task.status
        task.lease_token = None
        task.lease_expires_at = None
        member = db.get(AgentTeamMember, task.member_id)
        _mark_task_request_terminal(
            db,
            team,
            task,
            member,
            success=False,
            error="任务租约到期，执行结果不确定",
            now=now,
        )
        if int(task.attempt_count or 0) >= int(task.max_attempts or team.max_attempts or 1):
            task.status = "dead_letter"
            task.completed_at = now
            _event(
                db,
                team,
                "task.dead_letter",
                task=task,
                from_status=previous,
                to_status="dead_letter",
                detail={"error": "租约到期且重试预算耗尽"},
            )
        else:
            strategy_instruction = "先确认 Worker 健康和输入边界，再使用新会话重新执行本任务"
            execution_strategy = _apply_execution_strategy(
                task,
                member,
                instruction=strategy_instruction,
                error="任务租约到期，执行结果不确定",
                automatic=True,
                mode="fresh_worker_session_after_lease_expiry",
            )
            task.instructions = (
                f"{task.instructions}\n\n[小菱租约恢复改道第 {task.attempt_count + 1} 次] " f"{strategy_instruction}"
            )[:12000]
            task.status = "queued"
            task.completed_at = None
            task.next_attempt_at = now
            _event(
                db,
                team,
                "task.lease_retry_queued",
                task=task,
                from_status=previous,
                to_status="queued",
                detail={
                    "reason": "lease_expired",
                    "automatic": True,
                    "strategy_change": strategy_instruction,
                    "execution_strategy": execution_strategy,
                },
            )
        _promote_dependencies(db, team)
        _refresh_member_statuses(db, team)
        _refresh_team_status(db, team)
        count += 1
        db.commit()
    return count


def cancel_team(db: Session, user: User, team_id: int, *, reason: str = "用户取消") -> dict[str, Any]:
    team = _team_or_raise(db, user, team_id, lock=True)
    if team.status not in _TERMINAL_TEAM:
        old = team.status
        team.status = "cancelled"
        team.completed_at = team.completed_at or _now()
        now = _now()
        tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
        for task in tasks:
            if task.status in _TERMINAL_TASK:
                continue
            previous_task_status = task.status
            task.status = "cancelled"
            task.completed_at = now
            task.next_attempt_at = None
            task.lease_token = None
            task.lease_expires_at = None
            member = db.get(AgentTeamMember, task.member_id)
            _mark_task_request_terminal(
                db,
                team,
                task,
                member,
                success=False,
                error=reason,
                now=now,
            )
            _event(
                db,
                team,
                "task.cancelled",
                task=task,
                member=member,
                from_status=previous_task_status,
                to_status="cancelled",
                detail={"reason": reason},
            )
        _refresh_member_statuses(db, team, reclaim=True)
        _event(
            db,
            team,
            "team.cancelled",
            from_status=old,
            to_status="cancelled",
            actor_address=f"user:{user.id}",
            detail={"reason": reason},
        )
        db.commit()
        _cleanup_team_runtime_resources(db, team, reason="user_cancelled")
    return _team_out(db, team, include_events=True)


def retry_team(
    db: Session,
    user: User,
    team_id: int,
    *,
    task_keys: Optional[list[str]] = None,
    strategy_changes: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    team = _team_or_raise(db, user, team_id, lock=True)
    if team.status not in {"failed", "completed", "queued", "running"}:
        raise AgentTeamStateError("当前团队状态不允许重试")
    wanted = set(task_keys or [])
    query = db.query(AgentTeamTask).filter(
        AgentTeamTask.team_id == team.id, AgentTeamTask.status.in_(("failed", "dead_letter", "blocked"))
    )
    if wanted:
        query = query.filter(AgentTeamTask.task_key.in_(wanted))
    rows = query.all()
    if not rows:
        raise AgentTeamStateError("没有可重试的失败任务")
    # 只指定失败根节点时，自动带上由它阻断的后继节点；后继节点会在根节点
    # 成功后重新等待依赖，不要求小菱重复枚举整张工作图。
    if wanted:
        all_tasks = db.query(AgentTeamTask).filter(AgentTeamTask.team_id == team.id).all()
        selected_keys = {task.task_key for task in rows}
        changed = True
        while changed:
            changed = False
            for task in all_tasks:
                if task.task_key in selected_keys or task.status != "blocked":
                    continue
                dependencies = set(_unjson(task.dependency_keys_json, []))
                if dependencies & selected_keys:
                    rows.append(task)
                    selected_keys.add(task.task_key)
                    changed = True
    changes = {str(key): str(value).strip() for key, value in (strategy_changes or {}).items()}
    for task in rows:
        if task.status not in {"failed", "dead_letter"}:
            continue
        change = changes.get(task.task_key, "")
        if len(change) < 8 or change in task.instructions:
            raise AgentTeamValidationError(f"任务 {task.task_key} 重试前必须明确改变方案，不能原样重试")
    for task in rows:
        previous_status = task.status
        previous_hash = _task_strategy_hash(task)
        if task.status in {"failed", "dead_letter"}:
            member = db.get(AgentTeamMember, task.member_id)
            _apply_execution_strategy(
                task,
                member,
                instruction=changes[task.task_key],
                error=_json(_unjson(task.errors_json, [])),
                automatic=False,
                mode="user_directed_alternate_strategy",
            )
            task.instructions = f"{task.instructions.rstrip()}\n\n重试改道策略：{changes[task.task_key]}"
        task.status = "queued" if not _unjson(task.dependency_keys_json, []) else "waiting_dependency"
        # 保留历史尝试次数，确保每次领取生成新的请求消息 ID，且审计能还原完整重试链。
        task.next_attempt_at = _now() if task.status == "queued" else None
        task.completed_at = None
        task.result_json = "{}"
        task.artifacts_json = "[]"
        task.errors_json = "[]"
        new_hash = _task_strategy_hash(task)
        _event(
            db,
            team,
            "task.retry_requested",
            task=task,
            from_status=previous_status,
            to_status=task.status,
            actor_address=f"user:{user.id}",
            detail={
                "strategy_change": changes.get(task.task_key, "依赖恢复"),
                "previous_strategy_hash": previous_hash,
                "new_strategy_hash": new_hash,
            },
        )
    team.status = "queued"
    team.completed_at = None
    team.error_json = "{}"
    _event(
        db,
        team,
        "team.retry_requested",
        from_status="failed",
        to_status="queued",
        actor_address=f"user:{user.id}",
        detail={"task_keys": list(wanted), "strategy_changes": sorted(changes)},
    )
    _refresh_member_statuses(db, team)
    db.commit()
    return _team_out(db, team, include_events=True)


def archive_team(db: Session, user: User, team_id: int, *, reason: str = "归档") -> dict[str, Any]:
    team = _team_or_raise(db, user, team_id, lock=True)
    if team.status not in _TERMINAL_TEAM:
        raise AgentTeamStateError("仅终态团队可以归档")
    if team.archived_at is None:
        team.archived_at = _now()
        _event(db, team, "team.archived", actor_address=f"user:{user.id}", detail={"reason": reason})
        db.commit()
    return _team_out(db, team, include_events=True)
