"""Recover durable enterprise Xiaoling queue items after request/process loss."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import AppError
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantProactiveEvent, EnterpriseAssistantRun

_scheduler = None


def dispatch_once() -> dict[str, int]:
    """Execute a bounded batch of queued runs through the same DB-fenced claim path as HTTP."""
    stats = {"queued": 0, "attempted": 0, "failed": 0}
    if not settings.enterprise_assistant_worker_enabled:
        return stats

    # A failed optional trigger scan must never block explicit @小菱 runs.
    if settings.billing_commercial_metering_ready:
        try:
            _dispatch_proactive_events()
        except Exception:  # noqa: BLE001
            logger.exception("[enterprise-assistant] 主动讨论事件扫描失败")
            stats["failed"] += 1

    db = SessionLocal()
    try:
        bind = db.get_bind()
        db_engine = bind.engine if hasattr(bind, "engine") else bind
        candidates = (
            db.query(
                EnterpriseAssistantRun.enterprise_id,
                EnterpriseAssistantRun.conversation_id,
                EnterpriseAssistantRun.public_id,
            )
            .join(
                EnterpriseAssistantRuntimeState,
                EnterpriseAssistantRuntimeState.assistant_run_id == EnterpriseAssistantRun.id,
            )
            .filter(
                EnterpriseAssistantRuntimeState.status == "queued",
                EnterpriseAssistantRuntimeState.stop_requested.is_(False),
                EnterpriseAssistantRun.status == "queued",
            )
            .order_by(EnterpriseAssistantRuntimeState.id.asc())
            .limit(int(settings.enterprise_assistant_worker_batch_size))
            .all()
        )
        tasks = [(int(row.enterprise_id), int(row.conversation_id), str(row.public_id)) for row in candidates]
        stats["queued"] = len(tasks)
    except Exception:  # noqa: BLE001 - a failed scan must not take down the API process
        logger.exception("[enterprise-assistant] 队列扫描失败")
        stats["failed"] += 1
        return stats
    finally:
        db.close()

    if not tasks:
        return stats

    from app.services.enterprise_assistant_runtime_service import execute_queued_run

    concurrency = min(
        len(tasks), max(1, int(settings.enterprise_assistant_worker_concurrency)),
    )
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="enterprise-assistant") as pool:
        futures = {
            pool.submit(
                asyncio.run,
                execute_queued_run(
                    db_engine,
                    enterprise_id=enterprise_id,
                    conversation_id=conversation_id,
                    run_public_id=run_public_id,
                ),
            ): (enterprise_id, run_public_id)
            for enterprise_id, conversation_id, run_public_id in tasks
        }
        for future in as_completed(futures):
            enterprise_id, run_public_id = futures[future]
            try:
                future.result()
                stats["attempted"] += 1
            except Exception:  # noqa: BLE001 - leave durable run state for the next scan/reconciliation
                logger.exception(
                    "[enterprise-assistant] 执行队列失败 enterprise={} run={}",
                    enterprise_id,
                    run_public_id,
                )
                stats["failed"] += 1
    return stats


def _dispatch_proactive_events() -> None:
    from app.services.enterprise_assistant_runtime_service import run_for_message

    db = SessionLocal()
    try:
        events = (db.query(EnterpriseAssistantProactiveEvent).filter_by(status="pending")
                  .order_by(EnterpriseAssistantProactiveEvent.id.asc())
                  .limit(int(settings.enterprise_assistant_worker_batch_size)).all())
        triggers = [(int(e.id), int(e.enterprise_id), int(e.conversation_id),
                     int(e.actor_user_id), int(e.source_message_id)) for e in events]
    finally:
        db.close()
    for event_id, enterprise_id, conversation_id, actor_id, source_id in triggers:
        trigger_db = SessionLocal()
        try:
            asyncio.run(run_for_message(
                trigger_db, enterprise_id=enterprise_id, user_id=actor_id,
                conversation_id=conversation_id, source_message_id=source_id,
                _proactive_event_id=event_id,
            ))
        except AppError:
            trigger_db.rollback()
            event = (trigger_db.query(EnterpriseAssistantProactiveEvent).filter_by(id=event_id)
                     .populate_existing().with_for_update().first())
            if event is not None and event.status == "pending":
                event.status = "skipped"
                trigger_db.commit()
        except Exception:  # noqa: BLE001 - retain event for recovery; no provider call occurred
            trigger_db.rollback()
            logger.exception("[enterprise-assistant] 主动讨论事件准备失败 event={}", event_id)
        finally:
            trigger_db.close()


def start_enterprise_assistant_dispatcher() -> None:
    global _scheduler
    if not settings.enterprise_assistant_worker_enabled:
        logger.info("[enterprise-assistant] queued-run recovery disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return

    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        dispatch_once,
        "interval",
        id="enterprise-assistant-queued-runs",
        seconds=max(1, int(settings.enterprise_assistant_worker_interval_seconds)),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[enterprise-assistant] queued-run recovery started")


def stop_enterprise_assistant_dispatcher() -> None:
    global _scheduler
    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        logger.info("[enterprise-assistant] queued-run recovery stopped")
