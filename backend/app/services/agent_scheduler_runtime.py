"""Agent 治理后台调度运行时。

将持久化的 Agent 调度任务注册到 APScheduler，并由 FastAPI 生命周期启动和停止。

v3.0 AgentSkill 升级新增:
- _parse_hourly_schedule(): 解析 ``hourly@*:MM`` 格式的每小时调度表达式
- start_agent_governance_scheduler() 处理 daily、hourly 及分钟/秒级 interval schedule
- 同时启动 Skill 事件触发后台 task(由 event_bus.start_skill_event_subscriber 提供)
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from threading import RLock
from typing import Optional

from loguru import logger

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import ServiceUnavailableError, ValidationError
from app.services import agent_governance_service, scheduler_service

_scheduler = None
job_configuration_lock = RLock()


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


def _parse_interval_seconds_schedule(schedule: str) -> Optional[int]:
    """解析 ``interval@Ns``，返回秒数。"""
    prefix = "interval@"
    if not schedule or not schedule.startswith(prefix):
        return None
    value = schedule[len(prefix):].strip().lower()
    if not value.endswith("s"):
        return None
    try:
        seconds = int(value[:-1])
    except ValueError:
        return None
    return seconds if 1 <= seconds <= 86400 else None


def _parse_schedule(schedule: str):
    """解析已有简写、manual 和 APScheduler 五段 cron（星期一为 0）。"""
    if not isinstance(schedule, str) or not schedule.strip():
        raise ValueError("调度表达式不能为空")
    schedule = schedule.strip()
    if schedule == "manual":
        return None
    daily = _parse_daily_schedule(schedule)
    if daily is not None:
        return "cron", {"hour": daily[0], "minute": daily[1]}
    hourly = _parse_hourly_schedule(schedule)
    if hourly is not None:
        return "cron", {"hour": "*", "minute": hourly}
    minutes = _parse_interval_schedule(schedule)
    if minutes is not None:
        return "interval", {"minutes": minutes}
    seconds = _parse_interval_seconds_schedule(schedule)
    if seconds is not None:
        return "interval", {"seconds": seconds}
    if len(schedule.split()) == 5:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(schedule, timezone=getattr(_scheduler, "timezone", None))
        if trigger.get_next_fire_time(None, datetime.now(trigger.timezone)) is None:
            raise ValueError("调度表达式没有可执行日期")
        minute, hour, day, month, weekday = schedule.split()
        return "cron", {"minute": minute, "hour": hour, "day": day, "month": month, "day_of_week": weekday}
    raise ValueError("不支持的调度表达式")


def validate_schedule(schedule: str) -> str:
    """保存前验证计划，非法表达式不能以成功响应静默入库。"""
    try:
        _parse_schedule(schedule)
    except (TypeError, ValueError) as exc:
        raise ValidationError("无效调度计划：请使用 daily、hourly、interval、五段 cron 或 manual") from exc
    return schedule.strip()


def _run_scheduled_job(job_id: int, expected_schedule: Optional[str] = None) -> None:
    """执行一次后台调度任务并记录日志。

    Args:
        job_id: AgentJob 主键。

    Returns:
        None。
    """
    db = SessionLocal()
    try:
        run = scheduler_service.run_job(
            db, job_id, system_scheduled=True, expected_schedule=expected_schedule,
        )
        logger.info("[agent-governance-scheduler] job_id={} run_id={} status={}", job_id, run.id, run.status)
    except Exception as exc:  # noqa: BLE001 - 后台任务异常不能杀死调度器
        logger.warning("[agent-governance-scheduler] job_id={} failed: {}", job_id, exc)
    finally:
        db.close()


def _register_job_to_scheduler(scheduler, job) -> bool:
    """将单个 AgentJob 注册到 APScheduler，支持 daily、hourly 与 interval。

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

    try:
        parsed = _parse_schedule(job.schedule)
    except (TypeError, ValueError):
        logger.warning(
            "[agent-governance-scheduler] unsupported schedule job_code={} schedule={}",
            job.job_code, job.schedule,
        )
        return False
    if parsed is None:
        return False
    trigger, trigger_args = parsed
    scheduler.add_job(
        _run_scheduled_job,
        trigger,
        id=f"agent-governance-{job.id}",
        args=[job.id, job.schedule],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        **trigger_args,
    )
    return True


def _restore_registration(scheduler, scheduler_id: str, previous: Optional[dict]) -> None:
    current = scheduler.get_job(scheduler_id)
    if previous is None:
        if current is not None:
            scheduler.remove_job(scheduler_id)
    elif current is None:
        scheduler.add_job(id=scheduler_id, replace_existing=True, **previous)
    else:
        scheduler.modify_job(scheduler_id, **previous)


@contextmanager
def synchronize_job(job):
    """在配置锁内与数据库提交协作，失败恢复原触发器及下次执行时间。"""
    scheduler = _scheduler
    if scheduler is None or not scheduler.running:
        if settings.agent_governance_scheduler_enabled:
            raise ServiceUnavailableError("后台调度器不可用，任务配置未保存，请稍后重试")
        yield
        return

    scheduler_id = f"agent-governance-{job.id}"
    current = scheduler.get_job(scheduler_id)
    previous = None
    if current is not None:
        previous = {
            name: getattr(current, name)
            for name in (
                "func", "trigger", "args", "kwargs", "executor", "name",
                "misfire_grace_time", "coalesce", "max_instances", "next_run_time",
            )
        }
    should_register = (
        job.status == "enabled"
        and job.schedule != "manual"
        and settings.agent_governance_scheduler_enabled
        and (job.job_type not in {"skill_evolution", "skill_proactive"} or settings.skill_scheduler_enabled)
    )
    try:
        if should_register:
            if current is None or current.args != (job.id, job.schedule):
                if not _register_job_to_scheduler(scheduler, job):
                    raise ValueError("调度计划未注册")
        elif current is not None:
            scheduler.remove_job(scheduler_id)
    except Exception as exc:
        try:
            _restore_registration(scheduler, scheduler_id, previous)
        except Exception as restore_error:
            logger.exception("[agent-governance-scheduler] registration rollback failed job_id={}", job.id)
            raise ServiceUnavailableError("调度同步及恢复失败，配置未保存，请重试并核验后台计划") from restore_error
        raise ServiceUnavailableError("调度同步失败，任务配置未保存，请稍后重试") from exc

    try:
        yield
    except Exception:
        try:
            _restore_registration(scheduler, scheduler_id, previous)
        except Exception as restore_error:
            logger.exception("[agent-governance-scheduler] registration rollback failed job_id={}", job.id)
            raise ServiceUnavailableError("数据库提交失败且调度恢复失败，请重试并核验后台计划") from restore_error
        raise


def start_agent_governance_scheduler() -> None:
    """序列化启动与配置更新，避免启动快照覆盖刚保存的计划。"""
    with job_configuration_lock:
        _start_agent_governance_scheduler()


def _start_agent_governance_scheduler() -> None:
    """启动 Agent 治理后台调度器(含 v3.0 Skill 调度 + 事件触发订阅)

    流程:
        1. 启动 APScheduler 后台调度器(daily + hourly + interval)
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
    """在停止调度器前等待当前配置事务完成。"""
    with job_configuration_lock:
        _stop_agent_governance_scheduler()


def _stop_agent_governance_scheduler() -> None:
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
