"""远程项目导入持久化队列消费者。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from loguru import logger

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import project_import_service

_scheduler = None


def _execute_claimed(claimed: dict[str, Any]) -> bool:
    db = SessionLocal()
    try:
        try:
            result = project_import_service.execute_claimed_import(
                db,
                int(claimed["id"]),
                lease_token=str(claimed["lease_token"]),
            )
            completed = project_import_service.complete_import_task(
                db,
                int(claimed["id"]),
                lease_token=str(claimed["lease_token"]),
                result=result,
            )
            if not completed:
                logger.warning(
                    "[project-import] task={} 完成时租约已失效",
                    claimed.get("task_id"),
                )
            return completed
        except Exception as exc:  # noqa: BLE001 - 单任务失败必须持久化并隔离
            db.rollback()
            saved = project_import_service.fail_import_task(
                db,
                int(claimed["id"]),
                lease_token=str(claimed["lease_token"]),
                error=exc,
                retryable=project_import_service.is_retryable_error(exc),
            )
            if not saved:
                logger.warning(
                    "[project-import] task={} 失败时租约已失效: {}",
                    claimed.get("task_id"),
                    exc,
                )
            else:
                logger.warning(
                    "[project-import] task={} 执行失败并已记录: {}",
                    claimed.get("task_id"),
                    exc,
                )
            return False
    finally:
        db.close()


def dispatch_once() -> dict[str, int]:
    """回收过期租约并消费一批任务。"""

    stats = {"recovered": 0, "claimed": 0, "completed": 0, "failed": 0}
    if not settings.project_import_dispatcher_enabled:
        return stats
    db = SessionLocal()
    try:
        stats["recovered"] = project_import_service.recover_expired_leases(db)
        worker_count = max(1, min(int(settings.project_import_max_workers), 4))
        claimed: list[dict[str, Any]] = []
        for _ in range(worker_count):
            task = project_import_service.claim_next_task(
                db,
                lease_seconds=int(settings.project_import_lease_seconds),
            )
            if task is None:
                break
            claimed.append(task)
        stats["claimed"] = len(claimed)
    finally:
        db.close()

    if not claimed:
        return stats
    with ThreadPoolExecutor(
        max_workers=len(claimed),
        thread_name_prefix="project-import",
    ) as executor:
        futures = {executor.submit(_execute_claimed, item): item for item in claimed}
        for future in as_completed(futures):
            item = futures[future]
            try:
                succeeded = future.result()
            except Exception as exc:  # pragma: no cover - worker 内已有兜底
                logger.exception(
                    "[project-import] task={} worker 崩溃: {}",
                    item.get("task_id"),
                    exc,
                )
                succeeded = False
            stats["completed" if succeeded else "failed"] += 1
    return stats


def start_project_import_dispatcher() -> None:
    global _scheduler
    if not settings.project_import_dispatcher_enabled:
        logger.info("[project-import] dispatcher disabled by config")
        return
    if _scheduler and getattr(_scheduler, "running", False):
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        dispatch_once,
        "interval",
        id="project-remote-import-dispatch",
        seconds=max(1, int(settings.project_import_dispatch_interval_seconds)),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[project-import] dispatcher started")


def stop_project_import_dispatcher() -> None:
    global _scheduler
    if not _scheduler:
        return
    try:
        if getattr(_scheduler, "running", False):
            _scheduler.shutdown(wait=False)
    finally:
        _scheduler = None
        logger.info("[project-import] dispatcher stopped")
