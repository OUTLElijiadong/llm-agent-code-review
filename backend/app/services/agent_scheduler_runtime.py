"""Agent 治理后台调度运行时。

将持久化的 Agent 调度任务注册到 APScheduler，并由 FastAPI 生命周期启动和停止。
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import scheduler_service

_scheduler = None


def _parse_daily_schedule(schedule: str) -> Optional[tuple[int, int]]:
    """解析 ``daily@HH:MM`` 格式的每日调度表达式。

    Args:
        schedule: 数据库存储的调度表达式。

    Returns:
        Optional[tuple[int, int]]: 成功时返回小时和分钟，否则返回 None。
    """
    prefix = "daily@"
    if not schedule or not schedule.startswith(prefix):
        return None
    clock = schedule[len(prefix):]
    try:
        hour_text, minute_text = clock.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _run_scheduled_job(job_id: int) -> None:
    """执行一次后台调度任务并记录日志。

    Args:
        job_id: AgentJob 主键。

    Returns:
        None。
    """
    db = SessionLocal()
    try:
        run = scheduler_service.run_job(db, job_id)
        logger.info("[agent-governance-scheduler] job_id={} run_id={} status={}", job_id, run.id, run.status)
    except Exception as exc:  # noqa: BLE001 - 后台任务异常不能杀死调度器
        logger.warning("[agent-governance-scheduler] job_id={} failed: {}", job_id, exc)
    finally:
        db.close()


def start_agent_governance_scheduler() -> None:
    """启动 Agent 治理后台调度器。

    Returns:
        None。
    """
    global _scheduler
    if not settings.agent_governance_scheduler_enabled:
        logger.info("[agent-governance-scheduler] disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as exc:  # noqa: BLE001 - 缺依赖时降级为手动触发
        logger.warning("[agent-governance-scheduler] APScheduler unavailable, manual jobs only: {}", exc)
        return

    db = SessionLocal()
    try:
        jobs = scheduler_service.ensure_default_jobs(db)
    except Exception as exc:  # noqa: BLE001 - 迁移未完成时不阻断主应用
        logger.warning("[agent-governance-scheduler] default jobs unavailable, scheduler skipped: {}", exc)
        return
    finally:
        db.close()

    scheduler = BackgroundScheduler()
    registered = 0
    for job in jobs:
        if job.status != "enabled":
            continue
        parsed = _parse_daily_schedule(job.schedule)
        if not parsed:
            logger.warning(
                "[agent-governance-scheduler] unsupported schedule job_code={} schedule={}",
                job.job_code,
                job.schedule,
            )
            continue
        hour, minute = parsed
        scheduler.add_job(
            _run_scheduled_job,
            "cron",
            id=f"agent-governance-{job.id}",
            args=[job.id],
            hour=hour,
            minute=minute,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered += 1
    scheduler.start()
    _scheduler = scheduler
    logger.info("[agent-governance-scheduler] started with {} daily jobs", registered)


def stop_agent_governance_scheduler() -> None:
    """停止 Agent 治理后台调度器。

    Returns:
        None。
    """
    global _scheduler
    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
            logger.info("[agent-governance-scheduler] stopped")
    finally:
        _scheduler = None
