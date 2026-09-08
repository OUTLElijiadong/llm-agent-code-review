"""Configured Redis failures must not create a fresh per-worker login budget."""

from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import rate_limit
from app.core.exceptions import ServiceUnavailableError

SPEC = importlib.util.spec_from_file_location(
    "login_redis_fixture", Path(__file__).with_name("test_login_rate_limit.py")
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _limiter(redis):
    return rate_limit.LoginFailureLimiter(
        redis_url="redis://local-test.invalid", limit=5, window_seconds=60, redis_client=redis
    )


def test_warm_and_cold_workers_fail_closed_on_shared_redis_outage():
    shared = MODULE._AtomicRedis()
    warm, cold = _limiter(shared), _limiter(shared)
    for _ in range(5):
        attempt = warm.begin_attempt("192.0.2.11")
        warm.finish_attempt("192.0.2.11", attempt.reservation_id, success=False)
    shared.fail = True
    for limiter in (warm, cold):
        with pytest.raises(ServiceUnavailableError) as error:
            limiter.begin_attempt("192.0.2.11")
        assert error.value.http_status == 503 and error.value.retry_after == 5


def test_backoff_does_not_probe_early_and_recovery_preserves_shared_full_bucket(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    shared = MODULE._AtomicRedis()
    warm, cold = _limiter(shared), _limiter(shared)
    for _ in range(5):
        warm.record_failure("192.0.2.12")
    shared.fail = True
    with pytest.raises(ServiceUnavailableError):
        cold.begin_attempt("192.0.2.12")
    calls = shared.calls
    now[0] += 3.2
    shared.fail = False
    with pytest.raises(ServiceUnavailableError) as error:
        cold.begin_attempt("192.0.2.12")
    assert error.value.retry_after == 2 and shared.calls == calls
    now[0] = 1005.01
    assert not cold.begin_attempt("192.0.2.12").allowed
    assert shared.calls == calls + 1


@pytest.mark.parametrize("result", [None, ["malformed"], [1, -1, 1, 60], [2, 0, 1, 60], [1, 0, 1, 0]])
def test_invalid_redis_result_fails_closed(result):
    redis = SimpleNamespace(eval=lambda *_args: result)
    with pytest.raises(ServiceUnavailableError):
        _limiter(redis).begin_attempt("192.0.2.13")


def test_outage_concurrency_uses_one_probe_per_worker_backoff(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    shared = MODULE._AtomicRedis()
    shared.fail = True
    limiter = _limiter(shared)

    def attempt(_index):
        with pytest.raises(ServiceUnavailableError) as error:
            limiter.begin_attempt("192.0.2.16")
        return error.value.retry_after

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert list(pool.map(attempt, range(20))) == [5] * 20
    assert shared.calls == 1
    now[0] += 5.01
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert list(pool.map(attempt, range(20))) == [5] * 20
    assert shared.calls == 2


def test_redis_initialization_failure_fails_closed_and_recovers(monkeypatch):
    import redis as redis_module

    now = [1000.0]
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now[0]))
    attempts = []
    shared = MODULE._AtomicRedis()

    def connect(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise ValueError("invalid temporary configuration")
        return shared

    monkeypatch.setattr(redis_module.Redis, "from_url", connect)
    limiter = rate_limit.LoginFailureLimiter(redis_url="redis://local-test.invalid", limit=5, window_seconds=60)
    with pytest.raises(ServiceUnavailableError):
        limiter.begin_attempt("192.0.2.14")
    now[0] += 5.01
    assert limiter.begin_attempt("192.0.2.14").allowed
    assert len(attempts) == 2


def test_unconfigured_redis_keeps_local_memory_mode():
    limiter = rate_limit.LoginFailureLimiter(redis_url="", limit=5, window_seconds=60)
    for _ in range(5):
        attempt = limiter.begin_attempt("192.0.2.15")
        assert attempt.allowed
        limiter.finish_attempt("192.0.2.15", attempt.reservation_id, success=False)
    assert not limiter.begin_attempt("192.0.2.15").allowed
