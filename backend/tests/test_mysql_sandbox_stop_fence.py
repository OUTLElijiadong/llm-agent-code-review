"""MySQL RR ordering for sandbox stop versus request registration.

Run only with the isolated `fresh_chain` fixture; no real Worker is called.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.agent_capability import SandboxEnvironment, SandboxEvent, SandboxWorker
from app.models.project import Project
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import sandbox_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _seed(factory):
    suffix = uuid4().hex[:12]
    with factory.begin() as db:
        actor = User(username=f"sandbox-lock-{suffix}", password="x", role="user", status=1)
        db.add(actor)
        db.flush()
        project = Project(user_id=actor.id, project_name="sandbox lock", language="python", status="active")
        worker = SandboxWorker(code=f"lock-{suffix}", name="fixture", worker_type="local", transport="unix",
                               endpoint="/unused", supported_languages_json='["python"]',
                               supported_modes_json='["whitebox"]', runtime="runsc", status="healthy", enabled=1)
        db.add_all([project, worker])
        db.flush()
        env = SandboxEnvironment(
            public_id=f"sbx_{suffix}", project_id=project.id, owner_id=actor.id, worker_id=worker.id,
            agent_code="whitebox_tester", purpose="test", language="python", test_mode="whitebox",
            status="running", runtime="runsc", image_ref="image", source_sha256="a" * 64,
            execution_token="test-token", resource_policy_json="{}", agent_config_json="{}",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(env)
        db.flush()
        return actor.id, env.id, env.public_id


def _register(db, env, request_id):
    return sandbox_service._persist_worker_execution_snapshot(
        db, env, execution_bytes=b"fixture", source_sha256="b" * 64, repair_round=1,
        request_envelope={"request_id": request_id},
        request_config_json='{"worker_request_ids":["' + request_id + '"]}', execution_token="test-token")


def _stub_stop(monkeypatch, observed, reached=None, resume=None):
    monkeypatch.setattr(sandbox_service, "_get_visible",
                        lambda db, _actor, public_id: db.query(SandboxEnvironment).filter_by(public_id=public_id).one())
    monkeypatch.setattr(sandbox_service, "_can_manage", lambda *_args: True)
    monkeypatch.setattr(sandbox_service, "_publish_terminal_sandbox_report", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "environment_to_dict", lambda *_args: {})
    monkeypatch.setattr(sandbox_service.audit_service, "log", lambda *_args, **_kwargs: None)

    def stop(_worker, env):
        ids = sandbox_service._registered_worker_request_ids(env)
        observed.extend(ids)
        if reached is not None:
            reached.set()
            assert resume.wait(15)
        return {request_id: {"request_id": request_id, "status": "stopped",
                             "result": {"cleanup_confirmed": True}} for request_id in ids}

    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", stop)


def test_registration_first_blocks_stop_and_stop_sees_committed_request(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    observed, connection_id, entered = [], [], Event()
    _stub_stop(monkeypatch, observed)

    def stop():
        with factory() as db:
            connection_id.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            actor = db.get(User, actor_id)
            entered.set()
            sandbox_service.stop_environment(db, actor, public_id)

    with factory() as registration, ThreadPoolExecutor(max_workers=1) as pool:
        env = registration.get(SandboxEnvironment, env_id)
        assert _register(registration, env, f"{public_id}-r1")
        future = pool.submit(stop)
        try:
            assert entered.wait(15)
            deadline, locked = monotonic() + 10, False
            while monotonic() < deadline:
                with fresh_chain.connect() as connection:
                    locked = bool(connection.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"), {"cid": connection_id[0]}).first())
                if locked:
                    break
                sleep(0.05)
            assert locked, "stop did not wait on the registration row lock"
        finally:
            registration.commit()
        future.result(timeout=15)
    assert set(observed) == {public_id, f"{public_id}-r1"}
    with factory() as db:
        env = db.get(SandboxEnvironment, env_id)
        assert env.status == "stopped"
        event = db.query(SandboxEvent).filter_by(environment_id=env_id, stage="worker_stop_receipts").one()
        receipt = json.loads(event.payload_json)
        assert receipt["worker_id"] == env.worker_id
        assert receipt["execution_token_sha256"] == hashlib.sha256(b"test-token").hexdigest()
        assert {item["request_id"] for item in receipt["receipts"]} == set(observed)
        assert all(item["status"] == "stopped" and item["cleanup_confirmed"] is True
                   for item in receipt["receipts"])


def test_stop_first_rejects_registration_from_older_snapshot(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    observed, stopping, resume = [], Event(), Event()
    _stub_stop(monkeypatch, observed, stopping, resume)

    def stop():
        with factory() as db:
            sandbox_service.stop_environment(db, db.get(User, actor_id), public_id)

    with factory() as registration, ThreadPoolExecutor(max_workers=1) as pool:
        env = registration.get(SandboxEnvironment, env_id)  # RR snapshot before stop
        future = pool.submit(stop)
        try:
            assert stopping.wait(15)
            assert not _register(registration, env, f"{public_id}-r1")
        finally:
            resume.set()
        future.result(timeout=15)
    assert observed == [public_id]


def test_uncommitted_stop_forces_older_registration_to_wait_and_then_reject(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    observed, connection_id = [], []
    snapshot_ready, stop_locked, register_now, release_stop = Event(), Event(), Event(), Event()
    _stub_stop(monkeypatch, observed)

    def register():
        with factory() as db:
            env = db.get(SandboxEnvironment, env_id)  # establish an older RR snapshot
            connection_id.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            snapshot_ready.set()
            assert register_now.wait(15)
            return _register(db, env, f"{public_id}-r1")

    def stop():
        with factory() as db:
            original_commit = db.commit
            first_commit = True

            def commit_with_barrier():
                nonlocal first_commit
                if first_commit:
                    first_commit = False
                    db.flush()  # persist stopping but keep its row lock uncommitted
                    stop_locked.set()
                    assert release_stop.wait(15)
                original_commit()

            db.commit = commit_with_barrier
            sandbox_service.stop_environment(db, db.get(User, actor_id), public_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        registration = pool.submit(register)
        assert snapshot_ready.wait(15)
        stopping = pool.submit(stop)
        try:
            assert stop_locked.wait(15)
            register_now.set()
            deadline, locked = monotonic() + 10, False
            while monotonic() < deadline:
                with fresh_chain.connect() as connection:
                    locked = bool(connection.execute(text(
                        "SELECT 1 FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"), {"cid": connection_id[0]}).first())
                if locked:
                    break
                sleep(0.05)
            assert locked, "registration did not wait on the uncommitted stop"
        finally:
            release_stop.set()
        assert registration.result(timeout=15) is False
        stopping.result(timeout=15)
    assert observed == [public_id]
    with factory() as db:
        env = db.get(SandboxEnvironment, env_id)
        assert env.status == "stopped"
        assert sandbox_service._registered_worker_request_ids(env) == [public_id]


@pytest.mark.parametrize("execute_outcome", ["success", "error"])
def test_worker_observes_committed_stop_and_persists_receipt_on_mysql(fresh_chain, monkeypatch, execute_outcome):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    source_sha = hashlib.sha256(b"source").hexdigest()
    with factory.begin() as db:
        user_role = db.query(Role).filter_by(code="user", status="active").one()
        db.add(UserRole(user_id=actor_id, role_id=user_role.id))
        env = db.get(SandboxEnvironment, env_id)
        env.status = "queued"
        env.source_sha256 = source_sha
        env.agent_config_json = '{"worker_mode":"whitebox","remote_only":false}'

    calls = []

    def worker(_worker, _method, path, payload):
        calls.append(path)
        if path == "/execute":
            # A separate transaction requests stop while the worker HTTP call is in flight.
            with factory.begin() as db:
                row = (db.query(SandboxEnvironment).filter_by(id=env_id)
                       .populate_existing().with_for_update().one())
                row.status = "stopping"
            if execute_outcome == "error":
                raise RuntimeError("worker transmission failed")
            return {"result": {"request_id": public_id, "source_sha256": source_sha,
                               "status": "succeeded", "result": {"exit_code": 0},
                               "events": [], "last_sequence": 0}}
        assert path == "/stop"
        return {"result": {"request_id": payload["request_id"], "status": "stopped",
                           "result": {"cleanup_confirmed": True}}}

    monkeypatch.setattr(sandbox_service, "SessionLocal", factory)
    monkeypatch.setattr(sandbox_service, "_call_worker", worker)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_generate_agent_test_cases", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_generate_deployment_patch", lambda *_args, **_kwargs: None)
    sandbox_service._execute_environment(env_id, "c291cmNl", execution_token="test-token")

    with factory() as db:
        env = db.get(SandboxEnvironment, env_id)
        assert calls == ["/execute", "/stop"], f"status={env.status}, error={env.error!r}"
        assert env.status == "stopped"
        event = db.query(SandboxEvent).filter_by(environment_id=env_id, stage="worker_stop_receipts").one()
        receipt = json.loads(event.payload_json)
        assert receipt["worker_id"] == env.worker_id
        assert receipt["execution_token_sha256"] == hashlib.sha256(b"test-token").hexdigest()
        assert receipt["receipts"] == [{"request_id": public_id, "status": "stopped", "cleanup_confirmed": True}]


@pytest.mark.parametrize("bad_receipt", ["missing", "extra", "wrong_id", "not_clean", "not_terminal"])
def test_stop_rejects_incomplete_or_mismatched_worker_receipts(fresh_chain, monkeypatch, bad_receipt):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    observed = []
    _stub_stop(monkeypatch, observed)

    def bad_stop(_worker, env):
        ids = sandbox_service._registered_worker_request_ids(env)
        receipts = {request_id: {"request_id": request_id, "status": "stopped",
                                 "result": {"cleanup_confirmed": True}} for request_id in ids}
        if bad_receipt == "missing":
            receipts.pop(ids[0])
        elif bad_receipt == "extra":
            receipts["unrelated"] = {"request_id": "unrelated", "status": "stopped",
                                     "result": {"cleanup_confirmed": True}}
        elif bad_receipt == "wrong_id":
            receipts[ids[0]]["request_id"] = "unrelated"
        elif bad_receipt == "not_clean":
            receipts[ids[0]]["result"]["cleanup_confirmed"] = False
        else:
            receipts[ids[0]]["status"] = "running"
        return receipts

    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", bad_stop)
    with factory() as db:
        with pytest.raises(RuntimeError):
            sandbox_service.stop_environment(db, db.get(User, actor_id), public_id)
    with factory() as db:
        assert db.get(SandboxEnvironment, env_id).status == "stopping"
        assert db.query(SandboxEvent).filter_by(environment_id=env_id, stage="worker_stop_receipts").count() == 0


def test_stale_stop_cannot_regress_already_stopped_environment(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    observed = []
    _stub_stop(monkeypatch, observed)
    calls = []

    def stop_once(_worker, env):
        calls.append(env.id)
        if len(calls) > 1:
            raise RuntimeError("late duplicate worker stop failed")
        return {request_id: {"request_id": request_id, "status": "stopped",
                             "result": {"cleanup_confirmed": True}}
                for request_id in sandbox_service._registered_worker_request_ids(env)}

    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", stop_once)
    with factory() as stale:
        actor = stale.get(User, actor_id)
        assert stale.get(SandboxEnvironment, env_id).status == "running"  # establish RR snapshot
        with factory() as first:
            sandbox_service.stop_environment(first, first.get(User, actor_id), public_id)
        # A later duplicate must observe the terminal row and never call the worker again.
        sandbox_service.stop_environment(stale, actor, public_id)
    assert calls == [env_id]
    with factory() as db:
        assert db.get(SandboxEnvironment, env_id).status == "stopped"


def test_overlapping_stops_do_not_report_failure_after_other_stop_succeeds(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    observed, calls = [], []
    first_in_worker, release_first = Event(), Event()
    _stub_stop(monkeypatch, observed)

    def competing_stop(_worker, env):
        calls.append(env.id)
        if len(calls) == 1:
            first_in_worker.set()
            assert release_first.wait(15)
            raise RuntimeError("late worker error after other stop succeeded")
        return {request_id: {"request_id": request_id, "status": "stopped",
                             "result": {"cleanup_confirmed": True}}
                for request_id in sandbox_service._registered_worker_request_ids(env)}

    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", competing_stop)

    def stop():
        with factory() as db:
            return sandbox_service.stop_environment(db, db.get(User, actor_id), public_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(stop)
        try:
            assert first_in_worker.wait(15)
            second = pool.submit(stop)
            second.result(timeout=15)
        finally:
            release_first.set()
        first.result(timeout=15)
    with factory() as db:
        assert db.get(SandboxEnvironment, env_id).status == "stopped"
        assert db.query(SandboxEvent).filter_by(environment_id=env_id, stage="worker_stop_receipts").count() == 1


@pytest.mark.parametrize("invalid_evidence", ["missing", "wrong_token", "missing_request"])
def test_overlapping_stop_failure_cannot_use_invalid_success_evidence(fresh_chain, monkeypatch, invalid_evidence):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    actor_id, env_id, public_id = _seed(factory)
    observed, calls = [], []
    first_in_worker, release_first = Event(), Event()
    _stub_stop(monkeypatch, observed)
    with factory.begin() as db:
        assert _register(db, db.get(SandboxEnvironment, env_id), f"{public_id}-r1")

    def competing_stop(_worker, env):
        calls.append(env.id)
        if len(calls) == 1:
            first_in_worker.set()
            assert release_first.wait(15)
            raise RuntimeError("late worker stop failed")
        return {request_id: {"request_id": request_id, "status": "stopped",
                             "result": {"cleanup_confirmed": True}}
                for request_id in sandbox_service._registered_worker_request_ids(env)}

    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", competing_stop)

    def stop():
        with factory() as db:
            return sandbox_service.stop_environment(db, db.get(User, actor_id), public_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(stop)
        try:
            assert first_in_worker.wait(15)
            pool.submit(stop).result(timeout=15)
            with factory.begin() as db:
                event = db.query(SandboxEvent).filter_by(
                    environment_id=env_id, stage="worker_stop_receipts").one()
                if invalid_evidence == "missing":
                    db.delete(event)
                else:
                    payload = json.loads(event.payload_json)
                    if invalid_evidence == "wrong_token":
                        payload["execution_token_sha256"] = "0" * 64
                    else:
                        payload["receipts"] = [item for item in payload["receipts"]
                                               if item["request_id"] != f"{public_id}-r1"]
                    event.payload_json = json.dumps(payload)
        finally:
            release_first.set()
        with pytest.raises(RuntimeError, match="late worker stop failed"):
            first.result(timeout=15)
    with factory() as db:
        assert db.get(SandboxEnvironment, env_id).status == "stopped"
