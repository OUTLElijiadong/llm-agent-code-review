"""Independent local verification. No production credentials, requests, or database access."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path('/Users/li/Documents/代码程序/基于大模型智能体的代码审查平台')
sys.path.insert(0, str(ROOT / 'backend'))
from app.api.v1 import auth as api
from app.core import rate_limit
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.schemas.auth import RegisterIn
from pydantic import ValidationError

RESULTS = {}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    now = [1000.0]
    monkeypatch.setattr(rate_limit, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(api, 'login_failure_limiter', rate_limit.LoginFailureLimiter(redis_url='', limit=5, window_seconds=60))
    factory = sessionmaker(bind=create_engine(f'sqlite:///{tmp_path / "local.sqlite"}', connect_args={'check_same_thread': False}), expire_on_commit=False)
    Base.metadata.create_all(factory.kw['bind'])
    with factory() as db:
        db.add(User(id=501, username='isolated-login-proof', password=hash_password('isolated-correct-password'), role='user', status=1))
        db.commit()
    app = FastAPI()
    register_handlers(app)
    app.include_router(api.router, prefix='/api/auth')
    def get_local_db():
        with factory() as db: yield db
    app.dependency_overrides[get_db] = get_local_db
    actual_login = api.auth_service.login
    calls = []
    def counted(*args, **kwargs):
        calls.append(True)
        return actual_login(*args, **kwargs)
    monkeypatch.setattr(api.auth_service, 'login', counted)
    client = TestClient(app, client=('203.0.113.51', 50000))
    log = []
    def submit(offset, *, correct=False, target=client, headers=None):
        now[0] = 1000 + offset
        r = target.post('/api/auth/login', json={'username': 'isolated-login-proof', 'password': 'isolated-correct-password' if correct else 'isolated-wrong-password'}, headers=headers)
        log.append({'seconds_from_first': offset, 'correct_password': correct, 'http': r.status_code, 'retry_after': r.headers.get('retry-after'), 'authentication_calls': len(calls)})
        return r
    yield SimpleNamespace(submit=submit, now=now, app=app, factory=factory, client=client, log=log, calls=calls)
    client.close()
    factory.kw['bind'].dispose()


def fail_five(h):
    for t in (0, 2, 4, 6, 8): assert h.submit(t).status_code == 401


def test_same_window_correct_password_never_reaches_auth(harness):
    h = harness
    fail_five(h)
    assert h.submit(10).status_code == 429
    assert h.submit(10.01, correct=True).status_code == 429
    assert h.submit(30, correct=True).headers['retry-after'] == '30'
    assert len(h.calls) == 5
    RESULTS['same_window'] = h.log


def test_sixth_reject_near_expiry_then_correct_011_seconds_later_is_allowed(harness):
    h = harness
    fail_five(h)
    assert h.submit(59.9).headers['retry-after'] == '1'
    success = h.submit(60.01, correct=True)
    assert success.status_code == 200 and success.json()['data']['access_token']
    assert len(h.calls) == 6
    RESULTS['crossed_original_window_boundary'] = h.log


def test_changed_real_source_ip_is_an_independent_bucket_but_forged_header_is_not(harness):
    h = harness
    fail_five(h)
    assert h.submit(10, correct=True, headers={'X-Real-IP': '203.0.113.52', 'X-Forwarded-For': '203.0.113.52'}).status_code == 429
    other = TestClient(h.app, client=('203.0.113.52', 50001))
    try: assert h.submit(10.01, correct=True, target=other).status_code == 200
    finally: other.close()
    RESULTS['source_ip_scope'] = h.log


def test_success_before_capacity_clears_previous_failures(harness):
    h = harness
    for offset in (0, 2, 4, 6): assert h.submit(offset).status_code == 401
    assert h.submit(8, correct=True).status_code == 200
    for offset in (10, 12, 14, 16, 18): assert h.submit(offset).status_code == 401
    assert h.submit(20).status_code == 429
    RESULTS['admitted_success_clears'] = h.log


def test_password_rules_are_backend_enforced_but_accept_common_six_digits(tmp_path, monkeypatch):
    for password in ('12345', 'a' * 33):
        with pytest.raises(ValidationError): RegisterIn(username='isolated-register', password=password)
    payload = RegisterIn(username='isolated-register', password='123456')
    monkeypatch.setattr(settings, 'beta_registration_enabled', False)
    engine = create_engine(f'sqlite:///{tmp_path / "registration.sqlite"}')
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        user = api.auth_service.register(db, payload)
        assert user.password != payload.password
        assert verify_password('123456', user.password)
        assert db.query(User).count() == 1
    engine.dispose()
    RESULTS['password_policy'] = {'server_5_chars_rejected': True, 'server_33_chars_rejected': True, 'server_123456_registration_accepted': True, 'stored_bcrypt_hash_not_plaintext': True, 'database': 'isolated_sqlite', 'beta_invite_disabled_only_in_local_test': True}


def test_cold_worker_redis_outage_can_admit_despite_other_worker_limit():
    spec = importlib.util.spec_from_file_location('existing_redis_fake', ROOT / 'backend/tests/unit/core/test_login_rate_limit.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shared = module._AtomicRedis()
    first = rate_limit.LoginFailureLimiter(redis_url='redis://isolated.invalid', limit=5, window_seconds=60, redis_client=shared)
    cold = rate_limit.LoginFailureLimiter(redis_url='redis://isolated.invalid', limit=5, window_seconds=60, redis_client=shared)
    ip = '203.0.113.53'
    for _ in range(5):
        attempt = first.begin_attempt(ip)
        first.finish_attempt(ip, attempt.reservation_id, success=False)
    assert not first.begin_attempt(ip).allowed
    shared.fail = True
    assert not first.begin_attempt(ip).allowed
    assert cold.begin_attempt(ip).allowed
    RESULTS['redis_cold_worker_outage'] = {'shared_redis_5_failures': True, 'warm_worker_denied_after_outage': True, 'cold_worker_admitted_after_outage': True, 'scope': 'isolated_injected_redis_outage_not_production_observation'}


class ContainerRedis:
    def command(self, *args):
        r = subprocess.run(['docker', 'exec', 'prism-login-audit-20260908', 'redis-cli', '-s', '/tmp/redis.sock', '--raw', *map(str, args)], capture_output=True, check=True, timeout=10)
        return r.stdout.decode().strip()
    def eval(self, script, count, key, *args):
        return [int(x) for x in self.command('EVAL', script, count, key, *args).splitlines()]


def test_real_redis_lua_denies_same_window_correct_request_before_auth(harness, monkeypatch):
    h = harness
    redis = ContainerRedis()
    limiter = rate_limit.LoginFailureLimiter(redis_url='redis://isolated-container', limit=5, window_seconds=60, redis_client=redis)
    monkeypatch.setattr(api, 'login_failure_limiter', limiter)
    fail_five(h)
    assert h.submit(10).status_code == 429
    assert h.submit(10.01, correct=True).status_code == 429
    assert len(h.calls) == 5
    RESULTS['real_redis_same_window'] = {'events': h.log, 'redis_version': redis.command('INFO', 'server').split('redis_version:')[1].splitlines()[0], 'clock_note': 'event offsets only drive local shadow clock; Redis uses its actual clock, so Retry-After remains near60', 'isolation': 'local_network_none_unix_socket'}


def test_real_redis_ttl_zero_reports_full_window_then_expires():
    redis = ContainerRedis()
    limiter = rate_limit.LoginFailureLimiter(redis_url='redis://isolated-container', limit=5, window_seconds=60, redis_client=redis)
    ip = '203.0.113.54'
    for _ in range(5):
        a = limiter.begin_attempt(ip)
        limiter.finish_attempt(ip, a.reservation_id, success=False)
    key = limiter._key(ip)
    assert redis.command('PEXPIRE', key, 450) == '1'
    pttl = int(redis.command('PTTL', key))
    assert 0 < pttl <= 450
    denied = limiter.begin_attempt(ip)
    assert denied.allowed is False and denied.retry_after == 60
    time.sleep(0.6)
    admitted = limiter.begin_attempt(ip)
    assert admitted.allowed is True
    RESULTS['real_redis_subsecond_expiry'] = {'pttl_ms_before_reject': pttl, 'rejected_retry_after_seconds': denied.retry_after, 'admitted_after_sleep_seconds': 0.6, 'ttl_injection': 'PEXPIRE on isolated test key to reach final450ms of existing60s window', 'production_redis_modified': False}


def test_z_write_sanitized_evidence():
    assert len(RESULTS) == 8
    Path('/tmp/login_security_review_20260908_scenarios.json').write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2) + '\n')
