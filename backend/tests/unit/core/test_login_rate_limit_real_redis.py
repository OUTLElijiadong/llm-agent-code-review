"""Optional real Lua contract checks against an explicitly named isolated local container.

Run with PRISM_LOGIN_TEST_REDIS_CONTAINER=prism-login-audit-<suffix>.
The container must have --network none and Redis unix socket /tmp/redis.sock.
No production Redis URL or credentials are accepted.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

import pytest

from app.core.rate_limit import LoginFailureLimiter

CONTAINER = os.environ.get('PRISM_LOGIN_TEST_REDIS_CONTAINER', '')
pytestmark = pytest.mark.skipif(not CONTAINER, reason='Requires explicitly created isolated local Redis container')


class _ContainerRedis:
    def command(self, *args):
        result = subprocess.run(
            ['docker', 'exec', CONTAINER, 'redis-cli', '-s', '/tmp/redis.sock', '--raw', *map(str, args)],
            capture_output=True, check=True, timeout=10,
        )
        return result.stdout.decode().strip()

    def eval(self, script, keys, key, *args):
        return [int(value) for value in self.command('EVAL', script, keys, key, *args).splitlines()]


@pytest.fixture
def redis_limiter():
    assert re.fullmatch(r'prism-login-audit-[a-z0-9-]+', CONTAINER)
    inspected = subprocess.run(
        ['docker', 'inspect', '--format', '{{json .HostConfig.NetworkMode}}', CONTAINER],
        capture_output=True, check=True, timeout=10,
    )
    assert json.loads(inspected.stdout) == 'none', 'Only a network-isolated local fixture is allowed'
    redis = _ContainerRedis()
    limiter = LoginFailureLimiter(redis_url='isolated-unix-socket', limit=5, window_seconds=60, redis_client=redis)
    ip = 'isolated-test-' + uuid.uuid4().hex
    yield redis, limiter, ip
    redis.command('DEL', limiter._key(ip))


def _fail(limiter, ip, count):
    for _ in range(count):
        attempt = limiter.begin_attempt(ip)
        assert attempt.allowed
        limiter.finish_attempt(ip, attempt.reservation_id, success=False)


def _last_subsecond(redis, limiter, ip):
    key = limiter._key(ip)
    assert redis.command('PEXPIRE', key, 450) == '1'
    remaining = int(redis.command('PTTL', key))
    assert 0 < remaining <= 450
    return key


def test_real_lua_keeps_same_window_blocked(redis_limiter):
    _, limiter, ip = redis_limiter
    _fail(limiter, ip, 5)
    assert not limiter.begin_attempt(ip).allowed
    assert not limiter.begin_attempt(ip).allowed


def test_real_lua_subsecond_remaining_reports_one_second_and_then_expires(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 5)
    _last_subsecond(redis, limiter, ip)
    blocked = limiter.begin_attempt(ip)
    assert not blocked.allowed
    assert blocked.retry_after == 1
    time.sleep(0.6)
    assert limiter.begin_attempt(ip).allowed


def test_real_lua_admission_near_expiry_does_not_restart_window(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 4)
    key = _last_subsecond(redis, limiter, ip)
    assert limiter.begin_attempt(ip).allowed
    assert 0 < int(redis.command('PTTL', key)) <= 450


def test_real_lua_failure_settlement_near_expiry_does_not_restart_window(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 4)
    attempt = limiter.begin_attempt(ip)
    key = _last_subsecond(redis, limiter, ip)
    state = limiter.finish_attempt(ip, attempt.reservation_id, success=False)
    assert not state.allowed
    assert state.retry_after == 1
    assert 0 < int(redis.command('PTTL', key)) <= 450


def test_real_lua_check_and_legacy_increment_preserve_subsecond_window(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 4)
    key = _last_subsecond(redis, limiter, ip)
    assert limiter.record_failure(ip).retry_after == 1
    assert limiter.check(ip).retry_after == 1
    assert 0 < int(redis.command('PTTL', key)) <= 450
