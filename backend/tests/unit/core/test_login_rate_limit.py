"""登录失败限流与可信代理来源识别回归测试。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from starlette.requests import Request

from app.core.rate_limit import LoginFailureLimiter, build_limiter, client_ip


def _request(peer: str, **headers: str) -> Request:
    raw_headers = [
        (name.replace("_", "-").encode("ascii"), value.encode("ascii"))
        for name, value in headers.items()
    ]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": raw_headers,
            "client": (peer, 443),
            "server": ("testserver", 443),
            "scheme": "https",
            "query_string": b"",
        }
    )


def test_client_ip_only_trusts_proxy_headers_from_configured_networks() -> None:
    trusted = ["127.0.0.0/8", "172.16.0.0/12"]

    proxied = _request(
        "172.20.0.3",
        x_real_ip="203.0.113.8",
        x_forwarded_for="198.51.100.99, 203.0.113.8",
    )
    assert client_ip(proxied, trusted_proxy_cidrs=trusted) == "203.0.113.8"

    direct = _request(
        "198.51.100.10",
        x_real_ip="203.0.113.9",
        x_forwarded_for="203.0.113.9",
    )
    assert client_ip(direct, trusted_proxy_cidrs=trusted) == "198.51.100.10"

    invalid = _request("127.0.0.1", x_real_ip="not-an-ip")
    assert client_ip(invalid, trusted_proxy_cidrs=trusted) == "127.0.0.1"


def test_slowapi_limiter_uses_shared_redis_and_emits_retry_after() -> None:
    limiter = build_limiter("redis://redis:6379/0")

    assert limiter._storage_uri == "redis://redis:6379/0"
    assert limiter._headers_enabled is True
    assert limiter._retry_after == "delta-seconds"


def test_login_failure_limiter_counts_only_recorded_failures_and_resets() -> None:
    limiter = LoginFailureLimiter(redis_url="", limit=2, window_seconds=60)

    assert limiter.check("203.0.113.10").allowed is True
    assert limiter.check("203.0.113.10").allowed is True

    first = limiter.record_failure("203.0.113.10")
    assert first.allowed is True
    assert first.remaining == 1

    second = limiter.record_failure("203.0.113.10")
    assert second.allowed is False
    assert second.retry_after >= 1
    assert limiter.check("203.0.113.10").allowed is False

    limiter.reset("203.0.113.10")
    assert limiter.check("203.0.113.10").allowed is True


class _AtomicRedis:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, int]] = {}
        self.expires_at: dict[str, float] = {}
        self.calls = 0
        self.fail = False
        self._lock = threading.Lock()

    def _bucket(self, key: str) -> dict[str, int]:
        expires_at = self.expires_at.get(key, 0.0)
        if expires_at <= time.monotonic():
            self.values.pop(key, None)
            self.expires_at.pop(key, None)
        return self.values.setdefault(key, {"failures": 0, "pending": 0})

    def _ttl(self, key: str) -> int:
        return max(1, int(self.expires_at.get(key, time.monotonic() + 1) - time.monotonic()))

    def eval(self, script: str, _keys: int, key: str, *args: object):
        with self._lock:
            self.calls += 1
            if self.fail:
                raise RuntimeError("redis unavailable")
            bucket = self._bucket(key)
            if "prism:login-check" in script:
                return [bucket["failures"], bucket["pending"], self._ttl(key)]
            if "prism:login-reserve" in script:
                limit, window, token = int(args[0]), int(args[1]), str(args[2])
                current = bucket["failures"] + bucket["pending"]
                if current >= limit:
                    return [0, bucket["failures"], bucket["pending"], self._ttl(key)]
                bucket["pending"] += 1
                bucket[f"r:{token}"] = 1
                self.expires_at.setdefault(key, time.monotonic() + window)
                return [1, bucket["failures"], bucket["pending"], self._ttl(key)]
            if "prism:login-finish-success" in script:
                token = str(args[0])
                if bucket.pop(f"r:{token}", None) is not None:
                    bucket["pending"] = max(0, bucket["pending"] - 1)
                    bucket["failures"] = 0
                return [1, bucket["failures"], bucket["pending"], self._ttl(key)]
            if "prism:login-finish-failure" in script:
                limit, token = int(args[0]), str(args[1])
                if bucket.pop(f"r:{token}", None) is not None:
                    bucket["pending"] = max(0, bucket["pending"] - 1)
                    bucket["failures"] = min(limit, bucket["failures"] + 1)
                return [1, bucket["failures"], bucket["pending"], self._ttl(key)]
            if "prism:login-release" in script:
                token = str(args[0])
                if bucket.pop(f"r:{token}", None) is not None:
                    bucket["pending"] = max(0, bucket["pending"] - 1)
                return [1, bucket["failures"], bucket["pending"], self._ttl(key)]
            if "prism:login-increment" in script:
                limit, window = int(args[0]), int(args[1])
                bucket["failures"] = min(limit, bucket["failures"] + 1)
                self.expires_at.setdefault(key, time.monotonic() + window)
                return [bucket["failures"], bucket["pending"], self._ttl(key)]
            raise AssertionError(f"unknown script: {script}")

    def delete(self, key: str) -> int:
        with self._lock:
            self.calls += 1
            if self.fail:
                raise RuntimeError("redis unavailable")
            existed = int(key in self.values)
            self.values.pop(key, None)
            self.expires_at.pop(key, None)
            return existed


def test_login_failure_limiter_redis_state_is_shared_between_instances() -> None:
    shared = _AtomicRedis()
    first = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=2,
        window_seconds=60,
        redis_client=shared,
    )
    second = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=2,
        window_seconds=60,
        redis_client=shared,
    )

    assert first.record_failure("203.0.113.11").allowed is True
    assert second.check("203.0.113.11").remaining == 1
    assert second.record_failure("203.0.113.11").allowed is False
    assert first.check("203.0.113.11").allowed is False


def test_memory_reservation_admission_is_atomic_under_concurrency() -> None:
    limiter = LoginFailureLimiter(redis_url="", limit=5, window_seconds=60)
    barrier = threading.Barrier(20)

    def reserve():
        barrier.wait()
        return limiter.begin_attempt("203.0.113.12")

    with ThreadPoolExecutor(max_workers=20) as pool:
        attempts = list(pool.map(lambda _index: reserve(), range(20)))

    admitted = [attempt for attempt in attempts if attempt.allowed]
    assert len(admitted) == 5
    assert limiter.check("203.0.113.12").allowed is False

    for attempt in admitted:
        limiter.finish_attempt("203.0.113.12", attempt.reservation_id, success=False)
    assert limiter.check("203.0.113.12").allowed is False


def test_success_releases_reservation_and_clears_failures() -> None:
    limiter = LoginFailureLimiter(redis_url="", limit=2, window_seconds=60)
    limiter.record_failure("203.0.113.13")
    attempt = limiter.begin_attempt("203.0.113.13")
    assert attempt.allowed is True

    state = limiter.finish_attempt("203.0.113.13", attempt.reservation_id, success=True)

    assert state.allowed is True
    assert state.remaining == 2
    assert limiter.check("203.0.113.13").remaining == 2


def test_release_returns_memory_reservation_capacity_without_counting_failure() -> None:
    limiter = LoginFailureLimiter(redis_url="", limit=1, window_seconds=60)
    attempt = limiter.begin_attempt("203.0.113.17")
    assert attempt.allowed is True
    assert limiter.begin_attempt("203.0.113.17").allowed is False

    limiter.release_attempt("203.0.113.17", attempt.reservation_id)

    assert limiter.begin_attempt("203.0.113.17").allowed is True


def test_redis_lua_reservation_admission_is_atomic_under_concurrency() -> None:
    redis = _AtomicRedis()
    limiter = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=5,
        window_seconds=60,
        redis_client=redis,
    )
    barrier = threading.Barrier(20)

    def reserve():
        barrier.wait()
        return limiter.begin_attempt("203.0.113.14")

    with ThreadPoolExecutor(max_workers=20) as pool:
        attempts = list(pool.map(lambda _index: reserve(), range(20)))

    admitted = [attempt for attempt in attempts if attempt.allowed]
    assert len(admitted) == 5
    assert limiter.check("203.0.113.14").allowed is False


def test_redis_success_clears_failures_for_other_limiter_instances() -> None:
    redis = _AtomicRedis()
    first = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=2,
        window_seconds=60,
        redis_client=redis,
    )
    second = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=2,
        window_seconds=60,
        redis_client=redis,
    )
    first.record_failure("203.0.113.18")
    attempt = first.begin_attempt("203.0.113.18")

    first.finish_attempt("203.0.113.18", attempt.reservation_id, success=True)

    assert second.check("203.0.113.18").remaining == 2


def test_redis_failure_switches_one_limiter_to_memory_without_read_write_mismatch() -> None:
    redis = _AtomicRedis()
    limiter = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=2,
        window_seconds=60,
        redis_client=redis,
    )
    limiter.record_failure("203.0.113.15")
    calls_before_failure = redis.calls
    redis.fail = True

    state_after_read_failure = limiter.check("203.0.113.15")
    assert state_after_read_failure.remaining == 1

    limiter.record_failure("203.0.113.15")
    assert redis.calls == calls_before_failure + 1
    assert limiter.check("203.0.113.15").allowed is False

    redis.fail = False
    limiter.reset("203.0.113.15")
    assert limiter.check("203.0.113.15").allowed is True
    assert redis.calls == calls_before_failure + 1


def test_redis_finish_failure_uses_shadow_and_stays_on_memory() -> None:
    redis = _AtomicRedis()
    limiter = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=2,
        window_seconds=60,
        redis_client=redis,
    )
    limiter.record_failure("203.0.113.16")
    attempt = limiter.begin_attempt("203.0.113.16")
    assert attempt.allowed is True
    redis.fail = True
    calls_before_finish = redis.calls

    state = limiter.finish_attempt("203.0.113.16", attempt.reservation_id, success=True)

    assert state.allowed is True
    assert state.remaining == 2
    assert redis.calls == calls_before_finish + 1
    redis.fail = False
    limiter.record_failure("203.0.113.16")
    assert redis.calls == calls_before_finish + 1
    assert limiter.check("203.0.113.16").remaining == 1


def test_redis_reset_failure_also_switches_limiter_to_memory_permanently() -> None:
    redis = _AtomicRedis()
    limiter = LoginFailureLimiter(
        redis_url="redis://redis:6379/0",
        limit=2,
        window_seconds=60,
        redis_client=redis,
    )
    limiter.record_failure("203.0.113.19")
    redis.fail = True
    calls_before_reset = redis.calls

    limiter.reset("203.0.113.19")

    assert redis.calls == calls_before_reset + 1
    redis.fail = False
    limiter.record_failure("203.0.113.19")
    assert redis.calls == calls_before_reset + 1
    assert limiter.check("203.0.113.19").remaining == 1
