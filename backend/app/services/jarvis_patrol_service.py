"""管理小菱 JARVIS 只读运维巡逻。

周期性地收集平台异常证据(未处理高危告警、卡死/不健康沙箱、近期失败运行)。
为避免后台巡逻消耗用户额度,默认只保留证据采集;只有显式开启自动派发时,
才把简报投递给在线管理会话。写入/运维动作仍由小菱走既有审批链。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.agent_capability import SandboxEnvironment
from app.models.agent_governance import AgentAlert
from app.models.agent_mesh import AgentMeshConversation
from app.models.agent_response_run import AgentResponseRun
from app.models.user import User

_scheduler = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _brief_evidence(db) -> tuple[list[dict[str, Any]], str]:
    """收集异常证据并生成稳定指纹;无异常时返回空列表与空指纹。"""
    now = _now()
    alerts = (
        db.query(AgentAlert)
        .filter(AgentAlert.status == "open", AgentAlert.severity.in_(("high", "critical")))
        .order_by(AgentAlert.id.desc())
        .limit(20)
        .all()
    )
    failed_runs = int(
        db.query(AgentResponseRun.id)
        .filter(AgentResponseRun.status == "failed", AgentResponseRun.update_time >= now - timedelta(hours=1))
        .count()
    )
    # 卡死沙箱:超过 2 小时仍停留在 queued/running 的环境视为僵尸
    # 列以 naive UTC 存储(DateTime 无 tz),比较基准也用 naive UTC 保持一致
    stuck_cutoff = datetime.utcnow() - timedelta(hours=2)
    stuck_sandboxes = (
        db.query(SandboxEnvironment)
        .filter(
            SandboxEnvironment.status.in_(("queued", "running")),
            SandboxEnvironment.create_time <= stuck_cutoff,
        )
        .order_by(SandboxEnvironment.id.desc())
        .limit(10)
        .all()
    )
    items: list[dict[str, Any]] = []
    for row in alerts:
        items.append({
            "id": int(row.id),
            "alert_type": row.alert_type,
            "severity": row.severity,
            "category": row.category or "",
            "source": row.source or "",
            "title": row.title[:200],
            "detail": (row.detail_json or "")[:1500],
            "created_at": row.create_time.isoformat() if row.create_time else "",
        })
    if failed_runs:
        # 失败次数计入指纹,次数变化时简报会刷新,而不是被旧幂等键吞掉。
        items.append({
            "id": f"failed_runs_1h:{failed_runs}",
            "alert_type": "recent_failed_runs",
            "severity": "warning",
            "category": "run_health",
            "source": "jarvis_patrol",
            "title": f"近 1 小时有 {failed_runs} 次小菱运行失败",
            "detail": "",
            "created_at": now.isoformat(),
        })
    for box in stuck_sandboxes:
        # DateTime 列以 naive UTC 存储,与 naive 基准相减(aware 会抛 TypeError)
        if box.create_time:
            age_hours = round((datetime.utcnow() - box.create_time).total_seconds() / 3600, 1)
        else:
            age_hours = 0
        items.append({
            "id": f"stuck_sandbox:{int(box.id)}",
            "alert_type": "stuck_sandbox",
            "severity": "warning",
            "category": "sandbox_health",
            "source": "jarvis_patrol",
            "title": f"沙箱 {box.public_id} 已 {age_hours} 小时未进入终态(状态 {box.status})",
            "detail": f"project_id={box.project_id} purpose={box.purpose}",
            "created_at": now.isoformat(),
        })
    if not items:
        return [], ""
    fingerprint = hashlib.sha256(
        json.dumps([item["id"] for item in items], sort_keys=True).encode("utf-8")
    ).hexdigest()
    return items, fingerprint


def _online_admin_sessions(db, now: datetime) -> list[AgentMeshConversation]:
    window = now - timedelta(minutes=settings.agent_jarvis_online_window_minutes)
    admin_ids = [
        int(row[0])
        for row in db.query(User.id)
        .filter(User.role.in_(("admin", "super_admin")), User.status == 1)
        .all()
    ]
    if not admin_ids:
        return []
    return (
        db.query(AgentMeshConversation)
        .filter(
            AgentMeshConversation.user_id.in_(admin_ids),
            AgentMeshConversation.surface == "admin",
            AgentMeshConversation.status == "active",
            AgentMeshConversation.last_seen_at >= window,
        )
        .order_by(AgentMeshConversation.id.asc())
        .limit(10)
        .all()
    )


def patrol_once() -> dict[str, int]:
    """执行一次 JARVIS 巡逻;默认只采集证据,不触发模型调用。"""
    if not settings.agent_jarvis_patrol_enabled:
        return {"alerts": 0, "delivered": 0, "skipped": 0}
    db = SessionLocal()
    try:
        from app.services import agent_mesh_service

        cleanup = agent_mesh_service.sweep_blocked_jarvis_messages(db)
        if cleanup["messages"] or cleanup["runs"]:
            logger.info(
                "[jarvis-patrol] cost guard swept messages={} runs={}",
                cleanup["messages"],
                cleanup["runs"],
            )
        items, fingerprint = _brief_evidence(db)
        if not items:
            return {"alerts": 0, "delivered": 0, "skipped": 0}
        sessions = _online_admin_sessions(db, _now())
        if not settings.agent_jarvis_auto_dispatch_enabled:
            # 保留在线会话数量作为可观测统计,但不写入会触发模型运行的 Mesh 消息。
            # 管理员可在小菱中明确发起只读核验,不影响告警和审计记录。
            return {"alerts": len(items), "delivered": 0, "skipped": len(sessions)}
        delivered = 0
        for conversation in sessions:
            try:
                _deliver_brief(
                    db,
                    user_id=int(conversation.user_id),
                    session_key=conversation.session_key,
                    items=items,
                    fingerprint=fingerprint,
                )
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - 单会话投递失败不影响其他管理员
                logger.warning(
                    "[jarvis-patrol] deliver to session={} failed: {}",
                    conversation.session_key,
                    exc,
                )
                db.rollback()
        return {"alerts": len(items), "delivered": delivered, "skipped": 0}
    except Exception as exc:  # noqa: BLE001 - 巡逻异常不能杀死调度器
        logger.warning("[jarvis-patrol] patrol failed: {}", exc)
        db.rollback()
        return {"alerts": 0, "delivered": 0, "skipped": 1}
    finally:
        db.close()


def _deliver_brief(
    db,
    *,
    user_id: int,
    session_key: str,
    items: list[dict[str, Any]],
    fingerprint: str,
) -> None:
    from app.schemas.agent_mesh import AgentMeshMessageIn
    from app.services import agent_mesh_service

    user = db.get(User, int(user_id))
    if user is None:
        return
    message = AgentMeshMessageIn.model_validate({
        "schema_version": "1.0",
        "idempotency_key": (
            f"jarvis:{hashlib.sha256(session_key.encode('utf-8')).hexdigest()[:24]}:{fingerprint}"
        ),
        "trace_id": f"jarvis-{fingerprint[:16]}",
        "correlation_id": fingerprint,
        "causation_id": "",
        "sent_from": "agent:monitor",
        "send_to": f"session:admin:{session_key}",
        "message_type": "status.update",
        "priority": "high",
        "subject": "JARVIS 运维简报",
        "payload": {
            "patrol_kind": "jarvis",
            "evidence": items,
            "recommendations": [
                "先在管理小菱中复核证据,再决定处置;高危写操作必须等待你点击批准",
                "建议依次处理:未读高危告警 → 失败运行/卡死沙箱 → 节点健康 → 权限与审计异常",
                "可用只读工具先核验:系统状态、指标快照、Worker 健康、审批中心清单",
            ],
            # 自动处置手册:让管理小菱知道每类证据的标准动作与安全边界
            "auto_runbook": {
                "high_alert": {
                    "action": "先用只读工具复核告警详情,判断是否真实;真实且涉及写操作的,给出处置方案等管理员批准",
                    "auto_safe": False,
                },
                "recent_failed_runs": {
                    "action": "查询失败运行的错误摘要,归类失败原因(模型超时/参数错误/权限);同因重复失败给出针对性建议",
                    "auto_safe": True,
                },
                "stuck_sandbox": {
                    "action": (
                        "已超 2 小时未终态的沙箱:向管理员建议关闭(close_sandbox 走审批),"
                        "并说明该沙箱归属项目与用途"
                    ),
                    "auto_safe": False,
                },
            },
            "escalation": "同一证据连续 3 轮巡逻仍在(管理员未处理)时,提高 priority 并在简报里点名催办",
        },
        "context": {"run_id": fingerprint},
        "artifacts": [],
        "errors": [],
        "delivery": {"requires_ack": True, "max_attempts": 1},
    })
    agent_mesh_service.send_message(
        db,
        user,
        surface="admin",
        session_key=session_key,
        message=message,
        trusted_source=True,
    )


def start_jarvis_patrol() -> None:
    global _scheduler
    if not settings.agent_jarvis_patrol_enabled:
        logger.info("[jarvis-patrol] disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        patrol_once,
        "interval",
        id="jarvis-patrol",
        seconds=max(60, int(settings.agent_jarvis_patrol_interval_seconds)),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[jarvis-patrol] started")


def stop_jarvis_patrol() -> None:
    global _scheduler
    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        logger.info("[jarvis-patrol] stopped")
