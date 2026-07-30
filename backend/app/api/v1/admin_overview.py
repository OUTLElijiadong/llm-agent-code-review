"""管理员总览大屏聚合 API。

汇总服务器状态、安全态势、登录来源地理分布、Agent 活跃状态,
供管理员总览大屏一次拉取渲染。
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.models.agent_governance import AgentProfile, ToolCallLog
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.common import Resp
from app.services import system_service
from app.services.geoip_service import locate_ip_cached
from app.services.security_posture_service import security_posture

router = APIRouter()


def _agent_activity(db: Session) -> list[dict]:
    """各 Agent 活跃状态:状态/今日调用/主要用途。

    ToolCallLog 无 actor 维度,并发用户数暂不可精确统计,以"近30分钟有调用"
    标记该 Agent 当前是否处于活跃;今日调用量与高频动作反映负载与用途。
    """
    now = datetime.utcnow()
    recent = now - timedelta(minutes=30)
    today = now.date()

    profiles = db.query(AgentProfile).all()
    # 今日各 agent 调用量
    today_rows = (
        db.query(ToolCallLog.agent_code, func.count(ToolCallLog.id).label("cnt"))
        .filter(func.date(ToolCallLog.create_time) == today)
        .group_by(ToolCallLog.agent_code)
        .all()
    )
    today_map = {r[0]: r[1] for r in today_rows}
    # 近30分钟有调用的 agent(近似活跃)
    recent_codes = {
        r[0]
        for r in db.query(ToolCallLog.agent_code)
        .filter(ToolCallLog.create_time >= recent)
        .distinct()
        .all()
    }

    result = []
    for p in profiles:
        calls = today_map.get(p.code, 0)
        db_status = getattr(p, "status", "idle") or "idle"
        # 综合状态:DB 状态优先,其次按近期调用推断活跃
        if db_status in ("working", "error", "disabled"):
            status = db_status
        elif p.code in recent_codes or calls > 0:
            status = "working"
        else:
            status = "idle"
        # 用途:取该 agent 今日最常见的调用动作
        purpose = ""
        if calls:
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
            "purpose": purpose,
            "is_enabled": getattr(p, "is_enabled", 1),
        })
    return result


def _login_geo(db: Session, days: int = 30) -> list[dict]:
    """近 N 天登录 IP 的地理分布(聚合打点)。

    Returns:
        list[dict]: [{ip, country, city, latitude, longitude, count}]
    """
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(AuditLog.ip, func.count(AuditLog.id).label("cnt"))
        .filter(AuditLog.action == "login", AuditLog.create_time >= since, AuditLog.ip.isnot(None), AuditLog.ip != "")
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
def overview_system(_: User = Depends(require_admin)):
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
