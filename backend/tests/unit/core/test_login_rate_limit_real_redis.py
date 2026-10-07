"""Optional real Lua contract checks against an isolated local Redis server.

Prefer `python scripts/run_login_rate_limit_real_redis_tests.py`, which creates
a temporary Unix-socket-only Redis 7 process and uses a minimal subprocess env.
The container fixture remains available with PRISM_LOGIN_TEST_REDIS_CONTAINER.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
import redis

from app.core.rate_limit import LoginFailureLimiter

CONTAINER = os.environ.get('PRISM_LOGIN_TEST_REDIS_CONTAINER', '')
SOCKET = os.environ.get('PRISM_LOGIN_TEST_REDIS_SOCKET', '')
SERVER_SOCKET = os.environ.get('PRISM_LOGIN_TEST_REDIS_SERVER_SOCKET', '')
requires_redis = pytest.mark.skipif(
    not (CONTAINER or SOCKET),
    reason='Requires an explicitly isolated local Redis container or Unix-socket server',
)


class _ContainerRedis:
    def command(self, *args):
        result = subprocess.run(
            ['docker', 'exec', CONTAINER, 'redis-cli', '-s', '/tmp/redis.sock', '--raw', *map(str, args)],
            capture_output=True, check=True, timeout=10,
        )
        return result.stdout.decode().strip()

    def eval(self, script, keys, key, *args):
        return [int(value) for value in self.command('EVAL', script, keys, key, *args).splitlines()]


class _UnixSocketRedis:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.client = redis.Redis(
            unix_socket_path=str(socket_path),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    def command(self, *args):
        result = self.client.execute_command(*args)
        if isinstance(result, bool):
            return '1' if result else '0'
        if isinstance(result, (list, tuple)):
            return '\n'.join(str(value) for value in result)
        return str(result)

    def eval(self, script, keys, key, *args):
        result = self.client.eval(script, keys, key, *args)
        return [int(value) for value in result]

    def delete(self, key):
        return int(self.client.delete(key))


def _isolated_socket_redis() -> _UnixSocketRedis:
    socket_path = Path(SOCKET)
    assert socket_path.is_absolute()
    assert socket_path.name == 'redis.sock'
    assert socket_path.parent.name.startswith('prism-login-redis-')
    assert socket_path.parent.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert socket_path.parent.stat().st_mode & 0o077 == 0, 'Temporary directory must not be accessible to other users'
    assert socket_path.exists() and stat.S_ISSOCK(socket_path.stat().st_mode)
    assert socket_path.stat().st_mode & 0o077 == 0, 'Unix socket must not be accessible to other users'

    client = _UnixSocketRedis(socket_path)
    assert client.client.ping()
    assert client.client.config_get('port').get('port') == '0', 'TCP must be disabled'
    assert client.client.config_get('tls-port').get('tls-port') == '0', 'TLS TCP must be disabled'
    _assert_redis_server_socket_path(
        client.client.config_get('unixsocket').get('unixsocket', ''),
        socket_path,
        SERVER_SOCKET,
    )
    return client


def _assert_redis_server_socket_path(configured_socket: str, host_socket: Path, server_socket: str = '') -> None:
    expected_socket = Path(server_socket) if server_socket else host_socket
    actual_socket = Path(configured_socket)
    assert expected_socket.is_absolute() and expected_socket.name == 'redis.sock'
    assert actual_socket == expected_socket, (
        f'Redis server socket path {actual_socket} does not match expected in-server path {expected_socket}'
    )


def test_redis_socket_path_check_accepts_same_namespace(tmp_path):
    socket_path = tmp_path / 'redis.sock'
    _assert_redis_server_socket_path(str(socket_path), socket_path)


def test_redis_socket_path_check_accepts_container_bind_mount_namespace(tmp_path):
    host_socket = tmp_path / 'prism-login-redis-123-1' / 'redis.sock'
    _assert_redis_server_socket_path('/tmp/redis.sock', host_socket, '/tmp/redis.sock')


def test_redis_socket_path_check_rejects_unexpected_server_socket(tmp_path):
    host_socket = tmp_path / 'prism-login-redis-123-1' / 'redis.sock'
    with pytest.raises(AssertionError, match='does not match expected in-server path'):
        _assert_redis_server_socket_path('/var/run/redis.sock', host_socket, '/tmp/redis.sock')


@pytest.fixture
def redis_limiter():
    assert not (CONTAINER and SOCKET), 'Select exactly one isolated Redis fixture'
    if CONTAINER:
        assert re.fullmatch(r'prism-login-audit-[a-z0-9-]+', CONTAINER)
        inspected = subprocess.run(
            ['docker', 'inspect', '--format', '{{json .HostConfig.NetworkMode}}', CONTAINER],
            capture_output=True, check=True, timeout=10,
        )
        assert json.loads(inspected.stdout) == 'none', 'Only a network-isolated local fixture is allowed'
        redis_client = _ContainerRedis()
    else:
        redis_client = _isolated_socket_redis()
    limiter = LoginFailureLimiter(
        redis_url='isolated-unix-socket',
        limit=5,
        window_seconds=60,
        redis_client=redis_client,
    )
    ip = 'isolated-test-' + uuid.uuid4().hex
    yield redis_client, limiter, ip
    redis_client.command('DEL', limiter._key(ip))


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


@requires_redis
def test_real_lua_keeps_same_window_blocked(redis_limiter):
    _, limiter, ip = redis_limiter
    _fail(limiter, ip, 5)
    assert not limiter.begin_attempt(ip).allowed
    assert not limiter.begin_attempt(ip).allowed


@requires_redis
def test_real_lua_subsecond_remaining_reports_one_second_and_then_expires(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 5)
    _last_subsecond(redis, limiter, ip)
    blocked = limiter.begin_attempt(ip)
    assert not blocked.allowed
    assert blocked.retry_after == 1
    time.sleep(0.6)
    assert limiter.begin_attempt(ip).allowed


@requires_redis
def test_real_lua_admission_near_expiry_does_not_restart_window(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 4)
    key = _last_subsecond(redis, limiter, ip)
    assert limiter.begin_attempt(ip).allowed
    assert 0 < int(redis.command('PTTL', key)) <= 450


@requires_redis
def test_real_lua_failure_settlement_near_expiry_does_not_restart_window(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 4)
    attempt = limiter.begin_attempt(ip)
    key = _last_subsecond(redis, limiter, ip)
    state = limiter.finish_attempt(ip, attempt.reservation_id, success=False)
    assert not state.allowed
    assert state.retry_after == 1
    assert 0 < int(redis.command('PTTL', key)) <= 450


@requires_redis
def test_real_lua_check_and_legacy_increment_preserve_subsecond_window(redis_limiter):
    redis, limiter, ip = redis_limiter
    _fail(limiter, ip, 4)
    key = _last_subsecond(redis, limiter, ip)
    assert limiter.record_failure(ip).retry_after == 1
    assert limiter.check(ip).retry_after == 1
    assert 0 < int(redis.command('PTTL', key)) <= 450


@requires_redis
@pytest.mark.skipif(not SOCKET, reason='URL factory check requires the Unix-socket runner')
def test_real_lua_client_factory_connects_through_unix_socket(redis_limiter):
    redis_client, _, ip = redis_limiter
    limiter = LoginFailureLimiter(
        redis_url=f'unix://{SOCKET}?db=0',
        limit=5,
        window_seconds=60,
    )
    try:
        attempt = limiter.begin_attempt(ip)
        assert attempt.allowed
        state = limiter.finish_attempt(ip, attempt.reservation_id, success=False)
        assert state.allowed
        assert redis_client.command('HGET', limiter._key(ip), 'failures') == '1'
    finally:
        if limiter._redis is not None:
            limiter._redis.close()
