"""进程内隔离源码重负载互斥门。

生产 Backend 当前以单 Uvicorn worker 运行；该门限制大归档解包、静态扫描和
YARA 扫描不能并发占用数百 MiB 内存。项目行锁另行保证跨请求的数据一致性。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from app.core.exceptions import ConflictError

_SOURCE_ARCHIVE_GATE = threading.BoundedSemaphore(value=1)


@contextmanager
def source_archive_workload() -> Iterator[None]:
    """非阻塞获取隔离源码重负载槽；忙时明确拒绝，避免请求堆积。"""
    acquired = _SOURCE_ARCHIVE_GATE.acquire(blocking=False)
    if not acquired:
        raise ConflictError("已有整包源码审计正在运行，请稍后重试", code=40902)
    try:
        yield
    finally:
        _SOURCE_ARCHIVE_GATE.release()
