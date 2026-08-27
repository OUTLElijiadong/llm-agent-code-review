"""共享接口限流、可信代理来源识别与登录失败计数。"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class LoginAttempt(LoginLimitState):
    reservation_id: Optional[str] = None


@dataclass
class _MemoryLoginBucket:
    failures: int = 0
    pending: int = 0
    reservations: set[str] = field(default_factory=set)
    expires_at: float = 0.0


class LoginFailureLimiter:
    """带原子准入预留的登录失败固定窗口限流器。

    登录开始前先占用一个窗口名额，认证结束后再把预留原子结算为成功或失败。
    Redis 可用时所有状态转换均由 Lua 完成；任一 Redis 操作失败后，该实例
    粘性降级到已同步的进程内影子状态，避免后续读写落在不同后端。
    """

    _CHECK_SCRIPT = """
-- prism:login-check
local failures = tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
local pending = tonumber(redis.call('HGET', KEYS[1], 'pending') or '0')
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = tonumber(ARGV[1]) end
return {failures, pending, ttl}
"""

    _RESERVE_SCRIPT = """
-- prism:login-reserve
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local token_field = 'r:' .. ARGV[3]
local failures = tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
local pending = tonumber(redis.call('HGET', KEYS[1], 'pending') or '0')
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = window end
if redis.call('HEXISTS', KEYS[1], token_field) == 1 then
  return {1, failures, pending, ttl}
end
if failures + pending >= limit then
  return {0, failures, pending, ttl}
end
pending = redis.call('HINCRBY', KEYS[1], 'pending', 1)
redis.call('HSET', KEYS[1], token_field, 1)
if redis.call('TTL', KEYS[1]) < 1 then redis.call('EXPIRE', KEYS[1], window) end
return {1, failures, pending, redis.call('TTL', KEYS[1])}
"""

    _FINISH_SUCCESS_SCRIPT = """
-- prism:login-finish-success
local token_field = 'r:' .. ARGV[1]
local window = tonumber(ARGV[2])
local settled = redis.call('HDEL', KEYS[1], token_field)
local failures = tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
local pending = tonumber(redis.call('HGET', KEYS[1], 'pending') or '0')
if settled == 1 then
  pending = math.max(0, pending - 1)
  failures = 0
  if pending == 0 then
    redis.call('DEL', KEYS[1])
    return {1, 0, 0, window}
  end
  redis.call('HSET', KEYS[1], 'pending', pending, 'failures', failures)
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = window end
return {settled, failures, pending, ttl}
"""

    _FINISH_FAILURE_SCRIPT = """
-- prism:login-finish-failure
local limit = tonumber(ARGV[1])
local token_field = 'r:' .. ARGV[2]
local window = tonumber(ARGV[3])
local settled = redis.call('HDEL', KEYS[1], token_field)
local failures = tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
local pending = tonumber(redis.call('HGET', KEYS[1], 'pending') or '0')
if settled == 1 then
  pending = math.max(0, pending - 1)
  failures = math.min(limit, failures + 1)
  redis.call('HSET', KEYS[1], 'pending', pending, 'failures', failures)
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then
  redis.call('EXPIRE', KEYS[1], window)
  ttl = window
end
return {settled, failures, pending, ttl}
"""

    _RELEASE_SCRIPT = """
-- prism:login-release
local token_field = 'r:' .. ARGV[1]
local window = tonumber(ARGV[2])
local settled = redis.call('HDEL', KEYS[1], token_field)
local failures = tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
local pending = tonumber(redis.call('HGET', KEYS[1], 'pending') or '0')
if settled == 1 then
  pending = math.max(0, pending - 1)
  if failures == 0 and pending == 0 then
    redis.call('DEL', KEYS[1])
    return {1, 0, 0, window}
  end
  redis.call('HSET', KEYS[1], 'pending', pending)
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then ttl = window end
return {settled, failures, pending, ttl}
"""

    _INCREMENT_SCRIPT = """
-- prism:login-increment
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local failures = tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
local pending = tonumber(redis.call('HGET', KEYS[1], 'pending') or '0')
failures = math.min(limit, failures + 1)
redis.call('HSET', KEYS[1], 'failures', failures, 'pending', pending)
if redis.call('TTL', KEYS[1]) < 1 then redis.call('EXPIRE', KEYS[1], window) end
return {failures, pending, redis.call('TTL', KEYS[1])}
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
        self._redis_disabled = False
        self._redis_lock = threading.RLock()
        self._memory: dict[str, _MemoryLoginBucket] = {}
        self._lock = threading.Lock()

    def _key(self, ip: str) -> str:
        digest = hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()
        return f"prism:auth:login-limit:v2:{digest}"

    def _client(self):
        with self._redis_lock:
            if self._redis_disabled:
                return None
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
            except Exception as exc:  # pragma: no cover - environment-specific
                self._redis_disabled = True
                self._redis = None
                logger.warning("[rate-limit] Redis 限流初始化失败，粘性降级进程内计数: {}", exc)
            return self._redis

    def _disable_redis(self, exc: Exception) -> None:
        with self._redis_lock:
            first_failure = not self._redis_disabled
            self._redis_disabled = True
            self._redis_initialized = True
            self._redis = None
        if first_failure:
            logger.warning("[rate-limit] Redis 操作失败，粘性降级进程内计数: {}", exc)

    def _eval(self, script: str, key: str, *args: object):
        client = self._client()
        if client is None:
            return None
        try:
            result = client.eval(script, 1, key, *args)
        except Exception as exc:  # pragma: no cover - covered by fault-injection fake
            self._disable_redis(exc)
            return None
        with self._redis_lock:
            return None if self._redis_disabled else result

    def _numbers(self, result: Any, expected: int) -> Optional[tuple[int, ...]]:
        try:
            values = tuple(int(value) for value in result)
            if len(values) != expected:
                raise ValueError(f"Redis Lua 返回字段数异常: {len(values)} != {expected}")
            return values
        except Exception as exc:
            self._disable_redis(exc)
            return None

    def _state(self, count: int, ttl: int) -> LoginLimitState:
        blocked = count >= self.limit
        return LoginLimitState(
            allowed=not blocked,
            remaining=max(0, self.limit - count),
            retry_after=max(1, int(ttl)) if blocked else 0,
        )

    def _attempt(self, admitted: bool, count: int, ttl: int, reservation_id: str) -> LoginAttempt:
        if admitted:
            return LoginAttempt(
                allowed=True,
                remaining=max(0, self.limit - count),
                retry_after=0,
                reservation_id=reservation_id,
            )
        state = self._state(count, ttl)
        return LoginAttempt(
            allowed=False,
            remaining=state.remaining,
            retry_after=max(1, state.retry_after or ttl),
            reservation_id=None,
        )

    def _active_bucket(self, key: str, now: float) -> Optional[_MemoryLoginBucket]:
        bucket = self._memory.get(key)
        if bucket is not None and bucket.expires_at <= now:
            self._memory.pop(key, None)
            return None
        return bucket

    def _shadow(
        self,
        key: str,
        *,
        failures: int,
        pending: int,
        ttl: int,
        add_reservation: Optional[str] = None,
        remove_reservation: Optional[str] = None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._active_bucket(key, now)
            if failures <= 0 and pending <= 0:
                self._memory.pop(key, None)
                return
            if bucket is None:
                bucket = _MemoryLoginBucket()
                self._memory[key] = bucket
            bucket.failures = max(0, int(failures))
            bucket.pending = max(0, int(pending))
            bucket.expires_at = now + max(1, int(ttl))
            if remove_reservation:
                bucket.reservations.discard(remove_reservation)
            if add_reservation:
                bucket.reservations.add(add_reservation)

    def _memory_check(self, key: str) -> LoginLimitState:
        now = time.monotonic()
        with self._lock:
            bucket = self._active_bucket(key, now)
            if bucket is None:
                return self._state(0, self.window_seconds)
            return self._state(
                bucket.failures + bucket.pending,
                math.ceil(bucket.expires_at - now),
            )

    def _memory_increment(self, key: str) -> LoginLimitState:
        now = time.monotonic()
        with self._lock:
            bucket = self._active_bucket(key, now)
            if bucket is None:
                bucket = _MemoryLoginBucket(expires_at=now + self.window_seconds)
                self._memory[key] = bucket
            bucket.failures = min(self.limit, bucket.failures + 1)
            return self._state(
                bucket.failures + bucket.pending,
                math.ceil(bucket.expires_at - now),
            )

    def _memory_begin(self, key: str, reservation_id: str) -> LoginAttempt:
        now = time.monotonic()
        with self._lock:
            bucket = self._active_bucket(key, now)
            if bucket is None:
                bucket = _MemoryLoginBucket(expires_at=now + self.window_seconds)
                self._memory[key] = bucket
            current = bucket.failures + bucket.pending
            ttl = math.ceil(bucket.expires_at - now)
            if current >= self.limit:
                return self._attempt(False, current, ttl, reservation_id)
            bucket.pending += 1
            bucket.reservations.add(reservation_id)
            return self._attempt(True, current + 1, ttl, reservation_id)

    def _memory_finish(
        self,
        key: str,
        reservation_id: str,
        *,
        success: Optional[bool],
    ) -> LoginLimitState:
        now = time.monotonic()
        with self._lock:
            bucket = self._active_bucket(key, now)
            if bucket is None:
                return self._state(0, self.window_seconds)
            if reservation_id in bucket.reservations:
                bucket.reservations.remove(reservation_id)
                bucket.pending = max(0, bucket.pending - 1)
                if success is True:
                    bucket.failures = 0
                elif success is False:
                    bucket.failures = min(self.limit, bucket.failures + 1)
            count = bucket.failures + bucket.pending
            ttl = math.ceil(bucket.expires_at - now)
            if count == 0:
                self._memory.pop(key, None)
                return self._state(0, self.window_seconds)
            return self._state(count, ttl)

    def check(self, ip: str) -> LoginLimitState:
        key = self._key(ip)
        with self._redis_lock:
            result = self._eval(self._CHECK_SCRIPT, key, self.window_seconds)
            values = self._numbers(result, 3) if result is not None else None
            if values is not None:
                failures, pending, ttl = values
                ttl = ttl if ttl > 0 else self.window_seconds
                self._shadow(key, failures=failures, pending=pending, ttl=ttl)
                return self._state(failures + pending, ttl)
        return self._memory_check(key)

    def record_failure(self, ip: str) -> LoginLimitState:
        key = self._key(ip)
        with self._redis_lock:
            result = self._eval(self._INCREMENT_SCRIPT, key, self.limit, self.window_seconds)
            values = self._numbers(result, 3) if result is not None else None
            if values is not None:
                failures, pending, ttl = values
                ttl = ttl if ttl > 0 else self.window_seconds
                self._shadow(key, failures=failures, pending=pending, ttl=ttl)
                return self._state(failures + pending, ttl)
        return self._memory_increment(key)

    def begin_attempt(self, ip: str) -> LoginAttempt:
        """原子检查窗口容量并为本次认证预留一个名额。"""

        key = self._key(ip)
        reservation_id = secrets.token_hex(16)
        with self._redis_lock:
            result = self._eval(
                self._RESERVE_SCRIPT,
                key,
                self.limit,
                self.window_seconds,
                reservation_id,
            )
            values = self._numbers(result, 4) if result is not None else None
            if values is not None:
                admitted, failures, pending, ttl = values
                ttl = ttl if ttl > 0 else self.window_seconds
                self._shadow(
                    key,
                    failures=failures,
                    pending=pending,
                    ttl=ttl,
                    add_reservation=reservation_id if admitted else None,
                )
                return self._attempt(bool(admitted), failures + pending, ttl, reservation_id)
        return self._memory_begin(key, reservation_id)

    def finish_attempt(
        self,
        ip: str,
        reservation_id: Optional[str],
        *,
        success: bool,
    ) -> LoginLimitState:
        """把已准入请求原子结算；成功清零失败，失败累计一次。"""

        if not reservation_id:
            return self.check(ip)
        key = self._key(ip)
        with self._redis_lock:
            if success:
                result = self._eval(
                    self._FINISH_SUCCESS_SCRIPT,
                    key,
                    reservation_id,
                    self.window_seconds,
                )
            else:
                result = self._eval(
                    self._FINISH_FAILURE_SCRIPT,
                    key,
                    self.limit,
                    reservation_id,
                    self.window_seconds,
                )
            values = self._numbers(result, 4) if result is not None else None
            if values is not None:
                _settled, failures, pending, ttl = values
                ttl = ttl if ttl > 0 else self.window_seconds
                self._shadow(
                    key,
                    failures=failures,
                    pending=pending,
                    ttl=ttl,
                    remove_reservation=reservation_id,
                )
                return self._state(failures + pending, ttl)
        return self._memory_finish(key, reservation_id, success=success)

    def release_attempt(self, ip: str, reservation_id: Optional[str]) -> LoginLimitState:
        """认证基础设施异常时释放预留，不把系统故障计为密码失败。"""

        if not reservation_id:
            return self.check(ip)
        key = self._key(ip)
        with self._redis_lock:
            result = self._eval(
                self._RELEASE_SCRIPT,
                key,
                reservation_id,
                self.window_seconds,
            )
            values = self._numbers(result, 4) if result is not None else None
            if values is not None:
                _settled, failures, pending, ttl = values
                ttl = ttl if ttl > 0 else self.window_seconds
                self._shadow(
                    key,
                    failures=failures,
                    pending=pending,
                    ttl=ttl,
                    remove_reservation=reservation_id,
                )
                return self._state(failures + pending, ttl)
        return self._memory_finish(key, reservation_id, success=None)

    def reset(self, ip: str) -> None:
        key = self._key(ip)
        with self._redis_lock:
            client = self._client()
            if client is not None:
                try:
                    client.delete(key)
                except Exception as exc:  # pragma: no cover - covered by fault-injection fake
                    self._disable_redis(exc)
        with self._lock:
            self._memory.pop(key, None)


limiter = build_limiter()
login_failure_limiter = LoginFailureLimiter(
    redis_url=settings.redis_url,
    limit=settings.login_failure_limit,
    window_seconds=settings.login_failure_window_seconds,
)
