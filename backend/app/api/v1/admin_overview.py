"""管理员总览大屏聚合 API。

汇总服务器状态、安全态势、登录来源地理分布、Agent 活跃状态,
供管理员总览大屏一次拉取渲染。
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.agents.event_bus import AgentEventBus
from app.core.database import get_db
from app.core.dependencies import require_admin, require_super_admin
from app.models.agent_governance import AgentProfile, ToolCallLog
from app.models.ai_call_log import AiCallLog
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.common import Resp
from app.services import system_service
from app.services.geoip_service import locate_ip_cached
from app.services.security_posture_service import security_posture

router = APIRouter()


# 讨论编排器使用的是审查画像名,治理中心使用注册 Agent code。两者必须
# 在统计层统一,否则圆桌的 general/security/reliability 等调用会从大屏
# 消失,让管理员误以为没有产生模型消费。
_AGENT_LABEL_ALIASES: dict[str, str] = {
    "general": "code_reviewer",
    "reliability": "code_reviewer",
    "performance": "code_reviewer",
    "maintainability": "code_reviewer",
    "security": "security_sentinel",
}


def _activity_agent_codes(
    agent_label: Optional[str],
    model_name: Optional[str],
    profile_codes: set[str],
) -> set[str]:
    """把历史/圆桌调用日志归并到当前 Agent profile。

    ``agent_label`` 是首选事实来源。旧日志可能没有 label,但把画像写入了
    ``model_name`` 的 ``/xxx-agent`` 后缀,因此按后缀补归因。没有可靠标识
    的原始调用留给“未归因模型调用”,不猜测为某个 Agent。
    """
    raw_label = str(agent_label or "").strip()
    if raw_label:
        if raw_label in profile_codes:
            return {raw_label}
        alias = _AGENT_LABEL_ALIASES.get(raw_label.lower())
        if alias in profile_codes:
            return {alias}

    raw_model = str(model_name or "")
    if raw_model.endswith("/multi-agent"):
        return {
            code for code in {"code_reviewer", "security_sentinel"}
            if code in profile_codes
        }
    if "/" in raw_model and raw_model.rsplit("/", 1)[-1].endswith("-agent"):
        suffix = raw_model.rsplit("/", 1)[-1][:-len("-agent")]
        if suffix in profile_codes:
            return {suffix}
        alias = _AGENT_LABEL_ALIASES.get(suffix.lower())
        if alias in profile_codes:
            return {alias}
    return set()


def _agent_activity(db: Session) -> list[dict]:
    """返回各 Agent 的当前阶段、今日调用量与主要用途。

    优先使用本进程事件总线的最新阶段事件；事件超过 90 秒自动过期，
    防止历史任务长期显示为运行中。跨进程或短暂无事件时，以最近 15 秒
    的真实工具网关日志作为兜底活动信号。
    """
    now = datetime.now(timezone.utc)
    recent_tool_cutoff = now - timedelta(seconds=15)
    active_event_cutoff = now - timedelta(seconds=90)
    today = now.date()

    profiles = db.query(AgentProfile).all()
    # 工具活动和模型调用必须分开统计。前者包含本地定时巡检，不产生模型
    # Token 费用；合并成一个“今日调用”会让管理员误判真实模型消费。
    tool_today_rows = (
        db.query(ToolCallLog.agent_code, func.count(ToolCallLog.id).label("cnt"))
        .filter(func.date(ToolCallLog.create_time) == today)
        .group_by(ToolCallLog.agent_code)
        .all()
    )
    tool_today_map = {r[0]: int(r[1] or 0) for r in tool_today_rows}
    component_total = (
        func.coalesce(AiCallLog.prompt_tokens, 0)
        + func.coalesce(AiCallLog.completion_tokens, 0)
    )
    logged_total = func.coalesce(AiCallLog.total_tokens, 0)
    # 某些 Responses/失败日志没有 total_tokens,或 total_tokens 比组件值小。
    # 采用预算服务相同的 max(total, prompt+completion) 口径,避免低报费用。
    effective_total = case(
        (logged_total >= component_total, logged_total),
        else_=component_total,
    )
    ai_label_rows = (
        db.query(
            AiCallLog.agent_label,
            AiCallLog.model_name,
            func.count(AiCallLog.id).label("cnt"),
            func.coalesce(func.sum(effective_total), 0).label("tokens"),
        )
        .filter(
            func.date(AiCallLog.create_time) == today,
            or_(AiCallLog.agent_label.isnot(None), AiCallLog.model_name.isnot(None)),
        )
        .group_by(AiCallLog.agent_label, AiCallLog.model_name)
        .all()
    )
    profiles_by_code = {profile.code: profile for profile in profiles}
    model_today_map: dict[str, int] = {}
    model_token_map: dict[str, int] = {}
    unattributed_model_calls = 0
    unattributed_model_tokens = 0
    for label, model_name, count, tokens in ai_label_rows:
        codes = _activity_agent_codes(label, model_name, set(profiles_by_code))
        if not codes:
            unattributed_model_calls += int(count or 0)
            unattributed_model_tokens += int(tokens or 0)
        for code in codes:
            model_today_map[code] = model_today_map.get(code, 0) + int(count or 0)
            model_token_map[code] = model_token_map.get(code, 0) + int(tokens or 0)
    event_status_map = {
        "dispatch": "thinking",
        "thinking": "thinking",
        "progress": "working",
        "complete": "idle",
        "failed": "error",
        "clarify": "blocked",
    }
    latest_events: dict[str, tuple[datetime, str, str]] = {}
    for event in AgentEventBus.instance().recent(limit=500):
        if event.agent not in profiles_by_code or event.type is None:
            continue
        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        mapped_status = event_status_map.get(event_type)
        if not mapped_status:
            continue
        try:
            event_time = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        else:
            event_time = event_time.astimezone(timezone.utc)
        previous = latest_events.get(event.agent)
        if previous is None or event_time >= previous[0]:
            latest_events[event.agent] = (event_time, mapped_status, event.message or "")

    # 工具网关日志是跨进程的事实来源；在没有事件总线事件时仅用极短窗口兜底。
    recent_tool_rows = (
        db.query(ToolCallLog)
        .filter(ToolCallLog.create_time >= recent_tool_cutoff)
        .order_by(ToolCallLog.create_time.desc(), ToolCallLog.id.desc())
        .all()
    )
    latest_tools: dict[str, ToolCallLog] = {}
    for row in recent_tool_rows:
        latest_tools.setdefault(row.agent_code, row)

    result = []
    for p in profiles:
        tool_calls = tool_today_map.get(p.code, 0)
        model_calls = model_today_map.get(p.code, 0)
        model_tokens = model_token_map.get(p.code, 0)
        calls = tool_calls + model_calls
        db_status = getattr(p, "status", "idle") or "idle"
        latest_event = latest_events.get(p.code)
        latest_tool = latest_tools.get(p.code)
        status = "disabled" if not getattr(p, "is_enabled", 1) else "idle"
        purpose = ""
        last_seen_at = None
        activity_source = "none"
        if db_status == "error":
            status = "error"
        elif latest_event and latest_event[0] >= active_event_cutoff:
            status = latest_event[1]
            purpose = latest_event[2]
            last_seen_at = latest_event[0].isoformat()
            activity_source = "event_bus"
        elif latest_tool and latest_tool.create_time:
            tool_time = latest_tool.create_time
            if tool_time.tzinfo is None:
                tool_time = tool_time.replace(tzinfo=timezone.utc)
            if tool_time >= recent_tool_cutoff:
                status = "error" if latest_tool.status == "failed" else "working"
                purpose = latest_tool.action or latest_tool.tool_code or ""
                last_seen_at = tool_time.isoformat()
                activity_source = "tool_log"

        # 用途:实时事件/工具没有用途时，回退到今日最常见动作
        purpose = ""
        if latest_event and latest_event[0] >= active_event_cutoff and latest_event[2]:
            purpose = latest_event[2]
        elif latest_tool and latest_tool.action:
            purpose = latest_tool.action
        elif calls:
            top_action = (
                db.query(ToolCallLog.action, func.count(ToolCallLog.id).label("c"))
                .filter(func.date(ToolCallLog.create_time) == today, ToolCallLog.agent_code == p.code)
                .group_by(ToolCallLog.action)
                .order_by(func.count(ToolCallLog.id).desc())
                .first()
            )
            purpose = top_action[0] if top_action else ""
        result.append({
            "agent_code": p.code,
            "name": getattr(p, "name", p.code),
            "status": status,
            "calls_today": calls,
            "model_calls_today": model_calls,
            "model_tokens_today": model_tokens,
            "tool_calls_today": tool_calls,
            "purpose": purpose,
            "is_enabled": getattr(p, "is_enabled", 1),
            "last_seen_at": last_seen_at,
            "activity_source": activity_source,
        })
    if unattributed_model_calls:
        result.append({
            "agent_code": "unattributed_model",
            "name": "未归因模型调用",
            "status": "idle",
            "calls_today": unattributed_model_calls,
            "model_calls_today": unattributed_model_calls,
            "model_tokens_today": unattributed_model_tokens,
            "tool_calls_today": 0,
            "purpose": "历史日志未记录 Agent 标识",
            "is_enabled": 1,
            "last_seen_at": None,
            "activity_source": "none",
        })
    return sorted(result, key=lambda row: (row["status"] not in {"working", "thinking", "blocked"}, row["name"]))


def _login_geo(db: Session, days: int = 30) -> list[dict]:
    """近 N 天成功登录 IP 的地理分布(聚合打点)。

    失败登录已经在安全态势中单独统计；来源地图只表达实际建立登录会话的
    来源，避免爆破流量把地图误显示成业务用户分布。

    Returns:
        list[dict]: [{ip, country, city, latitude, longitude, count}]
    """
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(AuditLog.ip, func.count(AuditLog.id).label("cnt"))
        .filter(
            AuditLog.action == "login",
            AuditLog.status == "success",
            AuditLog.create_time >= since,
            AuditLog.ip.isnot(None),
            AuditLog.ip != "",
        )
        .group_by(AuditLog.ip)
        .order_by(func.count(AuditLog.id).desc())
        .limit(200)
        .all()
    )
    points = []
    for ip, cnt in rows:
        loc = locate_ip_cached(ip)
        if not loc:
            continue
        points.append({
            "ip": ip,
            "country": loc.get("country"),
            "city": loc.get("city"),
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "count": cnt,
        })
    return points


@router.get("/overview/system", response_model=Resp[dict])
def overview_system(_: User = Depends(require_super_admin)):
    """服务器运行状态(CPU/内存/磁盘/负载/运行时长)。"""
    return Resp(data=system_service.system_status())


@router.get("/overview/security", response_model=Resp[dict])
def overview_security(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """安全态势(登录爆破/恶意扫描/来源TOP,基于应用日志)。"""
    return Resp(data=security_posture(db))


@router.get("/overview/geo", response_model=Resp[list])
def overview_geo(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """登录来源 IP 的地理分布(世界地图打点)。"""
    return Resp(data=_login_geo(db))


@router.get("/overview/agents-activity", response_model=Resp[list])
def overview_agents_activity(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """各 Agent 活跃状态(状态/并发用户/今日调用/用途)。"""
    return Resp(data=_agent_activity(db))
