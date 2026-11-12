"""A revoked JWT must not authorize a delayed Studio or pentest write on MySQL RR."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
import test_mysql_reviewer_refund_approval as cases
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.api.v1.agent_studio import require_studio_role
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import ForbiddenError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.core.security import create_access_token
from app.main import app
from app.models.user import User
from app.services import agent_studio_service, pentest_service, rbac_service, remote_pentest_service

fresh_chain = cases.fresh_chain
fresh_chain_url = cases.fresh_chain_url


def _setup(fresh_chain, monkeypatch, kind):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    monkeypatch.setattr(settings, "pentest_enabled", True)
    monkeypatch.setattr(pentest_service, "engagement_to_dict", lambda db, row, **_: {"status": row.status})
    monkeypatch.setattr(remote_pentest_service, "_to_dict", lambda db, row, **_: {"status": row.status})
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    reviewer_id, _, personal_id = cases._seed(sessions)
    target_id = cases._seed_remote(sessions, reviewer_id) if kind == "remote" else personal_id
    token = create_access_token(reviewer_id, "reviewer", token_version=0)
    return sessions, reviewer_id, target_id, token


def _authorized_actor(db, token, kind):
    actor = authenticate_access_token(token, db)
    if kind == "studio":
        require_studio_role(db=db, user=actor)
        permission = PermissionCode.AGENT_ASSET_CREATE
    else:
        permission = PermissionCode.PENTEST_AUTHORIZE
    require_permission(permission)(user=actor, db=db)
    return actor


def _write(db, actor, kind, target_id):
    if kind == "studio":
        return agent_studio_service.create_agent(
            db, actor, code=f"token_fence_{actor.id}", name="test", description="test", prompt="test",
            review_focus="test", model_config={"temperature": 0.2, "max_tokens": 1024},
        )
    if kind == "remote":
        return remote_pentest_service.authorize_task(db, actor, target_id)
    return pentest_service.authorize_engagement(db, actor, target_id)


@pytest.mark.parametrize("kind", ["studio", "personal", "remote"])
def test_token_revoke_first_rejects_write_from_old_jwt_transaction(fresh_chain, monkeypatch, kind):
    sessions, reviewer_id, target_id, token = _setup(fresh_chain, monkeypatch, kind)
    with sessions() as stale:
        actor = _authorized_actor(stale, token, kind)
        with sessions.begin() as revoking:
            user = revoking.query(User).filter_by(id=reviewer_id).with_for_update().one()
            user.token_version = int(user.token_version or 0) + 1
        with pytest.raises((ForbiddenError, pentest_service.PentestAccessError)):
            _write(stale, actor, kind, target_id)


@pytest.mark.parametrize("kind", ["studio", "personal", "remote"])
def test_authorized_write_first_fences_concurrent_token_revoke(fresh_chain, monkeypatch, kind):
    sessions, reviewer_id, target_id, token = _setup(fresh_chain, monkeypatch, kind)
    locked, release, started = Event(), Event(), Event()
    revoke_thread, revoke_connection = [], []
    original = rbac_service.check_permission_grant

    def pause_after_authority_lock(db, user_id, permission):
        allowed = original(db, user_id, permission)
        locked.set()
        assert release.wait(10)
        return allowed

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        if revoke_thread and get_ident() == revoke_thread[0] and "FOR UPDATE" in statement.upper():
            if not revoke_connection:
                revoke_connection.append(conn.connection.driver_connection.thread_id())
                started.set()

    def writer():
        with sessions() as db:
            return _write(db, _authorized_actor(db, token, kind), kind, target_id)

    def revoke():
        revoke_thread.append(get_ident())
        with sessions.begin() as db:
            user = db.query(User).filter_by(id=reviewer_id).with_for_update().one()
            user.token_version = int(user.token_version or 0) + 1

    monkeypatch.setattr(rbac_service, "check_permission_grant", pause_after_authority_lock)
    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(writer)
            assert locked.wait(10)
            second = pool.submit(revoke)
            assert started.wait(10)
            deadline = monotonic() + 8
            observed = False
            while monotonic() < deadline:
                with fresh_chain.connect() as conn:
                    observed = bool(conn.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID = w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID = :connection_id LIMIT 1"
                    ), {"connection_id": revoke_connection[0]}).first())
                if observed:
                    break
                sleep(0.05)
            assert observed, "JWT 撤销必须等待当前写入释放账号锁"
            release.set()
            assert first.result(timeout=10)
            second.result(timeout=10)
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", observe)


@pytest.mark.parametrize("kind", ["studio", "personal", "remote"])
def test_http_old_jwt_is_rejected_if_revoked_after_authentication(fresh_chain, monkeypatch, kind):
    sessions, reviewer_id, target_id, token = _setup(fresh_chain, monkeypatch, kind)
    reached_fence, release = Event(), Event()
    original = rbac_service.check_paid_permission_fenced

    def paused_fence(db, user_id, permission, *, expected_token_version=None):
        assert expected_token_version == 0
        reached_fence.set()
        assert release.wait(10)
        return original(db, user_id, permission, expected_token_version=expected_token_version)

    def override_db():
        with sessions() as db:
            yield db

    monkeypatch.setattr(rbac_service, "check_paid_permission_fenced", paused_fence)
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    if kind == "studio":
        path = "/api/agent-studio/agents"
        payload = {"code": f"token_http_{reviewer_id}", "name": "test", "description": "test",
                   "prompt": "A valid prompt long enough for the API.", "review_focus": "test",
                   "model_config_json": {"temperature": 0.2, "max_tokens": 1024}}
    elif kind == "personal":
        path, payload = f"/api/pentest/engagements/{target_id}/authorize", None
    else:
        path, payload = f"/api/remote-pentest/tasks/{target_id}/authorize", None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(client.post, path, json=payload, headers={"Authorization": f"Bearer {token}"})
            assert reached_fence.wait(10), "request did not reach the transaction fence"
            with sessions.begin() as revoking:
                user = revoking.query(User).filter_by(id=reviewer_id).with_for_update().one()
                user.token_version = int(user.token_version or 0) + 1
            release.set()
            assert request.result(timeout=10).status_code == 403
    finally:
        release.set()
        client.close()
        app.dependency_overrides.pop(get_db, None)
