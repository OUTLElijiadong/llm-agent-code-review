"""应用进程内存维护：定时释放过期副本，不干预运行中业务或系统页缓存。"""
from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger

_SWEEP_INTERVAL_SECONDS = 300
_sweeper: asyncio.Task | None = None


def sweep_runtime_caches() -> dict[str, int]:
    from app.agents.discussion_bus import DiscussionBus
    from app.services.dashboard_service import purge_expired_stats_cache
    from app.services.sandbox_service import purge_expired_test_cache

    counts = {
        "dashboard": purge_expired_stats_cache(),
        "sandbox_tests": purge_expired_test_cache(),
        "roundtable_sessions": 0,
    }
    bus = DiscussionBus._instance
    if bus is not None:
        counts["roundtable_sessions"] = bus.purge_expired_sessions()
    return counts


async def _run_sweeper() -> None:
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            counts = sweep_runtime_caches()
            logger.info("[runtime-cache] sweep released expired entries: {}", counts)
        except Exception:  # noqa: BLE001 - 维护失败不终止业务，下轮重试
            logger.exception("[runtime-cache] sweep failed")


def start_runtime_cache_sweeper() -> None:
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.create_task(_run_sweeper(), name="runtime-cache-maintenance")
        logger.info("[runtime-cache] started, interval={}s", _SWEEP_INTERVAL_SECONDS)


async def stop_runtime_cache_sweeper() -> None:
    global _sweeper
    if _sweeper is not None:
        _sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await _sweeper
        _sweeper = None
