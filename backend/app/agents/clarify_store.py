"""Clarify 会话临时存储 (v2.0 M4)

Agent 主动追问时,把待补充的 intent + payload 暂存在内存中,
用户回填后通过 clarify_id 取回,合并 answers 继续执行。

TTL 默认 5 分钟,过期自动失效。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Dict, Optional


@dataclass
class _ClarifyEntry:
    data: dict
    expires_at: float
    processing: bool = False


class ClarifyStore:
    _instance: Optional["ClarifyStore"] = None

    def __init__(self, ttl_seconds: int = 300):
        self._store: Dict[str, _ClarifyEntry] = {}
        self._ttl = ttl_seconds
        self._lock = RLock()

    @classmethod
    def instance(cls) -> "ClarifyStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def put(self, key: str, data: dict) -> None:
        with self._lock:
            self._gc()
            self._store[key] = _ClarifyEntry(data=data, expires_at=time.time() + self._ttl)

    def pop(self, key: str) -> Optional[dict]:
        with self._lock:
            self._gc()
            entry = self._store.pop(key, None)
            if entry is None:
                return None
            if entry.expires_at < time.time():
                return None
            return entry.data

    def reserve(self, key: str, *, expected_data: Optional[dict] = None) -> Optional[dict]:
        """原子预约一次追问，防止并发请求重复派发副作用。

        预约不会删除数据；调用方在派发失败时应调用 ``release``，成功时调用
        ``consume``。``expected_data`` 绑定此前的 peek 结果，避免旧请求覆盖新条目。
        """
        with self._lock:
            self._gc()
            entry = self._store.get(key)
            if entry is None or entry.processing:
                return None
            if expected_data is not None and entry.data is not expected_data:
                return None
            entry.processing = True
            return entry.data

    def release(self, key: str, data: dict) -> bool:
        """释放失败派发的预约，使用户可以重试。"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.data is not data:
                return False
            entry.processing = False
            return True

    def consume(self, key: str, data: dict) -> bool:
        """仅消费仍绑定同一数据且已预约的追问。"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.data is not data or not entry.processing:
                return False
            del self._store[key]
            return True

    def peek(self, key: str) -> Optional[dict]:
        with self._lock:
            self._gc()
            entry = self._store.get(key)
            if entry is None or entry.expires_at < time.time():
                return None
            return entry.data

    def _gc(self) -> None:
        now = time.time()
        expired = [k for k, e in self._store.items() if e.expires_at < now]
        for k in expired:
            self._store.pop(k, None)

    def size(self) -> int:
        with self._lock:
            self._gc()
            return len(self._store)
