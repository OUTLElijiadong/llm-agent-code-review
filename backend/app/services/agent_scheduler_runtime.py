"""Agent 治理后台调度运行时。

将持久化的 Agent 调度任务注册到 APScheduler，并由 FastAPI 生命周期启动和停止。

v3.0 AgentSkill 升级新增:
- _parse_hourly_schedule(): 解析 ``hourly@*:MM`` 格式的每小时调度表达式
- start_agent_governance_scheduler() 同时处理 daily 与 hourly 两种 schedule
- 同时启动 Skill 事件触发后台 task(由 event_bus.start_skill_event_subscriber 提供)
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import agent_governance_service, scheduler_service

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


def _parse_hourly_schedule(schedule: str) -> Optional[int]:
    """解析 ``hourly@*:MM`` 格式的每小时调度表达式(v3.0 AgentSkill 升级)

    支持的格式:
        - ``hourly@*:MM`` → 每小时的 MM 分钟触发
        - ``hourly@MM`` → 等价于 ``hourly@*:MM``

    Args:
        schedule: 数据库存储的调度表达式。

    Returns:
        Optional[int]: 成功时返回分钟(0-59), 否则返回 None。
    """
    prefix = "hourly@"
    if not schedule or not schedule.startswith(prefix):
        return None
    clock = schedule[len(prefix):]
    # 兼容 *:MM 与 MM 两种写法
    if clock.startswith("*:"):
        clock = clock[2:]
    try:
        minute = int(clock)
    except ValueError:
        return None
    if not (0 <= minute <= 59):
        return None
    return minute


def _parse_interval_schedule(schedule: str) -> Optional[int]:
    """解析 ``interval@Nm``，返回分钟数。"""
    prefix = "interval@"
    if not schedule or not schedule.startswith(prefix):
        return None
    value = schedule[len(prefix):].strip().lower()
    if not value.endswith("m"):
        return None
    try:
        minutes = int(value[:-1])
    except ValueError:
        return None
    return minutes if 1 <= minutes <= 1440 else None


def _run_scheduled_job(job_id: int) -> None:
    """执行一次后台调度任务并记录日志。

    Args:
        job_id: AgentJob 主键。

    Returns:
        None。
    """
    db = SessionLocal()
    try:
        run = scheduler_service.run_job(db, job_id, system_scheduled=True)
        logger.info("[agent-governance-scheduler] job_id={} run_id={} status={}", job_id, run.id, run.status)
    except Exception as exc:  # noqa: BLE001 - 后台任务异常不能杀死调度器
        logger.warning("[agent-governance-scheduler] job_id={} failed: {}", job_id, exc)
    finally:
        db.close()


def _register_job_to_scheduler(scheduler, job) -> bool:
    """将单个 AgentJob 注册到 APScheduler(v3.0 抽出, 支持 daily 与 hourly 两种 schedule)

    Args:
        scheduler: APScheduler 实例
        job: AgentJob ORM 实例

    Returns:
        bool: True=注册成功, False=跳过(不支持的表达式或被禁用)
    """
    if job.status != "enabled":
        return False
    if (
        getattr(job, "job_type", "") in {"skill_evolution", "skill_proactive"}
        and not settings.skill_scheduler_enabled
    ):
        return False

    # 优先尝试 daily 表达式
    daily_parsed = _parse_daily_schedule(job.schedule)
    if daily_parsed is not None:
        hour, minute = daily_parsed
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
        return True

    # 回退到 hourly 表达式(v3.0 新增)
    hourly_minute = _parse_hourly_schedule(job.schedule)
    if hourly_minute is not None:
        scheduler.add_job(
            _run_scheduled_job,
            "cron",
            id=f"agent-governance-{job.id}",
            args=[job.id],
            hour="*",
            minute=hourly_minute,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        return True

    interval_minutes = _parse_interval_schedule(job.schedule)
    if interval_minutes is not None:
        scheduler.add_job(
            _run_scheduled_job,
            "interval",
            id=f"agent-governance-{job.id}",
            args=[job.id],
            minutes=interval_minutes,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        return True

    logger.warning(
        "[agent-governance-scheduler] unsupported schedule job_code={} schedule={}",
        job.job_code,
        job.schedule,
    )
    return False


def start_agent_governance_scheduler() -> None:
    """启动 Agent 治理后台调度器(含 v3.0 Skill 调度 + 事件触发订阅)

    流程:
        1. 启动 APScheduler 后台调度器(daily + hourly)
        2. 注册所有 enabled 的 AgentJob(包括 v3.0 的 skill_evolution / skill_proactive)
        3. 启动 Skill 事件触发后台 task(event_bus.start_skill_event_subscriber)

    Returns:
        None。
    """
    global _scheduler
    if not settings.agent_governance_scheduler_enabled:
        logger.info("[agent-governance-scheduler] disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return

    db = SessionLocal()
    try:
        # Orchestrator 已在应用 lifespan 中先完成运行时注册；此处把运行时、
        # 治理及独立运维 Agent 同步到持久化画像，保证后台重启后立即可见、可委派。
        agent_governance_service.sync_profiles(db)
        jobs = scheduler_service.ensure_default_jobs(db)
    except Exception as exc:  # noqa: BLE001 - 迁移未完成时不阻断主应用
        logger.warning("[agent-governance-scheduler] default jobs unavailable, scheduler skipped: {}", exc)
        return
    finally:
        db.close()

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as exc:  # noqa: BLE001 - 缺依赖时降级为手动触发
        logger.warning("[agent-governance-scheduler] APScheduler unavailable, manual jobs only: {}", exc)
        return

    scheduler = BackgroundScheduler()
    registered = 0
    for job in jobs:
        if _register_job_to_scheduler(scheduler, job):
            registered += 1
    from app.services.sandbox_service import expire_due_environments

    scheduler.add_job(
        expire_due_environments,
        "interval",
        id="sandbox-expiry-reaper",
        minutes=5,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[agent-governance-scheduler] started with {} jobs", registered)

    # v3.0 AgentSkill: 启动 Skill 事件触发后台 task
    try:
        from app.agents.event_bus import start_skill_event_subscriber
        start_skill_event_subscriber()
    except Exception as exc:  # noqa: BLE001 - 事件触发启动失败不阻断调度器
        logger.warning(
            "[agent-governance-scheduler] start_skill_event_subscriber 失败(不影响调度器): {}",
            exc,
        )


def stop_agent_governance_scheduler() -> None:
    """停止 Agent 治理后台调度器(含 v3.0 Skill 事件触发订阅)

    Returns:
        None。
    """
    global _scheduler

    # v3.0 AgentSkill: 先停止 Skill 事件触发后台 task
    try:
        import asyncio

        from app.agents.event_bus import stop_skill_event_subscriber
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(stop_skill_event_subscriber())
            else:
                loop.run_until_complete(stop_skill_event_subscriber())
        except RuntimeError:
            # 没有事件循环(可能在 shutdown 阶段), 直接清理 task 句柄
            pass
    except Exception as exc:  # noqa: BLE001 - 停止失败不阻断主流程
        logger.warning(
            "[agent-governance-scheduler] stop_skill_event_subscriber 失败: {}", exc
        )

    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
            logger.info("[agent-governance-scheduler] stopped")
    finally:
        _scheduler = None
