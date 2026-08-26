"""共享接口限流、可信代理来源识别与登录失败计数。"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from fastapi import Request
from loguru import logger
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings


def _parsed_ip(value: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def client_ip(
    request: Request,
    *,
    trusted_proxy_cidrs: Optional[Iterable[str]] = None,
) -> str:
    """返回不可由公网客户端伪造的来源 IP。

    Nginx 会覆盖写入 ``X-Real-IP``。只有直连对端位于显式可信网段时才读取
    该头；不再信任客户端可自行拼接的 ``X-Forwarded-For`` 首段。
    """

    peer_text = request.client.host if request.client else ""
    peer = _parsed_ip(peer_text)
    if peer is None:
        return get_remote_address(request) or ""

    networks = trusted_proxy_cidrs if trusted_proxy_cidrs is not None else settings.trusted_proxy_cidrs
    trusted = False
    for item in networks:
        try:
            if peer in ipaddress.ip_network(str(item), strict=False):
                trusted = True
                break
        except ValueError:
            logger.warning("[rate-limit] 忽略无效可信代理网段: {}", item)
    if trusted:
        forwarded = _parsed_ip(request.headers.get("x-real-ip", ""))
        if forwarded is not None:
            return str(forwarded)
    return str(peer)


def _client_key(request: Request) -> str:
    return client_ip(request)


def build_limiter(storage_uri: Optional[str] = None) -> Limiter:
    """构造 SlowAPI 限流器；生产 Redis、多 worker 共享同一计数。"""

    return Limiter(
        key_func=_client_key,
        storage_uri=(storage_uri if storage_uri is not None else settings.redis_url) or "memory://",
        headers_enabled=True,
        retry_after="delta-seconds",
        key_prefix="prism:api",
    )


@dataclass(frozen=True)
class LoginLimitState:
    allowed: bool
    remaining: int
    retry_after: int = 0


class LoginFailureLimiter:
    """仅记录认证失败的固定窗口限流器。

    Redis 可用时使用 Lua 原子递增并设置 TTL；开发/测试或 Redis 短暂故障时
    回退到当前进程内存，避免限流依赖故障把正常登录全部阻断。
    """

    _INCREMENT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {current, redis.call('TTL', KEYS[1])}
"""

    def __init__(
        self,
        *,
        redis_url: str,
        limit: int,
        window_seconds: int,
        redis_client: Any = None,
    ) -> None:
        self.redis_url = (redis_url or "").strip()
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._redis = redis_client
        self._redis_initialized = redis_client is not None
        self._memory: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def _key(self, ip: str) -> str:
        digest = hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()
        return f"prism:auth:login-failure:{digest}"

    def _client(self):
        if self._redis_initialized:
            return self._redis
        self._redis_initialized = True
        if not self.redis_url:
            return None
        try:
            from redis import Redis

            self._redis = Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
        except Exception as exc:  # pragma: no cover - import/config failure is environment-specific
            logger.warning("[rate-limit] Redis 限流初始化失败，降级进程内计数: {}", exc)
            self._redis = None
        return self._redis

    def _state(self, count: int, ttl: int) -> LoginLimitState:
        blocked = count >= self.limit
        return LoginLimitState(
            allowed=not blocked,
            remaining=max(0, self.limit - count),
            retry_after=max(1, int(ttl)) if blocked else 0,
        )

    def _memory_check(self, key: str) -> LoginLimitState:
        now = time.monotonic()
        with self._lock:
            count, expires_at = self._memory.get(key, (0, 0.0))
            if expires_at <= now:
                self._memory.pop(key, None)
                return self._state(0, self.window_seconds)
            return self._state(count, math.ceil(expires_at - now))

    def _memory_increment(self, key: str) -> LoginLimitState:
        now = time.monotonic()
        with self._lock:
            count, expires_at = self._memory.get(key, (0, 0.0))
            if expires_at <= now:
                count = 0
                expires_at = now + self.window_seconds
            count += 1
            self._memory[key] = (count, expires_at)
            return self._state(count, math.ceil(expires_at - now))

    def check(self, ip: str) -> LoginLimitState:
        key = self._key(ip)
        client = self._client()
        if client is not None:
            try:
                value = client.get(key)
                if value is None:
                    return self._state(0, self.window_seconds)
                ttl = int(client.ttl(key))
                return self._state(int(value), ttl if ttl > 0 else self.window_seconds)
            except Exception as exc:  # pragma: no cover - exercised by production fault injection
                logger.warning("[rate-limit] Redis 查询失败，降级进程内计数: {}", exc)
        return self._memory_check(key)

    def record_failure(self, ip: str) -> LoginLimitState:
        key = self._key(ip)
        client = self._client()
        if client is not None:
            try:
                count, ttl = client.eval(self._INCREMENT_SCRIPT, 1, key, self.window_seconds)
                return self._state(int(count), int(ttl) if int(ttl) > 0 else self.window_seconds)
            except Exception as exc:  # pragma: no cover - exercised by production fault injection
                logger.warning("[rate-limit] Redis 写入失败，降级进程内计数: {}", exc)
        return self._memory_increment(key)

    def reset(self, ip: str) -> None:
        key = self._key(ip)
        client = self._client()
        if client is not None:
            try:
                client.delete(key)
            except Exception as exc:  # pragma: no cover - exercised by production fault injection
                logger.warning("[rate-limit] Redis 清理失败，继续清理进程内计数: {}", exc)
        with self._lock:
            self._memory.pop(key, None)


limiter = build_limiter()
login_failure_limiter = LoginFailureLimiter(
    redis_url=settings.redis_url,
    limit=settings.login_failure_limit,
    window_seconds=settings.login_failure_window_seconds,
)
