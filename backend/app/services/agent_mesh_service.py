"""小菱 Agent Mesh 的发现、寻址、投递、回执与追踪。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import inspect, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.contracts import CONTRACTS, collaboration_allowed
from app.agents.registry import AgentRegistry
from app.core.config import settings
from app.models.agent_mesh import AgentMeshConversation, AgentMeshMessage, AgentMeshMessageEvent
from app.models.agent_response_run import AgentResponseRun
from app.models.custom_agent import CustomAgent
from app.models.user import User
from app.schemas.agent_mesh import AgentMeshAckIn, AgentMeshMessageIn

ONLINE_WINDOW = timedelta(seconds=90)
_SESSION_TERMINAL = {"completed", "failed", "expired", "dead_letter"}


class AgentMeshError(ValueError):
    """Agent Mesh 领域错误。"""


class AgentMeshAccessError(AgentMeshError):
    """消息所有权或协作边界错误。"""


class AgentMeshTargetError(AgentMeshError):
    """目标地址不存在或不可见。"""


class AgentMeshStateError(AgentMeshError):
    """消息状态流转不合法。"""


class AgentMeshSupervisionError(AgentMeshError):
    """监督式调度信封校验不合法。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> str:
    aware = _aware(value)
    return aware.isoformat() if aware is not None else ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _load(value: Optional[str], fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _is_admin(user: User) -> bool:
    return str(getattr(user, "role", "")) in {"admin", "super_admin"}


def _session_address(surface: str, session_key: str) -> str:
    return f"session:{surface}:{session_key}"


def _assert_surface(db: Session, user: User, surface: str) -> None:
    if surface not in {"user", "admin"}:
        raise AgentMeshAccessError("surface 必须是 user 或 admin")
    if surface == "admin" and not _is_admin_surface(db, user):
        raise AgentMeshAccessError("仅管理员账户可注册或访问 admin 会话")


def _is_admin_surface(db: Session, user: User) -> bool:
    """admin 会话准入与 /agent-responses/stream 口径一致:
    旧版 role 字段之外,RBAC 绑定了 admin/super_admin 角色的账户同样放行,
    避免 AdminCopilot 能聊天但 Mesh 心跳/收件箱/归档 403 的割裂。
    """
    if _is_admin(user):
        return True
    try:
        from app.services import rbac_service

        return rbac_service.is_admin_user(db, int(user.id))
    except Exception:  # noqa: BLE001 - RBAC 查询失败时退回 role 字段判定
        return False


def _conversation(db: Session, user_id: int, surface: str, session_key: str) -> Optional[AgentMeshConversation]:
    return (
        db.query(AgentMeshConversation)
        .filter(
            AgentMeshConversation.user_id == user_id,
            AgentMeshConversation.surface == surface,
            AgentMeshConversation.session_key == session_key,
            AgentMeshConversation.status == "active",
        )
        .first()
    )


def heartbeat(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    title: str,
    active_run_id: str = "",
    active_run_status: str = "",
) -> dict[str, Any]:
    """注册或刷新当前账户的小菱会话。"""
    _assert_surface(db, user, surface)
    row = _conversation(db, int(user.id), surface, session_key)
    now = _now()
    if row is None:
        # 已归档会话不因在途 heartbeat 复活:归档是跨设备可见的用户决定,
        # 新会话始终使用新的 UUID,也不会触发唯一键冲突。
        archived = (
            db.query(AgentMeshConversation)
            .filter(
                AgentMeshConversation.user_id == int(user.id),
                AgentMeshConversation.surface == surface,
                AgentMeshConversation.session_key == session_key,
            )
            .first()
        )
        if archived is not None:
            return _conversation_out(archived, now=now)
        row = AgentMeshConversation(
            user_id=int(user.id),
            surface=surface,
            session_key=session_key,
            title=title.strip() or "新对话",
            status="active",
            last_seen_at=now,
        )
        db.add(row)
    row.title = title.strip() or row.title or "新对话"
    row.active_run_id = active_run_id or None
    row.active_run_status = active_run_status or None
    row.last_seen_at = now
    try:
        db.commit()
    except IntegrityError:
        # 同一账户并发首心跳可能同时新建同一会话;让已提交的一方获胜并返回其状态。
        db.rollback()
        existing = (
            db.query(AgentMeshConversation)
            .filter(
                AgentMeshConversation.user_id == int(user.id),
                AgentMeshConversation.surface == surface,
                AgentMeshConversation.session_key == session_key,
            )
            .first()
        )
        if existing is None:
            raise
        row = existing
    db.refresh(row)
    return _conversation_out(row, now=now)


def _authoritative_run_state(
    db: Session,
    *,
    user_id: int,
    surface: str,
    session_key: str,
) -> tuple[str, str]:
    """以 agent_response_run 数据库账本为会话忙碌状态的唯一事实源。

    心跳字段只反映该会话最后一次在前台时的状态,后台完成后可能长期陈旧;
    这里读取最新运行记录,避免陈旧快照把已完成会话误判为运行中/等待输入。
    """
    row = (
        db.query(AgentResponseRun)
        .filter(
            AgentResponseRun.user_id == int(user_id),
            AgentResponseRun.surface == surface,
            AgentResponseRun.session_key == session_key,
        )
        .order_by(AgentResponseRun.id.desc())
        .first()
    )
    if row is None:
        return "", ""
    return str(row.run_id or ""), str(row.status or "")


_OCCUPIED_RUN_STATUSES = {
    "running", "approving", "rejecting", "answering", "waiting_approval", "waiting_input",
}


def archive_conversation(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
) -> dict[str, Any]:
    """把当前账户的一个会话归档,并从发现目录隐藏。

    幂等:会话不存在或已归档时返回归档结果;运行中/等待审批/等待输入的会话
    拒绝归档,避免用户在另一设备上误删正在执行的任务。
    """
    _assert_surface(db, user, surface)
    _, run_status = _authoritative_run_state(
        db,
        user_id=int(user.id),
        surface=surface,
        session_key=session_key,
    )
    if run_status in _OCCUPIED_RUN_STATUSES:
        raise AgentMeshStateError("会话正在运行或等待操作,暂不能归档")
    row = (
        db.query(AgentMeshConversation)
        .filter(
            AgentMeshConversation.user_id == int(user.id),
            AgentMeshConversation.surface == surface,
            AgentMeshConversation.session_key == session_key,
        )
        .first()
    )
    if row is None or row.status == "archived":
        return {"session_id": session_key, "status": "archived"}
    row.status = "archived"
    row.active_run_id = None
    row.active_run_status = None
    db.commit()
    return {"session_id": session_key, "status": "archived"}


def archive_empty_conversations(db: Session) -> dict[str, Any]:
    """把长期无任何消息、无 Responses 运行的活跃空会话归档。"""
    hours = int(settings.agent_mesh_empty_session_archive_hours)
    cutoff = _now() - timedelta(hours=hours)
    archived = 0
    conversations = (
        db.query(AgentMeshConversation)
        .filter(AgentMeshConversation.status == "active")
        .all()
    )
    for row in conversations:
        last_seen = _aware(row.last_seen_at)
        if last_seen is None or last_seen >= cutoff:
            continue
        address = _session_address(row.surface, row.session_key)
        has_message = (
            db.query(AgentMeshMessage.id)
            .filter(
                or_(
                    AgentMeshMessage.send_to == address,
                    AgentMeshMessage.sent_from == address,
                )
            )
            .first()
        )
        if has_message is not None:
            continue
        has_run = (
            db.query(AgentResponseRun.id)
            .filter(
                AgentResponseRun.user_id == int(row.user_id),
                AgentResponseRun.surface == row.surface,
                AgentResponseRun.session_key == row.session_key,
            )
            .first()
        )
        if has_run is not None:
            continue
        row.status = "archived"
        archived += 1
    if archived:
        db.commit()
    return {"archived": archived}


def _conversation_out(row: AgentMeshConversation, *, now: Optional[datetime] = None) -> dict[str, Any]:
    current = now or _now()
    last_seen = _aware(row.last_seen_at)
    online = bool(last_seen and current - last_seen <= ONLINE_WINDOW)
    return {
        "address": _session_address(row.surface, row.session_key),
        "name": row.title,
        "kind": "session",
        "status": "online" if online else "offline",
        "capabilities": ["receive_message", "resume_context", "acknowledge"],
        "session_id": row.session_key,
        "surface": row.surface,
        "active_run_id": row.active_run_id or "",
        "active_run_status": row.active_run_status or "",
        "last_seen_at": _iso(row.last_seen_at),
    }


def _runtime_codes() -> set[str]:
    return {str(item.get("code") or "") for item in AgentRegistry.instance().list_runtime()}


def _custom_agents(db: Session, user: User) -> list[dict[str, Any]]:
    from app.services.agent_mesh_dispatcher import dispatch_state

    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(CustomAgent.__tablename__):
        return []
    from app.core.permission_codes import PermissionCode
    from app.services import rbac_service

    if not rbac_service.check_permission(db, int(user.id), PermissionCode.CUSTOM_AGENT_INVOKE):
        return []
    rows = (
        db.query(CustomAgent)
        .filter(CustomAgent.is_enabled == 1, CustomAgent.status == "published")
        .order_by(CustomAgent.code.asc())
        .all()
    )
    return [
        {
            "address": f"custom:{row.code}",
            "name": row.name,
            "kind": "custom",
            "status": "available",
            "capabilities": ["invoke_published_agent"],
            "session_id": "",
            "surface": "",
            "last_seen_at": "",
            "description": row.description or "",
            "dispatch_state": dispatch_state(f"custom:{row.code}"),
        }
        for row in rows
    ]


_TEAM_GOVERNED_CODES = frozenset({"sandbox_deployer", "test_verifier", "operations"})


def list_agents(db: Session, user: User) -> dict[str, Any]:
    """列出全部内置契约、可调用自定义 Agent 和同账户会话。"""
    from app.services.agent_mesh_dispatcher import dispatch_state

    runtime_codes = _runtime_codes()
    items: list[dict[str, Any]] = []
    for code, contract in CONTRACTS.items():
        kind = "runtime" if code in runtime_codes or contract.execution_mode in {
            "runtime", "runtime_service", "protected_runtime",
        } else "service"
        item = {
            "address": f"agent:{code}",
            "name": contract.name,
            "kind": kind,
            "status": "registered" if code in runtime_codes else "available",
            "capabilities": [skill.code for skill in contract.skills],
            "session_id": "",
            "surface": "",
            "last_seen_at": "",
            "description": contract.mission,
            "dispatch_state": dispatch_state(f"agent:{code}"),
        }
        # 受治理运行时在团队内由受信任调度接管审批:即使直接派发需审批,
        # 也可作为 create_agent_team 成员,避免模型被 approval_required 误导。
        if code in _TEAM_GOVERNED_CODES and item["dispatch_state"] == "approval_required":
            item["team_dispatch_state"] = "team_governed"
        if code == "operations":
            is_super_admin = (
                str(getattr(user, "username", "")) == "admin"
                and str(getattr(user, "role", "")) == "super_admin"
            )
            if is_super_admin:
                from app.services import rbac_service

                is_super_admin = rbac_service.is_super_admin_user(db, int(user.id))
            if is_super_admin:
                from app.services.ops_service import READ_ONLY_ACTIONS

                item["team_dispatch_state"] = "read_only"
                item["team_input_contract"] = {
                    "action": sorted(READ_ONLY_ACTIONS),
                    "params": "object",
                    "write_actions": "main_xiaoling_approval_only",
                }
        items.append(item)
    items.extend(_custom_agents(db, user))
    conversations = (
        db.query(AgentMeshConversation)
        .filter(
            AgentMeshConversation.user_id == int(user.id),
            AgentMeshConversation.status == "active",
        )
        .order_by(AgentMeshConversation.last_seen_at.desc(), AgentMeshConversation.id.desc())
        .all()
    )
    now = _now()
    for row in conversations:
        item = _conversation_out(row, now=now)
        # 覆盖心跳快照,用数据库运行账本给出权威 busy 状态。
        run_id, run_status = _authoritative_run_state(
            db,
            user_id=int(user.id),
            surface=row.surface,
            session_key=row.session_key,
        )
        item["active_run_id"] = run_id
        item["active_run_status"] = run_status
        items.append(item)
    by_kind: dict[str, int] = {}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {"items": items, "total": len(items), "by_kind": by_kind}


def _validate_target(db: Session, user: User, address: str) -> str:
    if address.startswith("agent:"):
        code = address.split(":", 1)[1]
        if code not in CONTRACTS:
            raise AgentMeshTargetError("目标 Agent 不存在或未登记")
        return "agent"
    if address.startswith("custom:"):
        code = address.split(":", 1)[1]
        if not any(item["address"] == address for item in _custom_agents(db, user)):
            raise AgentMeshTargetError(f"已发布 Agent {code} 不存在或不可调用")
        return "custom"
    parts = address.split(":", 2)
    if len(parts) == 3 and parts[0] == "session" and parts[1] in {"user", "admin"}:
        if _conversation(db, int(user.id), parts[1], parts[2]) is None:
            raise AgentMeshTargetError("目标会话不存在、已归档或不属于当前账户")
        return "session"
    raise AgentMeshTargetError("目标地址格式非法")


_SUPERVISION_CONTEXT_KEYS = (
    "supervision_objective",
    "supervision_round",
    "supervision_max_rounds",
    "supervision_correlation_id",
)


def _supervision_limit() -> int:
    """读取监督轮次上限；字段尚未接入时回退契约默认值 3。"""
    try:
        return max(1, int(getattr(settings, "agent_mesh_supervision_max_rounds", 3) or 3))
    except (TypeError, ValueError):
        return 3


def _effective_supervision_max_rounds(context: Mapping[str, Any]) -> int:
    """统一把 context 的 max_rounds 钳制到服务端上限，禁止用大数突破。"""
    limit = _supervision_limit()
    requested = context.get("supervision_max_rounds")
    if requested is None:
        return limit
    try:
        requested_int = int(requested)
    except (TypeError, ValueError):
        return limit
    return min(requested_int, limit)


def _validate_supervision_envelope(message: AgentMeshMessageIn) -> None:
    """校验 agent:/custom: task.request 的监督信封。"""
    context = message.context.model_dump()
    round_value = context.get("supervision_round")
    if round_value is None:
        return
    if isinstance(round_value, bool) or not isinstance(round_value, int):
        raise AgentMeshSupervisionError("supervision_round 必须是整数")
    max_rounds = _effective_supervision_max_rounds(context)
    if round_value < 1 or round_value > max_rounds:
        raise AgentMeshSupervisionError(
            f"supervision_round 必须在 1..{max_rounds} 之间，当前为 {round_value}"
        )
    if round_value > 1:
        correlation_id = context.get("supervision_correlation_id")
        if not isinstance(correlation_id, str) or len(correlation_id) == 0:
            raise AgentMeshSupervisionError(
                "supervision_round 大于 1 时必须提供非空 supervision_correlation_id"
            )


def _context_for_storage(message: AgentMeshMessageIn) -> dict[str, Any]:
    """持久化 context 时省略值为 None 的监督字段，保持无监督消息行为不变。"""
    context = message.context.model_dump()
    return {
        key: value
        for key, value in context.items()
        if key not in _SUPERVISION_CONTEXT_KEYS or value is not None
    }


def _supervision_result_metadata(row: AgentMeshMessage) -> Optional[dict[str, Any]]:
    """从原监督请求生成 task.result 的 supervision 元数据。"""
    context = _load(row.context_json, {})
    if not isinstance(context, dict):
        return None
    if not any(key in context for key in _SUPERVISION_CONTEXT_KEYS):
        return None
    return {
        "supervision_objective": context.get("supervision_objective"),
        "supervision_round": context.get("supervision_round"),
        "supervision_max_rounds": _effective_supervision_max_rounds(context),
        "supervision_correlation_id": row.message_id,
    }


def _event(
    db: Session,
    row: AgentMeshMessage,
    status: str,
    actor: str,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    db.add(AgentMeshMessageEvent(
        message_id=row.message_id,
        user_id=row.user_id,
        trace_id=row.trace_id,
        status=status,
        actor_address=actor,
        detail_json=_json(dict(detail or {})),
    ))


def _message_out(row: AgentMeshMessage, *, include_body: bool = True) -> dict[str, Any]:
    data = {
        "schema_version": row.schema_version,
        "message_id": row.message_id,
        "idempotency_key": row.idempotency_key,
        "trace_id": row.trace_id,
        "correlation_id": row.correlation_id,
        "causation_id": row.causation_id,
        "sent_from": row.sent_from,
        "send_to": row.send_to,
        "message_type": row.message_type,
        "priority": row.priority,
        "subject": row.subject,
        "status": row.status,
        "requires_ack": bool(row.requires_ack),
        "max_attempts": row.max_attempts,
        "attempt_count": row.attempt_count,
        "last_error": row.last_error or "",
        "expires_at": _iso(row.expires_at),
        "create_time": _iso(row.create_time),
        "update_time": _iso(row.update_time),
    }
    if include_body:
        data.update({
            "payload": _load(row.payload_json, {}),
            "context": _load(row.context_json, {}),
            "artifacts": _load(row.artifacts_json, []),
            "errors": _load(row.errors_json, []),
        })
    return data


def send_message(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    message: AgentMeshMessageIn,
    trusted_source: bool = False,
) -> dict[str, Any]:
    """校验并持久化标准消息；相同幂等键返回原记录。"""
    _assert_surface(db, user, surface)
    if _conversation(db, int(user.id), surface, session_key) is None:
        raise AgentMeshAccessError("发送会话尚未注册或不属于当前账户")
    derived_source = _session_address(surface, session_key)
    source = message.sent_from or derived_source
    if not trusted_source and source != derived_source:
        raise AgentMeshAccessError("sent_from 与当前认证会话不一致")
    target_kind = _validate_target(db, user, message.send_to)
    if target_kind in {"agent", "custom"} and message.message_type == "task.request":
        _validate_supervision_envelope(message)
    if trusted_source and source.startswith("agent:") and message.send_to.startswith("agent:"):
        source_code = source.split(":", 1)[1]
        target_code = message.send_to.split(":", 1)[1]
        if not collaboration_allowed(source_code, target_code):
            raise AgentMeshAccessError(f"Agent 协作越界: {source_code} -> {target_code}")

    existing = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.idempotency_key == message.idempotency_key,
        )
        .first()
    )
    if existing is not None:
        if existing.sent_from != source or existing.send_to != message.send_to:
            raise AgentMeshStateError("幂等键已用于不同的发送方或目标")
        return _message_out(existing)

    now = _now()
    row = AgentMeshMessage(
        message_id=f"msg_{uuid.uuid4().hex}",
        user_id=int(user.id),
        schema_version=message.schema_version,
        idempotency_key=message.idempotency_key,
        trace_id=message.trace_id or f"trc_{uuid.uuid4().hex}",
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        sent_from=source,
        send_to=message.send_to,
        message_type=message.message_type,
        priority=message.priority,
        subject=message.subject,
        payload_json=_json(message.payload),
        context_json=_json(_context_for_storage(message)),
        artifacts_json=_json(message.artifacts),
        errors_json=_json(message.errors),
        status="queued",
        requires_ack=1 if message.delivery.requires_ack else 0,
        max_attempts=message.delivery.max_attempts,
        attempt_count=0,
        expires_at=message.delivery.expires_at,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(AgentMeshMessage)
            .filter(
                AgentMeshMessage.user_id == int(user.id),
                AgentMeshMessage.idempotency_key == message.idempotency_key,
            )
            .first()
        )
        if existing is None:
            raise
        return _message_out(existing)
    _event(db, row, "queued", source)
    # Agent/自定义 Agent 必须由服务端消费者真正认领后才算 delivered。
    # 旧实现发送即 delivered，会在没有任何执行器时制造“已投递”的假象。
    # 非 task.request 消息只是审计通知，不应进入执行队列；记录完成表示
    # 已被 Mesh 账本接收，避免 status.update/notification 永久占用活动队列。
    if target_kind in {"agent", "custom"} and message.message_type != "task.request":
        row.status = "completed"
        row.completed_at = now
        _event(db, row, "completed", "mesh:ledger", {"summary": "非任务消息已记录，不触发 Agent 执行"})
    source_parts = derived_source.split(":", 2)
    source_conversation = _conversation(db, int(user.id), source_parts[1], source_parts[2])
    if source_conversation is not None:
        source_conversation.last_message_at = now
    if target_kind == "session":
        target_parts = message.send_to.split(":", 2)
        target_conversation = _conversation(db, int(user.id), target_parts[1], target_parts[2])
        if target_conversation is not None:
            target_conversation.last_message_at = now
    db.commit()
    db.refresh(row)
    return _message_out(row)


def claim_dispatch_message(
    db: Session,
    user: User,
    message_id: str,
    *,
    target_address: str,
    lease_seconds: int = 300,
) -> Optional[dict[str, Any]]:
    """使用数据库 CAS 和租约认领一个 Agent task.request。"""
    if not target_address.startswith(("agent:", "custom:")):
        raise AgentMeshTargetError("消费者只能认领 Agent 地址")
    _validate_target(db, user, target_address)
    now = _now()
    lease_token = f"lease_{uuid.uuid4().hex}"
    lease_expires_at = now + timedelta(seconds=max(30, min(int(lease_seconds), 1800)))
    for expected in ("delivered", "queued"):
        values: dict[str, Any] = {
            "status": "processing",
            "acknowledged_at": now,
            "processing_at": now,
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at,
            "next_attempt_at": None,
        }
        values.update({
            "delivered_at": now,
            "attempt_count": AgentMeshMessage.attempt_count + 1,
        })
        result = db.execute(
            update(AgentMeshMessage)
            .where(
                AgentMeshMessage.message_id == message_id,
                AgentMeshMessage.user_id == int(user.id),
                AgentMeshMessage.send_to == target_address,
                AgentMeshMessage.message_type == "task.request",
                AgentMeshMessage.status == expected,
                or_(AgentMeshMessage.expires_at.is_(None), AgentMeshMessage.expires_at > now),
                or_(AgentMeshMessage.next_attempt_at.is_(None), AgentMeshMessage.next_attempt_at <= now),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            continue
        row = (
            db.query(AgentMeshMessage)
            .filter(AgentMeshMessage.message_id == message_id)
            .populate_existing()
            .one()
        )
        _event(db, row, "delivered", target_address, {"delivery_kind": "dispatch"})
        _event(db, row, "acknowledged", target_address)
        _event(db, row, "processing", target_address, {"lease_expires_at": _iso(lease_expires_at)})
        db.commit()
        db.refresh(row)
        claimed = _message_out(row)
        claimed["lease_token"] = lease_token
        return claimed
    db.rollback()
    return None


def _source_session(row: AgentMeshMessage) -> tuple[str, str]:
    parts = row.sent_from.split(":", 2)
    if len(parts) != 3 or parts[0] != "session" or parts[1] not in {"user", "admin"}:
        raise AgentMeshStateError("Agent 结果缺少可回传的来源会话")
    return parts[1], parts[2]


def _send_dispatch_reply(
    db: Session,
    user: User,
    row: AgentMeshMessage,
    *,
    target_address: str,
    target_name: str,
    message_type: str,
    payload: Mapping[str, Any],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    surface, session_key = _source_session(row)
    reply_context = _load(row.context_json, {})
    if message_type == "task.result":
        metadata = _supervision_result_metadata(row)
        if metadata:
            reply_context = {**reply_context, **metadata}
    return send_message(
        db,
        user,
        surface=surface,
        session_key=session_key,
        trusted_source=True,
        message=AgentMeshMessageIn.model_validate({
            "idempotency_key": f"agent-result:{row.message_id}",
            "trace_id": row.trace_id,
            "correlation_id": row.message_id,
            "causation_id": row.message_id,
            "sent_from": target_address,
            "send_to": row.sent_from,
            "message_type": message_type,
            "priority": row.priority,
            "subject": f"{target_name}回复：{row.subject}"[:240],
            "payload": dict(payload),
            "context": reply_context,
            "artifacts": _load(row.artifacts_json, []),
            "errors": errors,
            "delivery": {"requires_ack": True, "max_attempts": row.max_attempts},
        }),
    )


def _dead_letter_undeliverable_reply(
    db: Session,
    row: AgentMeshMessage,
    *,
    target_address: str,
    error: AgentMeshError,
) -> dict[str, Any]:
    """来源会话失效时终止任务，避免无法回传的消息永久重试。"""
    now = _now()
    row.status = "dead_letter"
    row.completed_at = now
    row.last_error = f"Agent 结果无法回传：{error}"
    row.lease_token = None
    row.lease_expires_at = None
    row.next_attempt_at = None
    _event(db, row, "failed", target_address, {"error": row.last_error})
    _event(db, row, "dead_letter", "system", {
        "error": row.last_error,
        "reason": "reply target unavailable",
    })
    db.commit()
    db.refresh(row)
    return _message_out(row)


def complete_dispatch_message(
    db: Session,
    user: User,
    message_id: str,
    *,
    target_address: str,
    target_name: str,
    lease_token: str,
    success: bool,
    summary: Mapping[str, Any],
    error: str = "",
) -> dict[str, Any]:
    """仅允许当前租约持有者完成消息，并向来源会话回传唯一结果。"""
    row = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.message_id == message_id,
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.send_to == target_address,
            AgentMeshMessage.lease_token == lease_token,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        raise AgentMeshStateError("Agent 消息租约已失效，拒绝旧 Worker 回写")
    if row.status in _SESSION_TERMINAL:
        return _message_out(row)
    if row.status != "processing":
        raise AgentMeshStateError(f"Agent 消息当前状态 {row.status} 不能完成")

    now = _now()
    if not success:
        row.last_error = error or str(summary.get("summary") or "Agent 消息处理失败")
        _event(db, row, "failed", target_address, {"error": row.last_error})
        if int(row.attempt_count or 0) < int(row.max_attempts or 1):
            row.status = "queued"
            row.next_attempt_at = now + timedelta(seconds=min(60, 2 ** int(row.attempt_count or 1)))
            row.lease_token = None
            row.lease_expires_at = None
            _event(db, row, "queued", "system", {
                "reason": "agent retry scheduled",
                "next_attempt_at": _iso(row.next_attempt_at),
            })
        else:
            try:
                reply = _send_dispatch_reply(
                    db,
                    user,
                    row,
                    target_address=target_address,
                    target_name=target_name,
                    message_type="task.error",
                    payload={
                        "status": "failed",
                        "summary": row.last_error,
                        "in_reply_to": row.message_id,
                    },
                    errors=[{"code": "agent_dispatch_failed", "message": row.last_error}],
                )
            except AgentMeshError as reply_error:
                return _dead_letter_undeliverable_reply(
                    db,
                    row,
                    target_address=target_address,
                    error=reply_error,
                )
            row.status = "dead_letter"
            row.completed_at = now
            row.lease_token = None
            row.lease_expires_at = None
            _event(db, row, "dead_letter", "system", {
                "error": row.last_error,
                "reply_message_id": reply["message_id"],
            })
        db.commit()
        db.refresh(row)
        return _message_out(row)

    result_payload = dict(summary)
    result_payload.setdefault("status", "completed")
    result_payload.setdefault("agent_code", target_address.split(":", 1)[1])
    result_payload.setdefault("in_reply_to", row.message_id)
    try:
        reply = _send_dispatch_reply(
            db,
            user,
            row,
            target_address=target_address,
            target_name=target_name,
            message_type="task.result",
            payload=result_payload,
            errors=[],
        )
    except AgentMeshError as reply_error:
        return _dead_letter_undeliverable_reply(
            db,
            row,
            target_address=target_address,
            error=reply_error,
        )
    row.status = "completed"
    row.completed_at = now
    row.lease_token = None
    row.lease_expires_at = None
    _event(db, row, "completed", target_address, {"reply_message_id": reply["message_id"]})
    db.commit()
    db.refresh(row)
    result = _message_out(row)
    result["reply_message"] = reply
    return result


def expire_unclaimed_dispatch_messages(db: Session, *, limit: int = 100) -> int:
    """使到期且尚未执行的 Agent 消息进入 expired。"""
    now = _now()
    rows = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.message_type == "task.request",
            AgentMeshMessage.send_to.like("agent:%") | AgentMeshMessage.send_to.like("custom:%"),
            AgentMeshMessage.status.in_(("queued", "delivered")),
            AgentMeshMessage.expires_at.is_not(None),
            AgentMeshMessage.expires_at <= now,
        )
        .order_by(AgentMeshMessage.id.asc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    _expire_pending(db, rows, now)
    db.commit()
    return len(rows)


def recover_stale_dispatch_messages(db: Session, *, limit: int = 100) -> int:
    """回收租约已到期的 processing 消息，防止 Worker 崩溃后永久卡住。"""
    now = _now()
    rows = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.message_type == "task.request",
            or_(AgentMeshMessage.send_to.like("agent:%"), AgentMeshMessage.send_to.like("custom:%")),
            AgentMeshMessage.status == "processing",
            AgentMeshMessage.lease_expires_at.is_not(None),
            AgentMeshMessage.lease_expires_at <= now,
        )
        .order_by(AgentMeshMessage.id.asc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    recovered = 0
    for row in rows:
        if int(row.attempt_count or 0) >= int(row.max_attempts or 1):
            user = db.get(User, int(row.user_id))
            if user is not None and row.lease_token:
                code = row.send_to.split(":", 1)[1]
                target_name = CONTRACTS[code].name if row.send_to.startswith("agent:") else code
                if row.send_to.startswith("custom:"):
                    asset = db.query(CustomAgent).filter(CustomAgent.code == code).first()
                    target_name = asset.name if asset is not None else code
                try:
                    complete_dispatch_message(
                        db,
                        user,
                        row.message_id,
                        target_address=row.send_to,
                        target_name=target_name,
                        lease_token=row.lease_token,
                        success=False,
                        summary={"status": "failed", "summary": "Agent Worker 租约到期且重试预算耗尽"},
                        error="Agent Worker 租约到期且重试预算耗尽",
                    )
                    recovered += 1
                    continue
                except AgentMeshError:
                    db.rollback()
        result = db.execute(
            update(AgentMeshMessage)
            .where(
                AgentMeshMessage.id == row.id,
                AgentMeshMessage.status == "processing",
                AgentMeshMessage.lease_token == row.lease_token,
                AgentMeshMessage.lease_expires_at <= now,
            )
            .values(
                status="queued",
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=now,
                last_error="Agent Worker 租约到期，任务已回收",
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            continue
        db.refresh(row)
        _event(db, row, "failed", "system", {"error": "dispatch lease expired"})
        _event(db, row, "queued", "system", {"reason": "stale lease recovered"})
        recovered += 1
    db.commit()
    return recovered


def _expire_pending(db: Session, rows: list[AgentMeshMessage], now: datetime) -> None:
    for row in rows:
        expires_at = _aware(row.expires_at)
        if expires_at is None or expires_at > now or row.status in _SESSION_TERMINAL:
            continue
        row.status = "expired"
        row.completed_at = now
        _event(db, row, "expired", "system", {"reason": "delivery deadline exceeded"})


def pull_inbox(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """拉取当前会话的待处理消息，并原子标记首次送达。"""
    _assert_surface(db, user, surface)
    if _conversation(db, int(user.id), surface, session_key) is None:
        raise AgentMeshAccessError("目标会话尚未注册或不属于当前账户")
    target = _session_address(surface, session_key)
    rows = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.send_to == target,
            AgentMeshMessage.status.in_(("queued", "delivered")),
        )
        .order_by(AgentMeshMessage.id.asc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    now = _now()
    _expire_pending(db, rows, now)
    delivered: list[AgentMeshMessage] = []
    for row in rows:
        if row.status == "expired":
            continue
        if row.status == "queued":
            row.status = "delivered"
            row.delivered_at = now
            row.attempt_count = int(row.attempt_count or 0) + 1
            _event(db, row, "delivered", target)
        delivered.append(row)
    db.commit()
    return [_message_out(row) for row in delivered]


def _owned_target_message(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
) -> AgentMeshMessage:
    _assert_surface(db, user, surface)
    target = _session_address(surface, session_key)
    row = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.message_id == message_id,
            AgentMeshMessage.user_id == int(user.id),
            AgentMeshMessage.send_to == target,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        raise AgentMeshTargetError("消息不存在或不属于当前会话")
    return row


def ack_message(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
    acknowledgement: AgentMeshAckIn,
) -> dict[str, Any]:
    """推进消息状态；失败在预算内重新排队，超限进入死信。"""
    row = _owned_target_message(db, user, message_id, surface=surface, session_key=session_key)
    now = _now()
    requested = acknowledgement.status
    allowed = {
        "queued": {"acknowledged", "processing"},
        "delivered": {"acknowledged", "processing", "completed", "failed"},
        "acknowledged": {"processing", "completed", "failed"},
        "processing": {"completed", "failed"},
    }
    if requested == row.status:
        return _message_out(row)
    if requested not in allowed.get(row.status, set()):
        raise AgentMeshStateError(f"消息不能从 {row.status} 转为 {requested}")

    actor = _session_address(surface, session_key)
    if requested == "acknowledged":
        row.status = requested
        row.acknowledged_at = now
        _event(db, row, requested, actor, {"summary": acknowledgement.summary})
    elif requested == "processing":
        row.status = requested
        row.acknowledged_at = row.acknowledged_at or now
        row.processing_at = now
        _event(db, row, requested, actor, {"summary": acknowledgement.summary})
    elif requested == "completed":
        row.status = requested
        row.completed_at = now
        _event(db, row, requested, actor, {"summary": acknowledgement.summary})
    else:
        row.last_error = acknowledgement.error or acknowledgement.summary or "消息处理失败"
        _event(db, row, "failed", actor, {"error": row.last_error})
        if int(row.attempt_count or 0) < int(row.max_attempts or 1):
            row.status = "queued"
            _event(db, row, "queued", "system", {"reason": "retry scheduled"})
        else:
            row.status = "dead_letter"
            row.completed_at = now
            _event(db, row, "dead_letter", "system", {"error": row.last_error})
    db.commit()
    db.refresh(row)
    return _message_out(row)


def get_trace(db: Session, user: User, trace_id: str) -> dict[str, Any]:
    """按账户恢复一条 trace 的完整消息与状态时间线。"""
    rows = (
        db.query(AgentMeshMessage)
        .filter(AgentMeshMessage.user_id == int(user.id), AgentMeshMessage.trace_id == trace_id)
        .order_by(AgentMeshMessage.id.asc())
        .all()
    )
    if not rows:
        raise AgentMeshTargetError("追踪链不存在")
    message_ids = [row.message_id for row in rows]
    events = (
        db.query(AgentMeshMessageEvent)
        .filter(
            AgentMeshMessageEvent.user_id == int(user.id),
            AgentMeshMessageEvent.message_id.in_(message_ids),
        )
        .order_by(AgentMeshMessageEvent.id.asc())
        .all()
    )
    by_message: dict[str, list[dict[str, Any]]] = {message_id: [] for message_id in message_ids}
    for event in events:
        by_message[event.message_id].append({
            "status": event.status,
            "actor_address": event.actor_address,
            "detail": _load(event.detail_json, {}),
            "create_time": _iso(event.create_time),
        })
    output = []
    for row in rows:
        item = _message_out(row)
        item["events"] = by_message[row.message_id]
        output.append(item)
    return {"trace_id": trace_id, "messages": output, "total": len(output)}


def list_session_messages(
    db: Session,
    user: User,
    *,
    surface: str,
    session_key: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """返回当前账户会话收发的最近消息，供折叠时间线恢复。"""
    _assert_surface(db, user, surface)
    bind = db.get_bind()
    if bind is None or not inspect(bind).has_table(AgentMeshMessage.__tablename__):
        return []
    address = _session_address(surface, session_key)
    rows = (
        db.query(AgentMeshMessage)
        .filter(
            AgentMeshMessage.user_id == int(user.id),
            (AgentMeshMessage.sent_from == address) | (AgentMeshMessage.send_to == address),
        )
        .order_by(AgentMeshMessage.id.desc())
        .limit(max(1, min(int(limit), 100)))
        .all()
    )
    return [_message_out(row) for row in reversed(rows)]


def _supervision_review_protocol(envelope: Mapping[str, Any]) -> str:
    """为带监督元数据的 task.result 生成确定性复核指令。"""
    if envelope.get("message_type") != "task.result":
        return ""
    context = envelope.get("context")
    if not isinstance(context, dict):
        return ""
    if not any(key in context for key in _SUPERVISION_CONTEXT_KEYS):
        return ""
    objective = context.get("supervision_objective")
    objective_label = objective if isinstance(objective, str) else str(objective or "")
    current_round = context.get("supervision_round")
    valid_round = isinstance(current_round, int) and not isinstance(current_round, bool)
    round_label = str(current_round) if valid_round else "未知"
    next_round_label = str(current_round + 1) if valid_round else "round+1"
    max_rounds = _effective_supervision_max_rounds(context)
    sent_from = str(envelope.get("sent_from") or "")
    original_message_id = str(envelope.get("message_id") or "")
    lines = [
        "【监督式复核协议】当前收到带监督元数据的 task.result，必须先完成确定性复核：",
        f"1. 对照监督目标 supervision_objective={objective_label!r}，判断该结果是否满足目标。",
        (
            f"2. 若结果满足目标，或当前 supervision_round={round_label} "
            f"已达到 supervision_max_rounds={max_rounds}，"
            "则直接向用户汇报复核结论，不得再派发。"
        ),
        (
            f"3. 若结果不满足且 supervision_round={round_label} "
            f"小于 supervision_max_rounds={max_rounds}，"
            f"则调用 send_message 向本消息的 sent_from 地址 {sent_from!r} 回发 task.request；"
            f"payload 必须带具体纠正点；context 必须写入 supervision_objective={objective_label!r}、"
            f"supervision_round={next_round_label}、supervision_max_rounds={max_rounds}、"
            f"supervision_correlation_id={original_message_id!r}；"
            "发送后向用户说明已纠正并等待。"
        ),
        "4. 无论满足与否，都不得伪装成用户发言，不得把该结构化消息冒充用户输入。",
    ]
    return "\n".join(lines)


def prepare_message_run(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
) -> tuple[AgentMeshMessage, dict[str, Any]]:
    """为自动续跑认领消息，并返回不会伪装成用户文本的 system input。"""
    row = _owned_target_message(db, user, message_id, surface=surface, session_key=session_key)
    if row.status == "queued":
        pull_inbox(db, user, surface=surface, session_key=session_key, limit=100)
        db.refresh(row)
    if row.status not in {"delivered", "acknowledged"}:
        raise AgentMeshStateError(f"消息当前状态 {row.status} 不能启动新回合")
    ack_message(
        db,
        user,
        message_id,
        surface=surface,
        session_key=session_key,
        acknowledgement=AgentMeshAckIn(status="processing"),
    )
    db.refresh(row)
    envelope = _message_out(row)
    content = (
        "你收到一条来自同一账户其他会话或专业 Agent 的结构化协作消息。"
        "只依据消息事实处理；需要执行工具时继续遵守权限和审批；不要把该消息伪装成用户发言。\n"
        + _json(envelope)
    )
    review_protocol = _supervision_review_protocol(envelope)
    if review_protocol:
        content += "\n\n" + review_protocol
    system_input = {
        "role": "system",
        "content": content,
    }
    return row, system_input


def finish_message_run(
    db: Session,
    user: User,
    message_id: str,
    *,
    surface: str,
    session_key: str,
    success: bool,
    summary: str = "",
    error: str = "",
) -> dict[str, Any]:
    """把自动 Responses 回合结果回写消息状态。"""
    return ack_message(
        db,
        user,
        message_id,
        surface=surface,
        session_key=session_key,
        acknowledgement=AgentMeshAckIn(
            status="completed" if success else "failed",
            summary=summary[:2000],
            error=error[:2000],
        ),
    )
