"""Agent 治理调度服务。

本阶段持久化任务定义与运行记录，并预留 APScheduler 接入点。

v3.0 AgentSkill 升级新增:
- Skill 调度任务类型: skill_evolution / skill_proactive
- ensure_skill_jobs(): 注册 per-Agent 的 Skill 定时任务(每日 03:00 跑 evolution,
  每小时跑 proactive_check)
- _execute_skill_evolution / _execute_skill_proactive: 实际执行 Skill 的本地函数
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.agent_governance import AgentJob, AgentJobRun
from app.models.user import User
from app.services import agent_knowledge_service, agent_memory_service

_DEFAULT_JOBS = (
    ("daily_agent_knowledge_crawl", "crawl", "knowledge_distiller", "daily@02:00"),
    ("daily_agent_reflection", "reflection", "reflection", "daily@03:00"),
    ("daily_agent_evolution", "evolution", "evolution", "daily@04:00"),
    ("ops_health_check", "ops_health_check", "operations", "interval@5m"),
    ("security_monitor", "security_monitor", "operations", "interval@5m"),
    ("db_backup", "db_backup", "operations", "daily@02:30"),
    ("sandbox_heartbeat", "sandbox_heartbeat", "monitor", "interval@30s"),
    ("archive_empty_sessions", "archive_empty_sessions", "monitor", "interval@30m"),
)

# These tasks either reach externally configured sources or inspect the host.
# Unattended scheduler execution remains allowed, but interactive access is
# reserved for the unique super administrator.
SUPER_ADMIN_JOB_TYPES = frozenset({"crawl", "ops_health_check", "security_monitor", "db_backup"})

# v3.0 AgentSkill: per-Agent 进化任务(每日 03:00 跑 self_improve action=evolve)
# 14 个 Agent 各一条,与原 daily_agent_evolution 共存(后者保持兼容)
_SKILL_EVOLUTION_AGENTS = (
    "code_reviewer", "security_sentinel", "language_detector",
    "project_analyzer", "code_file_manager", "dashboard",
    "rule_manager", "reporter", "ai_prompt", "project_manager",
    "review_orchestrator", "chat_assistant", "orchestrator", "evolution",
)

# v3.0 AgentSkill: per-Agent 主动监测任务(每小时跑 proactive action=check_proactive)
# 同样 14 个 Agent,与 evolution 任务用不同 cron 表达式
_SKILL_PROACTIVE_AGENTS = _SKILL_EVOLUTION_AGENTS


def _utcnow() -> datetime:
    """获取 UTC 当前时间。

    Returns:
        datetime: 当前 UTC 时间。
    """
    return datetime.now(timezone.utc)


def requires_super_admin(job_type: str) -> bool:
    """Return whether a scheduler task is restricted to the unique super administrator."""

    return job_type in SUPER_ADMIN_JOB_TYPES


def can_access_restricted_jobs(db: Session, actor: Optional[User]) -> bool:
    """Return whether ``actor`` may access restricted scheduler tasks."""

    if actor is None:
        return False
    from app.services.rbac_service import is_super_admin_user

    return is_super_admin_user(db, actor.id)


def ensure_default_jobs(db: Session) -> list[AgentJob]:
    """确保默认治理调度任务存在。

    Args:
        db: 数据库会话。

    Returns:
        list[AgentJob]: 当前任务列表。
    """
    for code, job_type, agent_code, schedule in _DEFAULT_JOBS:
        exists = db.query(AgentJob).filter(AgentJob.job_code == code).first()
        if exists:
            continue
        db.add(AgentJob(
            job_code=code,
            job_type=job_type,
            agent_code=agent_code,
            schedule=schedule,
            status="enabled",
            config_json=json.dumps({"scheduler": "apscheduler-ready"}, ensure_ascii=False),
        ))
    db.commit()
    # v3.0 AgentSkill: 同步注册 per-Agent 的 Skill 调度任务
    try:
        ensure_skill_jobs(db)
    except Exception as exc:  # noqa: BLE001 - Skill 调度注册失败不阻断主流程
        from loguru import logger
        logger.warning(f"[scheduler_service] ensure_skill_jobs 失败(不影响主流程): {exc}")
    return list_jobs(db)


def ensure_skill_jobs(db: Session) -> list[AgentJob]:
    """注册 per-Agent 的 Skill 定时任务(v3.0 AgentSkill 升级)

    为每个 Agent 注册两条 cron 任务:
    1. 每日 03:00 跑 {agent}.self_improve action=evolve (自进化)
    2. 每小时跑 {agent}.proactive action=check_proactive (主动监测)

    Args:
        db: 数据库会话。

    Returns:
        list[AgentJob]: 当前已注册的 Skill 调度任务列表。
    """
    added: list[AgentJob] = []
    # 每日 03:00 跑 evolution
    for agent_code in _SKILL_EVOLUTION_AGENTS:
        job_code = f"daily_skill_evolution_{agent_code}"
        exists = db.query(AgentJob).filter(AgentJob.job_code == job_code).first()
        if exists:
            continue
        job = AgentJob(
            job_code=job_code,
            job_type="skill_evolution",
            agent_code=agent_code,
            schedule="daily@03:00",
            status="enabled",
            config_json=json.dumps({
                "scheduler": "apscheduler-ready",
                "skill_name": f"{agent_code}.self_improve",
                "action": "evolve",
                "trigger_type": "scheduled",
            }, ensure_ascii=False),
        )
        db.add(job)
        added.append(job)

    # 每小时跑 proactive(用 cron 表达式 hourly@*:00 表示)
    for agent_code in _SKILL_PROACTIVE_AGENTS:
        job_code = f"hourly_skill_proactive_{agent_code}"
        exists = db.query(AgentJob).filter(AgentJob.job_code == job_code).first()
        if exists:
            continue
        job = AgentJob(
            job_code=job_code,
            job_type="skill_proactive",
            agent_code=agent_code,
            schedule="hourly@*:00",
            status="enabled",
            config_json=json.dumps({
                "scheduler": "apscheduler-ready",
                "skill_name": f"{agent_code}.proactive",
                "action": "check_proactive",
                "trigger_type": "scheduled",
            }, ensure_ascii=False),
        )
        db.add(job)
        added.append(job)

    if added:
        db.commit()
        for j in added:
            db.refresh(j)
    return added


def list_jobs(db: Session) -> list[AgentJob]:
    """查询治理调度任务。

    Args:
        db: 数据库会话。

    Returns:
        list[AgentJob]: 任务列表。
    """
    return db.query(AgentJob).order_by(AgentJob.id.asc()).all()


def update_job(db: Session, job_id: int, payload: dict, *, actor: Optional[User] = None) -> AgentJob:
    """更新治理调度任务配置。

    Args:
        db: 数据库会话。
        job_id: 任务 ID。
        payload: 更新字段。

    Returns:
        AgentJob: 更新后的任务。

    Raises:
        NotFoundError: 任务不存在。
    """
    job = db.get(AgentJob, job_id)
    if not job:
        raise NotFoundError("调度任务不存在", code=40400)
    if requires_super_admin(job.job_type) and not can_access_restricted_jobs(db, actor):
        raise ForbiddenError("仅超级管理员 admin 可修改受限调度任务", code=40322)
    if payload.get("schedule") is not None:
        job.schedule = payload["schedule"]
    if payload.get("status") is not None:
        job.status = payload["status"]
    if payload.get("config_json") is not None:
        job.config_json = json.dumps(payload["config_json"], ensure_ascii=False)
    db.commit()
    db.refresh(job)
    return job


def run_job(
    db: Session,
    job_id: int,
    *,
    actor: Optional[User] = None,
    system_scheduled: bool = False,
) -> AgentJobRun:
    """手动运行一个治理调度任务。

    Args:
        db: 数据库会话。
        job_id: 任务 ID。

    Returns:
        AgentJobRun: 任务运行记录。

    Raises:
        NotFoundError: 任务不存在。
    """
    job = db.get(AgentJob, job_id)
    if not job:
        raise NotFoundError("调度任务不存在", code=40400)
    if (
        requires_super_admin(job.job_type)
        and not system_scheduled
        and not can_access_restricted_jobs(db, actor)
    ):
        raise ForbiddenError("仅超级管理员 admin 可手动运行受限调度任务", code=40322)
    run = AgentJobRun(job_id=job.id, status="running", started_at=_utcnow())
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        result = _execute_job(db, job)
        run.status = "failed" if job.job_type == "ops_health_check" and result.get("success") is False else "success"
        run.result_json = json.dumps(result, ensure_ascii=False, default=str)
        if run.status == "failed":
            run.error = "AI 自动运维巡检检测到不健康状态"
        run.finished_at = _utcnow()
        job.last_run_at = run.finished_at
    except Exception as exc:  # noqa: BLE001 - 调度失败需落库
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = _utcnow()
    db.commit()
    db.refresh(run)
    return run


def _execute_job(db: Session, job: AgentJob) -> dict:
    """执行调度任务的本地动作。

    Args:
        db: 数据库会话。
        job: 调度任务。

    Returns:
        dict: 执行结果摘要。
    """
    if job.job_type == "crawl":
        return agent_knowledge_service.crawl_enabled_sources(db, agent_code=job.agent_code or "")
    if job.job_type == "reflection":
        item = agent_memory_service.add_memory(
            db,
            agent_code=job.agent_code or "reflection",
            title="每日反思任务",
            content="调度触发自我反思，已将本次反思沉淀到 Agent 独立记忆。",
            memory_type="reflection",
            weight=0.5,
            source_ref=f"job:{job.id}",
        )
        return {"memory_id": item.id}
    if job.job_type == "evolution":
        return {"message": "自进化任务已记录，详细执行由 evolution_service 承接"}
    if job.job_type == "ops_health_check":
        return _execute_ops_health_check(db, job)
    if job.job_type == "security_monitor":
        return _execute_security_monitor(db, job)
    if job.job_type == "db_backup":
        return _execute_db_backup(db, job)
    if job.job_type == "sandbox_heartbeat":
        from app.services import sandbox_service

        return sandbox_service.heartbeat_and_recover_sandboxes(db)
    if job.job_type == "archive_empty_sessions":
        from app.services import agent_mesh_service

        return agent_mesh_service.archive_empty_conversations(db)
    # v3.0 AgentSkill: Skill 调度任务
    if job.job_type == "skill_evolution":
        return _execute_skill_evolution(db, job)
    if job.job_type == "skill_proactive":
        return _execute_skill_proactive(db, job)
    return {"message": f"未知任务类型 {job.job_type}，已跳过"}


def _execute_skill_evolution(db: Session, job: AgentJob) -> Dict[str, Any]:
    """执行 Skill 自进化调度任务(v3.0 AgentSkill 升级)

    通过 Orchestrator.invoke_skill 调用 {agent_name}.self_improve action=evolve,
    trigger_type="scheduled" 确保 agent_skill_record 正确归类。

    Args:
        db: 数据库会话。
        job: 调度任务(含 agent_code 与 config_json)。

    Returns:
        dict: 执行结果摘要, 含 success/effect/duration_ms/record_id 字段。
    """
    from loguru import logger

    from app.agents.base import AgentContext
    from app.agents.orchestrator import Orchestrator

    agent_name = job.agent_code or "evolution"
    skill_name = f"{agent_name}.self_improve"
    config = _parse_job_config(job.config_json)
    action = config.get("action", "evolve")

    logger.info(
        f"[scheduler_service] 触发定时自进化: agent={agent_name} "
        f"skill={skill_name} action={action}"
    )

    orch = Orchestrator(register=False)
    orch._db = db
    ctx = AgentContext(extra={"job_id": job.id, "job_code": job.job_code})
    try:
        result = orch.invoke_skill(
            agent_name=agent_name,
            skill_name=skill_name,
            params={"action": action},
            ctx=ctx,
            trigger_type="scheduled",
            trigger_source=f"scheduler:cron:{job.schedule}",
        )
    except Exception as exc:  # noqa: BLE001 - 调度异常需转成结构化返回
        logger.warning(
            f"[scheduler_service] 定时自进化异常: agent={agent_name} error={exc}"
        )
        return {
            "success": False,
            "error": str(exc),
            "agent_name": agent_name,
            "skill_name": skill_name,
        }

    data = result.data if isinstance(result.data, dict) else {}
    return {
        "success": result.success,
        "effect": data.get("effect", "?"),
        "duration_ms": data.get("duration_ms", result.duration_ms),
        "record_id": data.get("record_id"),
        "agent_name": agent_name,
        "skill_name": skill_name,
    }


def _execute_skill_proactive(db: Session, job: AgentJob) -> Dict[str, Any]:
    """执行 Skill 主动监测调度任务(v3.0 AgentSkill 升级)

    通过 Orchestrator.invoke_skill 调用 {agent_name}.proactive action=check_proactive,
    trigger_type="scheduled" 确保 agent_skill_record 正确归类。

    Args:
        db: 数据库会话。
        job: 调度任务(含 agent_code 与 config_json)。

    Returns:
        dict: 执行结果摘要, 含 success/effect/duration_ms/record_id 字段。
    """
    from loguru import logger

    from app.agents.base import AgentContext
    from app.agents.orchestrator import Orchestrator

    agent_name = job.agent_code or "orchestrator"
    skill_name = f"{agent_name}.proactive"
    config = _parse_job_config(job.config_json)
    action = config.get("action", "check_proactive")

    logger.info(
        f"[scheduler_service] 触发定时主动监测: agent={agent_name} "
        f"skill={skill_name} action={action}"
    )

    orch = Orchestrator(register=False)
    orch._db = db
    ctx = AgentContext(extra={"job_id": job.id, "job_code": job.job_code})
    try:
        result = orch.invoke_skill(
            agent_name=agent_name,
            skill_name=skill_name,
            params={"action_type": action},
            ctx=ctx,
            trigger_type="scheduled",
            trigger_source=f"scheduler:cron:{job.schedule}",
        )
    except Exception as exc:  # noqa: BLE001 - 调度异常需转成结构化返回
        logger.warning(
            f"[scheduler_service] 定时主动监测异常: agent={agent_name} error={exc}"
        )
        return {
            "success": False,
            "error": str(exc),
            "agent_name": agent_name,
            "skill_name": skill_name,
        }

    data = result.data if isinstance(result.data, dict) else {}
    return {
        "success": result.success,
        "effect": data.get("effect", "?"),
        "duration_ms": data.get("duration_ms", result.duration_ms),
        "record_id": data.get("record_id"),
        "agent_name": agent_name,
        "skill_name": skill_name,
    }


def _parse_job_config(config_json: Optional[str]) -> dict:
    """解析 AgentJob.config_json 字段

    Args:
        config_json: JSON 字符串(可能为 None 或非法 JSON)

    Returns:
        dict: 解析后的配置字典(失败时返回空 dict)
    """
    if not config_json:
        return {}
    try:
        result = json.loads(config_json)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _collect_application_health(db: Session, current_job_id: int) -> Dict[str, Any]:
    """从生产数据库汇总模型、任务、Agent 和自进化运行态。"""
    from app.models.agent_governance import AgentProfile
    from app.models.ai_call_log import AiCallLog

    now = _utcnow()
    model_cutoff = now - timedelta(hours=6)
    run_cutoff = now - timedelta(hours=6)
    evolution_cutoff = now - timedelta(hours=26)
    stuck_cutoff = now - timedelta(minutes=30)

    latest_model = (
        db.query(AiCallLog)
        .filter(AiCallLog.create_time >= model_cutoff)
        .order_by(AiCallLog.create_time.desc(), AiCallLog.id.desc())
        .first()
    )
    model_state = "unknown"
    if latest_model:
        model_state = "healthy" if latest_model.status == "success" else "error"

    stuck_runs = (
        db.query(AgentJobRun)
        .filter(
            AgentJobRun.status == "running",
            AgentJobRun.started_at < stuck_cutoff,
            AgentJobRun.job_id != current_job_id,
        )
        .count()
    )
    enabled_jobs = db.query(AgentJob).filter(AgentJob.status == "enabled").all()
    failed_jobs: list[str] = []
    failed_evolution_jobs: list[str] = []
    for scheduled_job in enabled_jobs:
        if scheduled_job.id == current_job_id:
            continue
        cutoff = evolution_cutoff if scheduled_job.job_type in {"evolution", "skill_evolution"} else run_cutoff
        latest_run = (
            db.query(AgentJobRun)
            .filter(AgentJobRun.job_id == scheduled_job.id, AgentJobRun.started_at >= cutoff)
            .order_by(AgentJobRun.started_at.desc(), AgentJobRun.id.desc())
            .first()
        )
        if latest_run and latest_run.status == "failed":
            failed_jobs.append(scheduled_job.job_code)
            if scheduled_job.job_type in {"evolution", "skill_evolution"}:
                failed_evolution_jobs.append(scheduled_job.job_code)

    error_agents = [
        code for (code,) in db.query(AgentProfile.code).filter(
            AgentProfile.is_enabled == 1,
            AgentProfile.status == "error",
        ).all()
    ]
    ok = model_state != "error" and stuck_runs == 0 and not failed_jobs and not error_agents
    return {
        "ok": ok,
        "model_api": {
            "state": model_state,
            "latest_call_id": latest_model.id if latest_model else None,
            "latest_agent": latest_model.agent_label if latest_model else None,
            "latest_model": latest_model.model_name if latest_model else None,
            "latest_status": latest_model.status if latest_model else None,
            "latest_at": latest_model.create_time.isoformat() if latest_model else None,
        },
        "task_queue": {"stuck_runs": stuck_runs, "failed_jobs": failed_jobs},
        "agents": {"error_count": len(error_agents), "error_agents": error_agents},
        "evolution": {
            "enabled_jobs": sum(1 for item in enabled_jobs if item.job_type in {"evolution", "skill_evolution"}),
            "failed_jobs": failed_evolution_jobs,
        },
    }


def _execute_security_monitor(db: Session, job: AgentJob) -> Dict[str, Any]:
    """执行安全监控巡检调度任务。

    安全监控总开关关闭时直接返回提示；否则调用
    ``security_monitor_service.run_security_monitor`` 拉取只读安全事件并按规则
    生成告警（单动作失败不中断整体）。

    Args:
        db: 数据库会话。
        job: 调度任务。

    Returns:
        Dict[str, Any]: 执行摘要（success/created_alerts/errors）。
    """
    from app.services import security_monitor_service

    if not settings.security_monitor_enabled:
        return {"message": "security monitor disabled"}
    result = security_monitor_service.run_security_monitor(db, job=job)
    return {
        "success": bool(result.get("success")),
        "created_alerts": result.get("created_alerts") or [],
        "errors": result.get("errors") or [],
        "job_id": job.id,
    }


def _execute_db_backup(db: Session, job: AgentJob) -> Dict[str, Any]:
    """执行生产数据库自动备份调度任务。

    受 ``backup_schedule_enabled`` 显式开关闸门（默认关闭，须最高管理员在
    .env 开启）。开启后调用 ``backup_database`` 运维动作做一次一致性压缩备份；
    备份目录的过期轮换清理由 backup.sh 按 ``BACKUP_RETENTION_DAYS`` 完成。
    失败时写入 critical 告警并 SSE 通知最高管理员，便于及时处置。
    """
    from app.agents.operations_agent import OperationsAgent
    from app.services import observability_service

    if not settings.backup_schedule_enabled:
        return {"message": "db backup disabled", "success": True, "skipped": True}

    agent = OperationsAgent()
    result = agent.execute_action(db, None, action="backup_database", source="scheduler")
    data = result.data if isinstance(result.data, dict) else {}
    success = bool(result.success) and data.get("status") == "success"
    summary: Dict[str, Any] = {
        "success": success,
        "execution_id": data.get("id"),
        "status": data.get("status"),
        "job_id": job.id,
    }
    if not success:
        error_text = str(result.error or data.get("error") or "备份失败")
        summary["error"] = error_text
        try:
            admin = db.query(User).filter(
                User.username == "admin", User.role == "super_admin"
            ).first()
            alert = observability_service.create_alert(
                db,
                alert_type="ops.backup",
                severity="critical",
                title="生产数据库自动备份失败",
                detail={"error": error_text[:500], "job_id": job.id},
                category="backup",
                source="db_backup",
                user_id=admin.id if admin else None,
                fingerprint="backup:auto_failed",
            )
            if admin is not None:
                from app.agents.event_bus import emit_event
                from app.agents.events import AgentEventType, new_trace_id
                emit_event(
                    AgentEventType.ADMIN_ALERT,
                    agent="operations",
                    trace_id=new_trace_id(),
                    parent="manager",
                    message="生产数据库自动备份失败",
                    payload={
                        "alert_id": alert.id,
                        "severity": "critical",
                        "category": "backup",
                        "title": "生产数据库自动备份失败",
                        "suggestion": "检查 MySQL 内存/磁盘空间与备份目录可写性，必要时手动执行 backup_database",
                    },
                    user_id=admin.id,
                )
        except Exception:  # noqa: BLE001 - 告警失败不应遮蔽原始备份错误
            pass
    return summary


def _execute_ops_health_check(db: Session, job: AgentJob) -> Dict[str, Any]:
    """真实执行宿主机巡检，并把异常转成告警与可审批的建议动作。"""
    from app.agents.operations_agent import OperationsAgent
    from app.models.admin_chat import AdminChatMessage
    from app.models.user import User
    from app.services import audit_service, observability_service

    agent = OperationsAgent()
    result = agent.execute_action(db, None, action="status", source="scheduler")
    data = result.data if isinstance(result.data, dict) else {}
    executor_payload = data.get("result") if isinstance(data.get("result"), dict) else {}
    payload = executor_payload.get("result") if isinstance(executor_payload.get("result"), dict) else executor_payload
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    certificate_result = agent.execute_action(db, None, action="certificate_status", source="scheduler")
    certificate_data = certificate_result.data if isinstance(certificate_result.data, dict) else {}
    certificate_executor = certificate_data.get("result") if isinstance(certificate_data.get("result"), dict) else {}
    certificate_payload = (
        certificate_executor.get("result")
        if isinstance(certificate_executor.get("result"), dict)
        else certificate_executor
    )
    certificate_ok = certificate_result.success and bool(certificate_payload.get("valid_for_30_days"))
    application = _collect_application_health(db, job.id)
    healthy = (
        str(checks.get("status") or "error") == "ok"
        and result.success
        and certificate_ok
        and bool(application.get("ok"))
    )
    summary: Dict[str, Any] = {
        "success": healthy,
        "execution_id": data.get("id"),
        "certificate_execution_id": certificate_data.get("id"),
        "checks": checks,
        "certificate": {
            "ok": certificate_ok,
            "valid_for_30_days": certificate_payload.get("valid_for_30_days"),
        },
        "application": application,
    }
    legacy_cards = db.query(AdminChatMessage).filter(
        AdminChatMessage.agent_code == "operations",
        AdminChatMessage.message_type.in_(["confirm", "danger_confirm"]),
        AdminChatMessage.action_status == "pending",
    ).all()
    for message in legacy_cards:
        try:
            message_payload = json.loads(message.payload_json or "{}")
        except (TypeError, ValueError):
            message_payload = {}
        if not isinstance(message_payload, dict):
            message_payload = {}
        message_payload["status"] = "cancelled"
        message_payload["resolution"] = "旧确认协议已停用，请在 Responses 全服管理 Agent 中重新发起"
        message.payload_json = json.dumps(message_payload, ensure_ascii=False, default=str)
        message.action_status = "cancelled"
    if legacy_cards:
        db.commit()
        summary["cancelled_legacy_confirmation_ids"] = [message.id for message in legacy_cards]

    backup = checks.get("checks", {}).get("backup", {}) if isinstance(checks.get("checks"), dict) else {}
    if not bool(backup.get("ok")):
        summary["backup_remediation"] = {
            "action": "backup_database",
            "params": {},
            "status": "approval_required",
        }

    alert_title = "AI 自动运维巡检异常"
    existing_alert = next(
        (
            alert
            for alert in observability_service.list_alerts(db, status="open", limit=50)
            if alert.title == alert_title
        ),
        None,
    )
    admin = (
        db.query(User)
        .filter(User.role.in_(["admin", "super_admin"]), User.status == 1)
        .order_by(User.id.asc())
        .first()
    )
    if healthy:
        if existing_alert:
            observability_service.resolve_alert(
                db,
                existing_alert.id,
                admin.id if admin else 0,
                note=json.dumps({"resolution": "定时巡检已恢复健康", "job_id": job.id}, ensure_ascii=False),
            )
            audit_service.log(
                db,
                None,
                "ops.alert.auto_resolve",
                target_type="agent_alert",
                target_id=str(existing_alert.id),
                detail="AI 自动运维巡检恢复健康，系统自动闭环告警",
            )
            summary["resolved_alert_id"] = existing_alert.id
        return summary

    failed_services = []
    containers = checks.get("checks", {}).get("containers", {}) if isinstance(checks.get("checks"), dict) else {}
    services = containers.get("services", {}) if isinstance(containers, dict) else {}
    if isinstance(services, dict):
        failed_services = [name for name, status in services.items() if status not in {"healthy", "running"}]

    alert_detail = {
        "job_id": job.id,
        "checks": checks,
        "certificate": summary["certificate"],
        "application": application,
        "failed_services": failed_services,
    }
    if not existing_alert:
        existing_alert = observability_service.create_alert(
            db,
            alert_type="ops_health",
            severity="high",
            title=alert_title,
            detail=alert_detail,
        )
    else:
        existing_alert.severity = "high"
        existing_alert.detail_json = json.dumps(alert_detail, ensure_ascii=False, default=str)
        db.commit()

    if settings.ops_health_diagnosis_enabled:
        diagnosis = agent.diagnose(db, None, alert_detail, f"trc_ops_sched_{job.id}")
        summary["diagnosis"] = str(diagnosis.data or diagnosis.error or "").strip()[:4000]
    else:
        # 巡检仍然生成告警和确定性处置建议,但不在后台隐式消耗模型额度。
        summary["diagnosis_skipped"] = True
        summary["diagnosis"] = "后台成本保护已跳过模型诊断;请管理员在小菱中明确发起只读核验。"

    remediation: Optional[Dict[str, Any]] = None
    if failed_services:
        service = failed_services[0]
        remediation = {
            "action": "restart_service",
            "params": {"service": service},
            "key": service,
            "operation": f"巡检发现 {service} 不健康，重启生产服务",
            "impact": f"服务 {service} 将短暂不可用；执行器会等待健康检查恢复。",
            "consequence": "如果服务无法恢复，需要继续查看巡检诊断和回滚记录。",
        }
    elif not certificate_ok:
        remediation = {
            "action": "renew_certificate",
            "params": {},
            "key": "renew_certificate",
            "operation": "巡检发现 TLS 证书不足 30 天，续期并重载 Nginx",
            "impact": "将调用 Let's Encrypt 续期流程并重载入口服务。",
            "consequence": "续期失败不会覆盖现有证书，但需要人工排查 DNS 或端口。",
        }

    if remediation:
        summary["recommended_remediation"] = remediation
    return summary
