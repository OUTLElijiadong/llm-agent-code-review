"""登录失败限流与可信代理来源识别回归测试。"""

from __future__ import annotations

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


class _SharedRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str):
        value = self.values.get(key)
        return None if value is None else str(value)

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    def eval(self, _script: str, _keys: int, key: str, window: int):
        self.values[key] = self.values.get(key, 0) + 1
        self.ttls[key] = int(window)
        return [self.values[key], self.ttls[key]]

    def delete(self, key: str) -> int:
        existed = int(key in self.values)
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return existed


def test_login_failure_limiter_redis_state_is_shared_between_instances() -> None:
    shared = _SharedRedis()
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
