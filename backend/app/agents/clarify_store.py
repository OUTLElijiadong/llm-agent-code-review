"""Clarify 会话临时存储 (v2.0 M4)

Agent 主动追问时,把待补充的 intent + payload 暂存在内存中,
用户回填后通过 clarify_id 取回,合并 answers 继续执行。

TTL 默认 5 分钟,过期自动失效。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class _ClarifyEntry:
    data: dict
    expires_at: float


class ClarifyStore:
    _instance: Optional["ClarifyStore"] = None

    def __init__(self, ttl_seconds: int = 300):
        self._store: Dict[str, _ClarifyEntry] = {}
        self._ttl = ttl_seconds

    @classmethod
    def instance(cls) -> "ClarifyStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def put(self, key: str, data: dict) -> None:
        self._gc()
        self._store[key] = _ClarifyEntry(data=data, expires_at=time.time() + self._ttl)

    def pop(self, key: str) -> Optional[dict]:
        self._gc()
        entry = self._store.pop(key, None)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            return None
        return entry.data

    def peek(self, key: str) -> Optional[dict]:
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
        self._gc()
        return len(self._store)
