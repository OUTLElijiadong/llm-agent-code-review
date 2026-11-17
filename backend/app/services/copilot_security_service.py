"""小菱安全中心：策略引擎与强制点。

管理小菱（``admin_copilot``）是全平台工具权限最大的 Agent：它能读项目源码、
改平台配置、发起运维动作。因此本模块把它的安全口径做成**可配置策略 + 真正生效的闸门**，
而不是一张贴在墙上的检查表。

六个强制点：
1. ``guard_request``：进会话前（IP 白名单 / 会话绑定 / 空闲与绝对超时 / 消息长度 / 注入检测）。
2. ``tighten_tool_decision``：每次工具调用（工具白名单与黑名单 / shell 与生产配置禁令 /
   写操作强制审批 / 单轮与每小时调用预算）。只允许把决策改严，永不放宽。
3. ``redact_for_audit``：审计与工具日志落库前脱敏。
4. ``posture``：安全态势评分，前端"小菱安全中心"直接渲染。
5. ``record_event``：所有拦截与降级都写 ``audit_log``，并可选生成治理告警。
6. ``apply_preset``：四档预设（观察/平衡/严格/最顶级），一键切换。

策略持久化复用 ``system_config``（键 ``copilot_security``），不新增表，
因此升级不需要跑迁移；策略缺失时回落到 ``DEFAULT_POLICY``。
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.agent_governance import PolicyDecisionLog, ToolCallLog
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.audit_log import AuditLog
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import copilot_security_rules as rules
from app.services import observability_service, policy_engine

# 策略在 system_config 里的键。
POLICY_KEY = "copilot_security"
# 审计事件动作前缀；SystemAudit 页按前缀就能筛出全部小菱安全事件。
EVENT_PREFIX = "copilot_security."
# 管理小菱的 Agent 编码（与 agent profile 一致）。
ADMIN_COPILOT_AGENT = "admin_copilot"
# 单个策略列表字段的最大条目数与单条长度，防止把策略表当数据库用。
_LIST_MAX_ITEMS = 100
_LIST_MAX_CHARS = 200
# 会话绑定行的审计动作。
_SESSION_BIND_ACTION = f"{EVENT_PREFIX}session_bind"

# 决策优先级：只允许向上收紧。
_DECISION_PRIORITY = {
    policy_engine.ALLOW: 0,
    policy_engine.ESCALATE: 1,
    policy_engine.DENY: 2,
}
_RISK_PRIORITY = {
    policy_engine.LOW: 0,
    policy_engine.MEDIUM: 1,
    policy_engine.HIGH: 2,
    policy_engine.CRITICAL: 3,
}
_RISK_SCORE = {
    policy_engine.LOW: 0.2,
    policy_engine.MEDIUM: 0.5,
    policy_engine.HIGH: 0.8,
    policy_engine.CRITICAL: 1.0,
}


# ▎安全闸门拒绝一次请求时抛出的错误（403）。前端据 error_code 区分"被策略拦"与"无权限"。
class CopilotSecurityError(ForbiddenError):
    """小菱安全中心闸门拒绝。"""

    error_code = "COPILOT_SECURITY_BLOCKED"

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "",
        detail: Optional[dict] = None,
    ):
        payload = dict(detail or {})
        if reason_code:
            payload.setdefault("reason_code", reason_code)
        self.reason_code = reason_code
        super().__init__(message, code=40330, detail=payload or None)


# ── 策略读写 ──────────────────────────────────────────────────────────────


# ▎轻量协议测试/内部编排可能传入不带 SQLAlchemy 能力的替身；这类会话只能读默认策略、
# ▎不能落库。所有碰数据库的闸门都要先过这一关，否则会把既有单测打断。
def _is_orm_session(db: Any) -> bool:
    """判断传入对象是否具备真实会话能力。"""
    return callable(getattr(db, "query", None)) and callable(getattr(db, "commit", None))


def _raw_policy_json(db: Session) -> Optional[str]:
    if not _is_orm_session(db):
        return None
    row = db.query(SystemConfig).filter(SystemConfig.config_key == POLICY_KEY).first()
    return row.config_value if row else None


def _coerce_policy(stored: Optional[dict]) -> dict:
    """把库里的策略叠加到默认值上，并做类型归一。

    库里被人改坏（字符串写成列表、列表写成字符串）时按类型纠正而不是原样使用：
    ``governed_agents="admin_copilot"`` 若原样进内存会变成字符集合，等于全部不受管。
    """
    policy = dict(rules.DEFAULT_POLICY)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key not in rules.DEFAULT_POLICY and key != "preset":
                continue
            default = rules.DEFAULT_POLICY.get(key)
            if isinstance(default, bool):
                policy[key] = _as_bool(value)
            elif isinstance(default, int):
                try:
                    policy[key] = int(value)
                except (TypeError, ValueError):
                    policy[key] = default
            elif isinstance(default, list):
                if isinstance(value, str):
                    value = [value]
                if isinstance(value, (list, tuple, set)):
                    policy[key] = [str(item) for item in value if str(item).strip()]
            elif isinstance(default, str):
                policy[key] = str(value or "")
            else:
                policy[key] = value
    policy.setdefault("preset", "balanced")
    return policy


def load_policy(db: Session) -> dict:
    """读取当前生效策略。

    Args:
        db: 数据库会话。

    Returns:
        dict: 完整策略字典；库里没有配置时返回 ``DEFAULT_POLICY``。
    """
    raw = _raw_policy_json(db)
    if not raw:
        return _coerce_policy(None)
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("copilot_security 策略 JSON 解析失败，回落默认策略")
        return _coerce_policy(None)
    return _coerce_policy(stored if isinstance(stored, dict) else None)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
    return bool(value)


def _as_int(value: Any, *, field_name: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} 必须是整数")
    if number < minimum or number > maximum:
        raise ValidationError(f"{field_name} 必须在 {minimum}~{maximum} 之间")
    return number


def _as_str_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        raise ValidationError(f"{field_name} 必须是字符串数组")
    items: list[str] = []
    for raw in list(value)[:_LIST_MAX_ITEMS]:
        text = str(raw or "").strip()
        if not text:
            continue
        if len(text) > _LIST_MAX_CHARS:
            raise ValidationError(f"{field_name} 单项长度不能超过 {_LIST_MAX_CHARS}")
        items.append(text)
    return items


# 每个字段的归一化器：名字 → (类型, 校验函数)。
def _sanitize_patch(current: dict, patch: dict) -> tuple[dict, list[str]]:
    """把前端提交的局部更新校验并合并进当前策略。

    Args:
        current: 当前策略。
        patch: 局部更新字段。

    Returns:
        tuple: (新策略, 被忽略的字段名列表)

    Raises:
        ValidationError: 字段类型或取值非法。
    """
    policy = dict(current)
    ignored: list[str] = []
    for key, value in (patch or {}).items():
        if key == "preset":
            if value not in {"balanced", *rules.PRESETS}:
                raise ValidationError(f"未知预设: {value}")
            policy["preset"] = value
            continue
        if key not in rules.DEFAULT_POLICY:
            ignored.append(str(key))
            continue
        default = rules.DEFAULT_POLICY[key]
        if isinstance(default, bool):
            policy[key] = _as_bool(value)
        elif isinstance(default, int):
            bounds = {
                # 阻断阈值不接受 0：0 会让"命中即拦"退化成"永不拦"。
                "injection_block_threshold": (1, 101),
                "injection_warn_threshold": (0, 101),
                "session_idle_timeout_seconds": (0, 7 * 24 * 3600),
                "session_absolute_timeout_seconds": (0, 30 * 24 * 3600),
                "max_message_chars": (0, 100_000),
                "max_tool_calls_per_turn": (0, 1000),
                "max_tool_calls_per_hour": (0, 100_000),
                "step_up_window_seconds": (0, 86_400),
            }
            minimum, maximum = bounds.get(key, (0, 1_000_000))
            policy[key] = _as_int(value, field_name=key, minimum=minimum, maximum=maximum)
        elif isinstance(default, list):
            policy[key] = _as_str_list(value, field_name=key)
            if key == "ip_allowlist":
                try:
                    rules.parse_networks(policy[key])
                except ValueError as exc:
                    raise ValidationError(f"ip_allowlist 含非法 IP/CIDR: {exc}")
        elif isinstance(default, str):
            policy[key] = str(value or "")[:500]
        else:  # pragma: no cover - 默认值类型集合已覆盖
            ignored.append(str(key))

    # 阈值语义：block 必须大于等于 warn，否则"告警区间"失去意义。
    if policy["injection_block_threshold"] < policy["injection_warn_threshold"]:
        raise ValidationError("injection_block_threshold 不能小于 injection_warn_threshold")
    if policy["mode"] not in {"monitor", "balanced", "strict", "max"}:
        raise ValidationError("mode 只能是 monitor/balanced/strict/max")
    return policy, ignored


def _persist(db: Session, policy: dict) -> None:
    payload = json.dumps(policy, ensure_ascii=False, sort_keys=True)
    row = db.query(SystemConfig).filter(SystemConfig.config_key == POLICY_KEY).first()
    if row is None:
        db.add(SystemConfig(config_key=POLICY_KEY, config_value=payload))
    else:
        row.config_value = payload
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def save_policy(
    db: Session,
    *,
    patch: dict,
    actor: Optional[User] = None,
    client_ip: str = "",
) -> dict:
    """保存策略局部更新，并记一条审计事件。

    Args:
        db: 数据库会话。
        patch: 局部更新字段。
        actor: 操作者。
        client_ip: 请求来源 IP。

    Returns:
        dict: 保存后的完整策略。
    """
    current = load_policy(db)
    policy, ignored = _sanitize_patch(current, patch or {})
    changed = sorted(key for key in policy if current.get(key) != policy.get(key))
    _persist(db, policy)
    record_event(
        db,
        kind="policy_update",
        actor=actor,
        client_ip=client_ip,
        status="success",
        detail={"changed": changed, "ignored": ignored, "mode": policy.get("mode")},
    )
    return policy


def apply_preset(
    db: Session,
    *,
    preset: str,
    actor: Optional[User] = None,
    client_ip: str = "",
) -> dict:
    """按预设整体替换策略。

    Args:
        db: 数据库会话。
        preset: ``monitor`` / ``balanced`` / ``strict`` / ``max``。
        actor: 操作者。
        client_ip: 请求来源 IP。

    Returns:
        dict: 应用后的完整策略。
    """
    name = str(preset or "").strip().lower()
    if name not in {*rules.PRESETS, "balanced"}:
        raise ValidationError(f"未知预设: {preset}")
    policy = rules.resolve_preset(name)
    # 保留管理员手工维护的白名单与停机开关，避免"切预设"把安全边界悄悄放宽。
    current = load_policy(db)
    policy["ip_allowlist"] = list(current.get("ip_allowlist") or [])
    policy["tool_allowlist"] = list(current.get("tool_allowlist") or [])
    policy["tool_denylist"] = list(current.get("tool_denylist") or [])
    # 代理拓扑是部署事实，不是安全档位：切预设不应把可信代理清空。
    policy["trusted_proxies"] = list(current.get("trusted_proxies") or [])
    policy["trust_proxy_headers"] = bool(current.get("trust_proxy_headers"))
    policy["kill_switch"] = bool(current.get("kill_switch"))
    policy["kill_switch_reason"] = current.get("kill_switch_reason") or ""
    policy["preset"] = name
    _persist(db, policy)
    record_event(
        db,
        kind="preset_applied",
        actor=actor,
        client_ip=client_ip,
        status="success",
        detail={"preset": name, "mode": policy["mode"]},
    )
    return policy


def set_kill_switch(
    db: Session,
    *,
    enabled: bool,
    reason: str = "",
    actor: Optional[User] = None,
    client_ip: str = "",
) -> dict:
    """打开/关闭管理小菱紧急停机开关。

    Args:
        db: 数据库会话。
        enabled: True 表示立即停机（所有受管控 Agent 拒绝执行）。
        reason: 停机原因。
        actor: 操作者。
        client_ip: 请求来源 IP。

    Returns:
        dict: 更新后的策略。
    """
    policy = load_policy(db)
    policy["kill_switch"] = bool(enabled)
    policy["kill_switch_reason"] = str(reason or "")[:200] if enabled else ""
    _persist(db, policy)
    record_event(
        db,
        kind="kill_switch",
        actor=actor,
        client_ip=client_ip,
        status="success" if enabled else "info",
        detail={"enabled": bool(enabled), "reason": policy["kill_switch_reason"]},
    )
    return policy


# ── 审计事件 ──────────────────────────────────────────────────────────────


def record_event(
    db: Session,
    *,
    kind: str,
    actor: Optional[User] = None,
    client_ip: str = "",
    status: str = "success",
    detail: Optional[dict] = None,
    target: str = "",
) -> Optional[AuditLog]:
    """写一条小菱安全事件到 ``audit_log``。

    事件必须在拦截发生时就落库并提交：调用方随后通常会抛错回滚业务事务，
    但"谁在什么时候被拦"这条记录必须留下来。

    Args:
        db: 数据库会话。
        kind: 事件类型，最终动作名为 ``copilot_security.<kind>``。
        actor: 操作者。
        client_ip: 请求来源 IP。
        status: success / failed / info。
        detail: 事件详情，落库前会脱敏。
        target: 事件对象标识（会话号、工具编码等）。

    Returns:
        AuditLog | None: 落库成功返回记录；失败只记日志不抛错。
    """
    policy = load_policy(db)
    if not policy.get("audit_events_enabled", True) or not _is_orm_session(db):
        return None
    payload = redact_for_audit(json.dumps(detail or {}, ensure_ascii=False, default=str), policy=policy)
    row = AuditLog(
        actor_id=getattr(actor, "id", None) if actor is not None else None,
        actor_name=(getattr(actor, "username", "") or "")[:80] or None,
        action=f"{EVENT_PREFIX}{kind}"[:40],
        target_type="copilot_security",
        target_id=str(target or "")[:80] or None,
        detail=payload,
        status=status,
        ip=(client_ip or "")[:64] or None,
        create_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception:  # noqa: BLE001 - 审计失败不能反过来打断业务主流程
        if _is_orm_session(db):
            db.rollback()
        logger.exception("写入小菱安全事件失败: %s", kind)
        return None
    return row


def _alert(db: Session, *, policy: dict, kind: str, title: str, detail: dict, severity: str = "high") -> None:
    """按策略开关生成治理告警，供管理台弹窗与告警列表。"""
    if not policy.get("alert_on_block", True) or not _is_orm_session(db):
        return
    try:
        observability_service.create_alert(
            db,
            alert_type=f"copilot_security.{kind}",
            severity=severity,
            title=title,
            detail=detail,
            category="security",
            source="copilot_security",
        )
    except Exception:  # noqa: BLE001 - 告警失败不影响拦截本身
        logger.exception("生成小菱安全告警失败: %s", kind)


# ── 脱敏 ──────────────────────────────────────────────────────────────────


def redact_for_audit(text: str, *, policy: Optional[dict] = None) -> str:
    """审计落库前的脱敏。

    Args:
        text: 待脱敏文本。
        policy: 策略（为空时按默认策略：开启脱敏）。

    Returns:
        str: 脱敏后的文本；策略关闭脱敏时原样返回。
    """
    active = policy or rules.DEFAULT_POLICY
    if not active.get("redaction_enabled", True) or not active.get("redact_in_audit", True):
        return text or ""
    redacted, _ = rules.redact_text(text or "", kinds=active.get("redaction_kinds") or None)
    return redacted


def inspect_text(text: str) -> dict:
    """演练接口：只做检测与脱敏，不落库、不拦截。"""
    scan = rules.scan_injection(text or "")
    redacted, applied = rules.redact_text(text or "")
    return {
        "injection": scan,
        "redacted": redacted,
        "redaction_hits": applied,
        "original_length": len(text or ""),
    }


# ── 会话守卫 ──────────────────────────────────────────────────────────────


def resolve_client_ip(request: Any, *, policy: Optional[dict] = None) -> str:
    """解析请求来源 IP。

    默认只信任 socket 对端地址；只有在策略里显式打开 ``trust_proxy_headers``
    时才读 ``X-Forwarded-For``（反代部署下由 Nginx 覆写该头）。

    Args:
        request: FastAPI Request 或任何带 client/headers 的对象。
        policy: 策略。

    Returns:
        str: 来源 IP；解析失败返回空串。
    """
    client = getattr(request, "client", None)
    peer = str(getattr(client, "host", "") or "")
    headers = getattr(request, "headers", None) or {}
    active = policy or rules.DEFAULT_POLICY
    if not active.get("trust_proxy_headers"):
        return peer
    # 只有 peer 本身是可信代理时才采信转发头，否则客户端可以随手伪造。
    trusted = list(active.get("trusted_proxies") or [])
    if not trusted or not rules.ip_allowed(peer, trusted):
        return peer
    forwarded = ""
    try:
        forwarded = str(headers.get("x-forwarded-for") or "")
    except (AttributeError, TypeError):
        forwarded = ""
    if not forwarded:
        return peer
    # 从右往左跳过可信代理，取第一个不可信地址（最左项由客户端控制，不可信）。
    trusted_networks = rules.parse_networks(trusted)
    chain = [item.strip() for item in forwarded.split(",") if item.strip()]
    for candidate in reversed(chain):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if any(address in network for network in trusted_networks):
            continue
        return str(address)
    return peer


# 审计表 target_id 只有 80 字符，写入与查询必须用同一个截断口径。
_SESSION_KEY_LIMIT = 80


def _session_key(session_id: str) -> str:
    """绑定用的会话键：统一截断，避免超长 session_id 永远查不到绑定。"""
    return str(session_id or "")[:_SESSION_KEY_LIMIT]


def _session_binding(db: Session, session_id: str) -> Optional[AuditLog]:
    if not _is_orm_session(db):
        return None
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.action == _SESSION_BIND_ACTION,
            AuditLog.target_id == _session_key(session_id),
        )
        .order_by(AuditLog.id.desc())
        .first()
    )


def _write_session_binding(
    db: Session,
    *,
    actor: Optional[User],
    session_id: str,
    client_ip: str,
) -> None:
    """写入会话绑定行。

    绑定必须独立于 ``audit_events_enabled`` 与脱敏开关：IP 存在不脱敏的
    ``AuditLog.ip`` 列，detail 只放长度这类无害信息，避免脱敏把 IP 抹成占位符
    导致每次请求都被判成"IP 不一致"而自锁。
    """
    if not _is_orm_session(db):
        return
    row = AuditLog(
        actor_id=getattr(actor, "id", None) if actor is not None else None,
        actor_name=(getattr(actor, "username", "") or "")[:80] or None,
        action=_SESSION_BIND_ACTION,
        target_type="copilot_security",
        target_id=_session_key(session_id) or None,
        detail=json.dumps({"bound": True}, ensure_ascii=False),
        status="success",
        ip=(client_ip or "")[:64] or None,
        create_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    try:
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001 - 绑定失败按未绑定处理，不阻断请求
        db.rollback()
        logger.exception("写入小菱会话绑定失败")


def _session_timeout_hit(
    db: Session,
    *,
    actor: Optional[User],
    session_id: str,
    policy: dict,
) -> Optional[str]:
    """检查会话空闲/绝对超时；命中返回原因，未命中返回 None。"""
    idle_limit = int(policy.get("session_idle_timeout_seconds") or 0)
    absolute_limit = int(policy.get("session_absolute_timeout_seconds") or 0)
    if not idle_limit and not absolute_limit:
        return None
    if not _is_orm_session(db):
        return None
    user_id = getattr(actor, "id", None)
    if not isinstance(user_id, int):
        return None
    row = (
        db.query(AgentResponseRun)
        .filter(
            AgentResponseRun.user_id == user_id,
            AgentResponseRun.session_key == session_id,
        )
        .order_by(AgentResponseRun.id.desc())
        .first()
    )
    if row is None:
        return None
    # 绝对存活时间从"这个会话的第一轮"算起，否则每发一轮就被重置，永远不会触发。
    first = (
        db.query(func.min(AgentResponseRun.create_time))
        .filter(
            AgentResponseRun.user_id == user_id,
            AgentResponseRun.session_key == session_id,
        )
        .scalar()
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    updated = getattr(row, "update_time", None) or getattr(row, "create_time", None)
    created = first or getattr(row, "create_time", None) or updated
    if absolute_limit and created and (now - created).total_seconds() > absolute_limit:
        return f"会话已超过绝对存活时间（{absolute_limit}s），请新建会话"
    if idle_limit and updated and (now - updated).total_seconds() > idle_limit:
        return f"会话已空闲超过 {idle_limit}s，请新建会话"
    return None


# ▎一次入站请求的守卫结果：放行或拦截 + 判定证据。
def guard_request(
    db: Session,
    *,
    actor: Optional[User],
    surface: str,
    session_id: str = "",
    texts: Iterable[str] = (),
    client_ip: str = "",
    user_agent: str = "",
    policy: Optional[dict] = None,
) -> dict:
    """入站守卫：停机开关 / IP 白名单 / 会话绑定 / 超时 / 消息长度 / 注入检测。

    Args:
        db: 数据库会话。
        actor: 触发用户。
        surface: ``user`` 或 ``admin``。
        session_id: 会话键。
        texts: 本次请求里需要检测的文本（一般只有最后一条用户消息）。
        client_ip: 来源 IP。
        user_agent: 来源 UA（仅记录，不参与判定）。
        policy: 已加载策略，避免重复查询。

    Returns:
        dict: ``{"allowed": True, "injection": {...}, "warnings": [...]}``。

    Raises:
        CopilotSecurityError: 命中任一硬闸门。
    """
    active = policy or load_policy(db)
    payload_texts = [text for text in texts if isinstance(text, str) and text]
    result: dict[str, Any] = {
        "allowed": True,
        "surface": surface,
        "policy_mode": active.get("mode"),
        "injection": {"score": 0, "hits": []},
        "warnings": [],
    }

    # 1) 紧急停机：最高优先级，且不受策略总开关影响（关掉总开关不能顺手解除停机）。
    if active.get("kill_switch"):
        reason = active.get("kill_switch_reason") or "管理员已开启紧急停机"
        record_event(
            db, kind="kill_switch_blocked", actor=actor, client_ip=client_ip,
            status="failed", target=session_id, detail={"reason": reason, "surface": surface},
        )
        _alert(
            db, policy=active, kind="kill_switch_blocked", severity="critical",
            title="管理小菱处于紧急停机状态，请求被拒绝",
            detail={"actor": getattr(actor, "username", ""), "ip": client_ip, "surface": surface},
        )
        raise CopilotSecurityError(
            f"管理小菱已紧急停机：{reason}", reason_code="kill_switch", detail={"reason": reason},
        )

    if not active.get("enabled", True):
        result["disabled"] = True
        return result

    guarded = surface in (active.get("session_guard_surfaces") or [])

    # 2) IP 白名单（仅守卫面生效；白名单为空表示不限来源）。
    allowlist = list(active.get("ip_allowlist") or [])
    if guarded and allowlist and not rules.ip_allowed(client_ip, allowlist):
        record_event(
            db, kind="ip_denied", actor=actor, client_ip=client_ip,
            status="failed", target=session_id, detail={"surface": surface, "allowlist_size": len(allowlist)},
        )
        _alert(
            db, policy=active, kind="ip_denied", severity="high",
            title="管理小菱收到非白名单来源的请求，已拒绝",
            detail={"ip": client_ip, "actor": getattr(actor, "username", "")},
        )
        raise CopilotSecurityError(
            "来源 IP 不在管理小菱的访问白名单内", reason_code="ip_not_allowed",
            detail={"ip": client_ip},
        )

    # 3) 会话绑定 IP：同一 session_id 换 IP 视为会话被劫持。
    if guarded and active.get("bind_session_to_ip") and session_id and client_ip:
        binding = _session_binding(db, session_id)
        if binding is None:
            _write_session_binding(db, actor=actor, session_id=session_id, client_ip=client_ip)
            record_event(
                db, kind="session_bind", actor=actor, client_ip=client_ip,
                status="info", target=_session_key(session_id),
                detail={"bound": True, "user_agent_len": len(user_agent or "")},
            )
        else:
            # 绑定 IP 取不脱敏的 AuditLog.ip 列。
            bound_ip = str(binding.ip or "")
            if bound_ip and bound_ip != client_ip:
                record_event(
                    db, kind="session_ip_mismatch", actor=actor, client_ip=client_ip,
                    status="failed", target=session_id, detail={"bound_ip": bound_ip},
                )
                _alert(
                    db, policy=active, kind="session_ip_mismatch", severity="critical",
                    title="管理小菱会话来源 IP 变化，疑似会话被劫持",
                    detail={"bound_ip": bound_ip, "current_ip": client_ip, "session": session_id},
                )
                raise CopilotSecurityError(
                    "会话来源 IP 与创建时不一致，请重新登录后新建会话",
                    reason_code="session_ip_mismatch",
                )

    # 4) 空闲/绝对超时。
    if guarded:
        timeout_reason = _session_timeout_hit(db, actor=actor, session_id=session_id, policy=active)
        if timeout_reason:
            record_event(
                db, kind="session_timeout", actor=actor, client_ip=client_ip,
                status="failed", target=session_id, detail={"reason": timeout_reason},
            )
            raise CopilotSecurityError(timeout_reason, reason_code="session_timeout")

    # 5) 单条消息长度。
    max_chars = int(active.get("max_message_chars") or 0)
    if max_chars:
        longest = max((len(text) for text in payload_texts), default=0)
        total_chars = sum(len(text) for text in payload_texts)
        if total_chars > max_chars:
            record_event(
                db, kind="message_too_long", actor=actor, client_ip=client_ip,
                status="failed", target=_session_key(session_id),
                detail={"total_length": total_chars, "limit": max_chars, "pieces": len(payload_texts)},
            )
            raise CopilotSecurityError(
                f"本次请求文本总量超过 {max_chars} 字符上限，请拆分后重试",
                reason_code="message_too_long", detail={"total_length": total_chars, "limit": max_chars},
            )
        if longest > max_chars:
            record_event(
                db, kind="message_too_long", actor=actor, client_ip=client_ip,
                status="failed", target=session_id, detail={"length": longest, "limit": max_chars},
            )
            raise CopilotSecurityError(
                f"单条消息超过 {max_chars} 字符上限，请拆分后重试",
                reason_code="message_too_long", detail={"length": longest, "limit": max_chars},
            )

    # 6) 提示注入检测。
    if active.get("injection_scan_enabled", True) and payload_texts:
        blocked_surfaces = set(active.get("injection_blocked_surfaces") or [])
        block_threshold = int(active.get("injection_block_threshold") or 101)
        warn_threshold = int(active.get("injection_warn_threshold") or 101)
        best: dict = {"score": 0, "hits": []}
        for text in payload_texts:
            scan = rules.scan_injection(text)
            if int(scan["score"]) > int(best["score"]):
                best = scan
        result["injection"] = best
        score = int(best["score"])
        if score >= warn_threshold and score < block_threshold:
            result["warnings"].append(
                {"code": "injection_warning", "score": score, "hits": best["hits"]}
            )
            record_event(
                db, kind="injection_warning", actor=actor, client_ip=client_ip,
                status="info", target=session_id,
                detail={"score": score, "hits": [hit["code"] for hit in best["hits"]], "surface": surface},
            )
        if score >= block_threshold and surface in blocked_surfaces:
            codes = [hit["code"] for hit in best["hits"]]
            record_event(
                db, kind="injection_blocked", actor=actor, client_ip=client_ip,
                status="failed", target=session_id,
                detail={"score": score, "codes": codes, "surface": surface,
                        "evidence": [hit["evidence"] for hit in best["hits"]][:5]},
            )
            _alert(
                db, policy=active, kind="injection_blocked", severity="high",
                title="管理小菱拦截了一次提示注入尝试",
                detail={"actor": getattr(actor, "username", ""), "ip": client_ip, "score": score, "codes": codes},
            )
            raise CopilotSecurityError(
                "该请求命中提示注入特征，已按小菱安全中心策略拦截",
                reason_code="prompt_injection",
                detail={"score": score, "codes": codes},
            )
    return result


# ── 工具闸门 ──────────────────────────────────────────────────────────────


def _governed_agent(policy: dict, agent_code: str) -> bool:
    governed = {str(code) for code in (policy.get("governed_agents") or [])}
    return str(agent_code or "") in governed


def _count_turn_calls(db: Session, run_id: str) -> int:
    if not run_id or not _is_orm_session(db):
        return 0
    return int(
        db.query(func.count(AgentToolExecution.id))
        .filter(AgentToolExecution.run_id == run_id)
        .scalar()
        or 0
    )


def _count_hourly_calls(db: Session, agent_code: str) -> int:
    if not _is_orm_session(db):
        return 0
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    return int(
        db.query(func.count(ToolCallLog.id))
        .filter(ToolCallLog.agent_code == agent_code, ToolCallLog.create_time >= since)
        .scalar()
        or 0
    )


def _tighten(
    db: Session,
    decision: policy_engine.PolicyDecision,
    *,
    subject: str,
    action: str,
    resource: str,
    context: dict,
    target: str,
    risk_level: str,
    reason: str,
) -> policy_engine.PolicyDecision:
    """把决策向 target 收紧（只进不退），并落一条策略决策日志。"""
    if decision.decision not in _DECISION_PRIORITY:
        decision = replace(
            decision, decision=policy_engine.DENY, risk_level=policy_engine.CRITICAL,
            risk_score=_RISK_SCORE[policy_engine.CRITICAL], reason="上游策略返回未知决策值，已阻断",
        )
    if _DECISION_PRIORITY[target] <= _DECISION_PRIORITY[decision.decision]:
        return decision
    risk = max(
        (decision.risk_level, risk_level),
        key=lambda level: _RISK_PRIORITY.get(level, 0),
    )
    tightened = replace(
        decision, decision=target, risk_level=risk,
        risk_score=_RISK_SCORE.get(risk, 1.0), reason=reason,
    )
    if not _is_orm_session(db):
        return tightened
    try:
        row = PolicyDecisionLog(
            subject=subject,
            action=action,
            resource=resource or "*",
            decision=tightened.decision,
            risk_level=tightened.risk_level,
            risk_score=tightened.risk_score,
            reason=tightened.reason,
            context_json=json.dumps(context or {}, ensure_ascii=False, default=str)[:20000],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        tightened.log_id = row.id
    except Exception:  # noqa: BLE001 - 决策日志失败不改变已收紧的决策
        db.rollback()
        logger.exception("写入小菱安全策略决策日志失败")
    return tightened


def tighten_tool_decision(
    db: Session,
    *,
    decision: policy_engine.PolicyDecision,
    agent_code: str,
    tool_code: str,
    action: str,
    resource: str = "",
    actor: Optional[User] = None,
    context: Optional[dict] = None,
    client_ip: str = "",
    policy: Optional[dict] = None,
) -> policy_engine.PolicyDecision:
    """工具闸门：把一次工具调用决策按小菱安全策略收紧。

    只允许更严：``allow → escalate → deny``。策略关闭或 Agent 不在管控名单时原样返回。

    Args:
        db: 数据库会话。
        decision: 上游（策略引擎 + 职责边界 + 工具权限）给出的决策。
        agent_code: Agent 编码。
        tool_code: 工具编码。
        action: 动作编码。
        resource: 资源编码。
        actor: 触发用户。
        context: 调用上下文（含 run_id / copilot_request_id）。
        client_ip: 来源 IP。
        policy: 已加载策略。

    Returns:
        policy_engine.PolicyDecision: 收紧后的决策。
    """
    active = policy or load_policy(db)
    # 停机是全局开关：即使 agent 不在受管名单里也必须拒（否则名单外编码等于后门）。
    if active.get("kill_switch"):
        tightened = _tighten(
            db, decision, subject=f"agent:{agent_code}", action=action, resource=resource,
            context=dict(context or {}), target=policy_engine.DENY,
            risk_level=policy_engine.CRITICAL,
            reason="管理小菱处于紧急停机状态，工具调用全部拒绝",
        )
        record_event(
            db, kind="kill_switch_tool", actor=actor, client_ip=client_ip, status="failed",
            target=tool_code, detail={"action": action, "resource": resource, "agent": agent_code},
        )
        _alert(
            db, policy=active, kind="kill_switch_tool", severity="critical",
            title="管理小菱处于紧急停机状态，工具调用被拒",
            detail={"agent": agent_code, "tool": tool_code, "actor": getattr(actor, "username", "")},
        )
        return tightened

    if not _governed_agent(active, agent_code):
        return decision

    ctx = dict(context or {})
    subject = f"agent:{agent_code}"
    flags = rules.classify_action(action, tool_code, resource)

    if not active.get("enabled", True):
        return decision

    def _block(target: str, risk: str, reason: str, kind: str, detail: dict) -> policy_engine.PolicyDecision:
        tightened = _tighten(
            db, decision, subject=subject, action=action, resource=resource,
            context=ctx, target=target, risk_level=risk, reason=reason,
        )
        record_event(
            db, kind=kind, actor=actor, client_ip=client_ip, status="failed",
            target=tool_code, detail={"action": action, "resource": resource, "reason": reason, **detail},
        )
        _alert(
            db, policy=active, kind=kind, severity="high" if target == policy_engine.DENY else "warning",
            title=f"管理小菱工具调用被收紧：{reason}",
            detail={"agent": agent_code, "tool": tool_code, "action": action,
                    "resource": resource, "actor": getattr(actor, "username", "")},
        )
        return tightened

    # 1) 工具黑白名单。
    denylist = {str(item) for item in (active.get("tool_denylist") or [])}
    allowlist = {str(item) for item in (active.get("tool_allowlist") or [])}
    if tool_code in denylist:
        return _block(
            policy_engine.DENY, policy_engine.CRITICAL,
            f"工具 {tool_code} 在小菱安全中心黑名单内", "tool_denied",
            {"tool_denylist": True},
        )
    if allowlist and tool_code not in allowlist:
        return _block(
            policy_engine.DENY, policy_engine.HIGH,
            f"工具 {tool_code} 不在小菱安全中心白名单内", "tool_not_allowlisted",
            {"allowlist_size": len(allowlist)},
        )

    # 2) 系统命令执行。
    if "shell" in flags and active.get("deny_shell", True):
        return _block(
            policy_engine.DENY, policy_engine.CRITICAL,
            "小菱安全中心禁止管理小菱执行系统命令", "shell_denied", {"flags": sorted(flags)},
        )

    # 3) 生产配置/权限/凭据类改动。
    if "production" in flags and active.get("deny_production_config", True):
        return _block(
            policy_engine.DENY, policy_engine.CRITICAL,
            "小菱安全中心禁止管理小菱改动生产配置/权限/凭据", "production_config_denied",
            {"flags": sorted(flags)},
        )

    # 4) 写类动作。
    if "write" in flags:
        if active.get("deny_write_actions"):
            return _block(
                policy_engine.DENY, policy_engine.HIGH,
                "小菱安全中心当前为只读模式，拒绝写操作", "write_denied", {"flags": sorted(flags)},
            )
        if active.get("require_approval_for_writes"):
            return _block(
                policy_engine.ESCALATE, policy_engine.HIGH,
                "写操作需管理员审批后执行", "write_requires_approval", {"flags": sorted(flags)},
            )

    # 4.5) 外带通道：能把数据带出平台的工具（HTTP/邮件/上传/浏览器）。
    if "egress" in flags:
        if active.get("deny_egress"):
            return _block(
                policy_engine.DENY, policy_engine.HIGH,
                "小菱安全中心禁止外带类工具", "egress_denied", {"flags": sorted(flags)},
            )
        if active.get("require_approval_for_writes"):
            return _block(
                policy_engine.ESCALATE, policy_engine.HIGH,
                "外带类工具需管理员审批后执行", "egress_requires_approval", {"flags": sorted(flags)},
            )

    # 5) 调用预算：已超额一律拒绝，不再转审批。
    run_id = str(ctx.get("run_id") or "")
    per_turn = int(active.get("max_tool_calls_per_turn") or 0)
    if per_turn:
        used = _count_turn_calls(db, run_id)
        if used >= per_turn:
            return _block(
                policy_engine.DENY, policy_engine.HIGH,
                f"单轮工具调用已达上限 {per_turn} 次", "turn_budget_exceeded",
                {"used": used, "limit": per_turn, "run_id": run_id},
            )
    per_hour = int(active.get("max_tool_calls_per_hour") or 0)
    if per_hour:
        used = _count_hourly_calls(db, agent_code)
        if used >= per_hour:
            return _block(
                policy_engine.DENY, policy_engine.HIGH,
                f"每小时工具调用已达上限 {per_hour} 次", "hour_budget_exceeded",
                {"used": used, "limit": per_hour},
            )
    # 6) 名字看不出风险、也不在只读白名单里的工具：升级审批而不是静默放行。
    if (
        not flags
        and active.get("escalate_unknown_tools")
        and tool_code not in rules.READ_ONLY_TOOL_CODES
    ):
        return _block(
            policy_engine.ESCALATE, policy_engine.HIGH,
            f"未知风险工具 {tool_code} 需管理员审批", "undeclared_tool_requires_approval",
            {"tool_code": tool_code},
        )

    # 7) 未知高风险工具：允许但必须审批。
    if decision.risk_level in {policy_engine.HIGH, policy_engine.CRITICAL} and active.get(
        "dangerous_tools_require_approval", True
    ):
        return _block(
            policy_engine.ESCALATE, decision.risk_level,
            "高风险工具调用需管理员审批", "dangerous_tool_requires_approval",
            {"risk": decision.risk_level},
        )

    return decision


# ── 工具输出回流扫描 ──────────────────────────────────────────────────────


def scan_tool_output(
    db: Session,
    *,
    agent_code: str,
    tool_code: str,
    output: Any,
    actor: Optional[User] = None,
    client_ip: str = "",
    policy: Optional[dict] = None,
) -> dict:
    """工具输出回流给模型前做注入扫描（间接注入闸门）。

    工具读回来的源码、网页、团队结果都可能被攻击者污染；这里只做检测与留痕，
    不改写数据（改写会破坏 XML/JSON 契约），命中即记事件并告警。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        tool_code: 工具编码。
        output: 工具返回值。
        actor: 触发用户。
        client_ip: 来源 IP。
        policy: 已加载策略。

    Returns:
        dict: ``{"score": int, "codes": [...], "suspicious": bool}``。
    """
    active = policy or load_policy(db)
    if not active.get("enabled", True) or not active.get("scan_tool_output", True):
        return {"score": 0, "codes": [], "suspicious": False}
    if not _governed_agent(active, agent_code):
        return {"score": 0, "codes": [], "suspicious": False}
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    scan = rules.scan_injection((text or "")[:20000])
    score = int(scan["score"])
    warn = int(active.get("injection_warn_threshold") or 101)
    codes = [hit["code"] for hit in scan["hits"]]
    suspicious = score >= warn
    if suspicious:
        record_event(
            db, kind="tool_output_injection", actor=actor, client_ip=client_ip,
            status="failed", target=tool_code,
            detail={"agent": agent_code, "score": score, "codes": codes,
                    "evidence": [hit["evidence"] for hit in scan["hits"]][:3]},
        )
        _alert(
            db, policy=active, kind="tool_output_injection", severity="high",
            title="工具输出疑似被投毒（间接注入）",
            detail={"agent": agent_code, "tool": tool_code, "score": score, "codes": codes,
                    "actor": getattr(actor, "username", "")},
        )
    return {"score": score, "codes": codes, "suspicious": suspicious}


# ── 安全态势 ──────────────────────────────────────────────────────────────


# 检查项定义：(键, 标题, 权重, 是否关键项, 修复建议)
_POSTURE_CHECKS: tuple[tuple[str, str, int, bool, str], ...] = (
    ("policy_enabled", "安全策略已启用", 6, True, "关闭策略等于放弃全部闸门，请保持启用。"),
    ("kill_switch_off", "紧急停机开关未开启", 4, True, "停机只用于事故处置；日常请关闭。"),
    ("ip_allowlist", "配置了来源 IP 白名单", 10, True,
     "把管理小菱限制在内网出口/办公固定 IP，例如 10.0.0.0/8、203.0.113.7/32。"),
    ("trust_proxy_headers", "未无条件信任转发头", 3, False,
     "除非前置反代会覆写 X-Forwarded-For，否则不要打开，防止伪造来源。"),
    ("bind_session_to_ip", "会话与来源 IP 绑定", 10, True, "阻止 cookie/令牌被搬到其他主机继续用。"),
    ("idle_timeout", "配置了会话空闲超时", 8, True, "建议 1800s：人离开后会话自动失效。"),
    ("absolute_timeout", "配置了会话绝对超时", 6, True, "建议 28800s：即使一直在用也要强制重登。"),
    ("message_limit", "配置了单条消息长度上限", 4, False, "建议 20000 字符，阻断超长载荷与刷量。"),
    ("injection_scan", "提示注入检测已开启", 10, True, "关闭后小菱会直接吃下被污染的上下文。"),
    ("injection_threshold", "注入阻断阈值足够低", 8, True, "建议 ≤40 分：命中一条高危规则即拦。"),
    ("injection_admin", "管理员面也拦截注入", 6, True,
     "管理员小菱工具最多，最不该豁免注入检测。"),
    ("redaction", "审计落库前脱敏", 8, True, "避免密钥、手机号、内网 IP 写进审计表。"),
    ("deny_shell", "禁止系统命令执行", 10, True, "shell 类工具是把平台打穿的最短路径。"),
    ("deny_production", "禁止改生产配置/权限/凭据", 10, True, "配置与凭据类改动必须人工执行。"),
    ("no_hard_deny_write", "写操作未被硬性禁死", 2, False,
     "只读模式适合事故期；日常建议改为“写操作需审批”。"),
    ("write_approval", "写操作强制审批", 8, True, "写操作走审批单，留痕且可回滚。"),
    ("dangerous_approval", "高风险工具强制审批", 8, True, "未知高风险工具默认升级审批。"),
    ("tool_allowlist", "工具白名单最小权限", 8, True, "按职责列出管理小菱真正需要的工具编码。"),
    ("turn_budget", "单轮工具调用预算", 5, True, "建议 24 次，防止被诱导做批量破坏。"),
    ("hour_budget", "每小时工具调用预算", 5, True, "建议 120 次，限制横向刷数据的速率。"),
    ("audit_enabled", "安全事件全部落审计", 8, True, "拦截必须留痕，否则事后无法复盘。"),
    ("alert_on_block", "拦截时生成告警", 4, False, "让管理员在弹窗里第一时间看到攻击。"),
)
# 评分等级：分数 → 等级。
_GRADE_BANDS: tuple[tuple[int, str], ...] = (
    (95, "A+"), (90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "E"),
)


def _check_passed(key: str, policy: dict) -> bool:
    if key == "policy_enabled":
        return bool(policy.get("enabled", True))
    if key == "kill_switch_off":
        return not bool(policy.get("kill_switch"))
    if key == "ip_allowlist":
        return bool(policy.get("ip_allowlist"))
    if key == "trust_proxy_headers":
        return not bool(policy.get("trust_proxy_headers"))
    if key == "bind_session_to_ip":
        return bool(policy.get("bind_session_to_ip"))
    if key == "idle_timeout":
        return int(policy.get("session_idle_timeout_seconds") or 0) > 0
    if key == "absolute_timeout":
        return int(policy.get("session_absolute_timeout_seconds") or 0) > 0
    if key == "message_limit":
        return int(policy.get("max_message_chars") or 0) > 0
    if key == "injection_scan":
        return bool(policy.get("injection_scan_enabled", True))
    if key == "injection_threshold":
        return int(policy.get("injection_block_threshold") or 101) <= 40
    if key == "injection_admin":
        return "admin" in set(policy.get("injection_blocked_surfaces") or [])
    if key == "redaction":
        return bool(policy.get("redaction_enabled", True)) and bool(policy.get("redact_in_audit", True))
    if key == "deny_shell":
        return bool(policy.get("deny_shell", True))
    if key == "deny_production":
        return bool(policy.get("deny_production_config", True))
    if key == "no_hard_deny_write":
        return not bool(policy.get("deny_write_actions"))
    if key == "write_approval":
        return bool(policy.get("require_approval_for_writes"))
    if key == "dangerous_approval":
        return bool(policy.get("dangerous_tools_require_approval", True))
    if key == "tool_allowlist":
        return bool(policy.get("tool_allowlist"))
    if key == "turn_budget":
        return int(policy.get("max_tool_calls_per_turn") or 0) > 0
    if key == "hour_budget":
        return int(policy.get("max_tool_calls_per_hour") or 0) > 0
    if key == "audit_enabled":
        return bool(policy.get("audit_events_enabled", True))
    if key == "alert_on_block":
        return bool(policy.get("alert_on_block", True))
    return False


def _event_counters(db: Session, *, hours: int = 24) -> dict:
    if not _is_orm_session(db):
        return {"window_hours": hours, "total": 0, "blocked": 0, "by_action": {}}
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = (
        db.query(AuditLog.action, AuditLog.status, func.count(AuditLog.id))
        .filter(AuditLog.action.like(f"{EVENT_PREFIX}%"), AuditLog.create_time >= since)
        .group_by(AuditLog.action, AuditLog.status)
        .all()
    )
    by_action: dict[str, int] = {}
    blocked = 0
    total = 0
    for action, status, count in rows:
        short = str(action or "").removeprefix(EVENT_PREFIX)
        amount = int(count or 0)
        by_action[short] = by_action.get(short, 0) + amount
        total += amount
        if str(status) == "failed":
            blocked += amount
    return {"window_hours": hours, "total": total, "blocked": blocked, "by_action": by_action}


def posture(db: Session, *, policy: Optional[dict] = None) -> dict:
    """计算小菱安全中心态势评分。

    Args:
        db: 数据库会话。
        policy: 已加载策略。

    Returns:
        dict: ``{score, grade, checks, recommendations, events, policy, generated_at}``。
    """
    active = policy or load_policy(db)
    checks: list[dict] = []
    earned = 0
    total_weight = 0
    for key, title, weight, critical, remediation in _POSTURE_CHECKS:
        passed = _check_passed(key, active)
        total_weight += weight
        if passed:
            earned += weight
        checks.append({
            "key": key,
            "title": title,
            "weight": weight,
            "critical": critical,
            "passed": passed,
            "remediation": remediation,
        })
    score = int(round(100 * earned / total_weight)) if total_weight else 0
    grade = next(band for threshold, band in _GRADE_BANDS if score >= threshold)
    recommendations = [item["remediation"] for item in checks if not item["passed"]]
    critical_gaps = [item["key"] for item in checks if not item["passed"] and item["critical"]]
    return {
        "score": score,
        "grade": grade,
        "checks": checks,
        "recommendations": recommendations,
        "critical_gaps": critical_gaps,
        "events": _event_counters(db),
        "policy": {
            "enabled": bool(active.get("enabled", True)),
            "mode": active.get("mode"),
            "preset": active.get("preset", "custom"),
            "kill_switch": bool(active.get("kill_switch")),
            "kill_switch_reason": active.get("kill_switch_reason") or "",
            "governed_agents": list(active.get("governed_agents") or []),
            "session_guard_surfaces": list(active.get("session_guard_surfaces") or []),
            "ip_allowlist": list(active.get("ip_allowlist") or []),
            "trusted_proxies": list(active.get("trusted_proxies") or []),
            "tool_allowlist": list(active.get("tool_allowlist") or []),
            "tool_denylist": list(active.get("tool_denylist") or []),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 事件查询（供安全中心列表；导出与趋势由 copilot_security_export_service 承担）──


def list_events(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 20,
    kind: str = "",
    status: str = "",
    days: int = 7,
) -> dict:
    """分页查询小菱安全事件。

    Args:
        db: 数据库会话。
        page: 页码（从 1 开始）。
        page_size: 每页条数。
        kind: 事件类型短名过滤，如 ``injection_blocked``。
        status: success / failed / info。
        days: 时间窗口天数。

    Returns:
        dict: ``{items, total, page, page_size, pages}``。
    """
    query = db.query(AuditLog).filter(AuditLog.action.like(f"{EVENT_PREFIX}%"))
    if kind:
        query = query.filter(AuditLog.action == f"{EVENT_PREFIX}{kind}")
    if status:
        query = query.filter(AuditLog.status == status)
    if days:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(days))
        query = query.filter(AuditLog.create_time >= since)
    total = int(query.count() or 0)
    rows = (
        query.order_by(AuditLog.id.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
        .all()
    )
    items = [
        {
            "id": row.id,
            "kind": str(row.action or "").removeprefix(EVENT_PREFIX),
            "status": row.status,
            "actor_id": row.actor_id,
            "actor_name": row.actor_name,
            "target": row.target_id,
            "ip": row.ip,
            "detail": row.detail,
            "create_time": row.create_time.isoformat() if row.create_time else None,
        }
        for row in rows
    ]
    pages = (total + page_size - 1) // page_size if page_size else 0
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


# 事件类型目录：前端筛选下拉直接用这份，避免前后端各写一套字符串。
EVENT_KINDS: tuple[dict[str, str], ...] = (
    {"kind": "injection_blocked", "label": "提示注入被拦截"},
    {"kind": "injection_warning", "label": "注入特征告警（未拦截）"},
    {"kind": "tool_denied", "label": "工具黑名单拒绝"},
    {"kind": "tool_not_allowlisted", "label": "工具不在白名单"},
    {"kind": "egress_denied", "label": "外带类工具被拒"},
    {"kind": "egress_requires_approval", "label": "外带类工具转审批"},
    {"kind": "undeclared_tool_requires_approval", "label": "未知风险工具转审批"},
    {"kind": "tool_output_injection", "label": "工具输出疑似投毒"},
    {"kind": "shell_denied", "label": "系统命令被拒"},
    {"kind": "production_config_denied", "label": "生产配置改动被拒"},
    {"kind": "write_denied", "label": "写操作被拒（只读模式）"},
    {"kind": "write_requires_approval", "label": "写操作转审批"},
    {"kind": "dangerous_tool_requires_approval", "label": "高风险工具转审批"},
    {"kind": "turn_budget_exceeded", "label": "单轮调用超预算"},
    {"kind": "hour_budget_exceeded", "label": "每小时调用超预算"},
    {"kind": "kill_switch", "label": "紧急停机开关"},
    {"kind": "kill_switch_blocked", "label": "停机会话被拒"},
    {"kind": "kill_switch_tool", "label": "停机工具被拒"},
    {"kind": "ip_denied", "label": "来源 IP 被拒"},
    {"kind": "session_bind", "label": "会话绑定 IP"},
    {"kind": "session_ip_mismatch", "label": "会话 IP 变化"},
    {"kind": "session_timeout", "label": "会话超时"},
    {"kind": "message_too_long", "label": "消息超长"},
    {"kind": "policy_update", "label": "策略更新"},
    {"kind": "preset_applied", "label": "预设切换"},
)
