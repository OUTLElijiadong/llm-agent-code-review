"""服务器运行状态采集服务。

用 psutil 采集 CPU/内存/磁盘/负载/运行时长,供管理员总览大屏"服务器状态"卡片。
psutil 缺失时优雅降级(返回 available=False),不影响大屏其它模块。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

try:
    import psutil  # type: ignore

    _HAS_PSUTIL = True
except Exception:  # pragma: no cover
    _HAS_PSUTIL = False

_BOOT_TIME = time.time()


def system_status() -> dict:
    """采集服务器运行状态。

    Returns:
        dict: {
            available: 是否可采集(psutil 是否可用),
            cpu_percent, memory_percent, memory_used_mb, memory_total_mb,
            disk_percent, disk_used_gb, disk_total_gb,
            load_avg: [1m,5m,15m] 或 None,
            uptime_seconds, process_uptime_seconds, collected_at
        }
    """
    base = {
        "available": _HAS_PSUTIL,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "process_uptime_seconds": int(time.time() - _BOOT_TIME),
    }
    if not _HAS_PSUTIL:
        return base

    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    try:
        load = list(psutil.getloadavg())
    except (AttributeError, OSError):
        load = None
    uptime = int(time.time() - psutil.boot_time())

    base.update(
        {
            "cpu_percent": round(cpu, 1),
            "memory_percent": round(mem.percent, 1),
            "memory_used_mb": round(mem.used / 1024 / 1024, 1),
            "memory_total_mb": round(mem.total / 1024 / 1024, 1),
            "disk_percent": round(disk.percent, 1),
            "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
            "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
            "load_avg": [round(x, 2) for x in load] if load else None,
            "uptime_seconds": uptime,
        }
    )
    return base
