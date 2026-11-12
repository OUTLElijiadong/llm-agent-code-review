"""The bounded audit worker never owns or blocks a caller's transaction."""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.database import Base
from app.models import load_all_models
from app.models.user import User
from app.services import project_access_audit
from app.services.project_access_audit import _DeferredAuditQueue, _Denial


def test_pending_audits_are_bounded_per_transaction(db, monkeypatch):
    reasons = []
    monkeypatch.setattr(project_access_audit, "_fallback", lambda reason, denial: reasons.append(reason))
    actor = SimpleNamespace(id=1, username="private-name")
    for _ in range(130):
        project_access_audit.record_project_denial(db, actor, 123, "none")
    assert len(db.info["project_denial_audit_pending"]) == 128
    assert reasons == ["session_pending_full", "session_pending_full"]


@pytest.mark.parametrize("url, expected", [
    ("sqlite://", True),
    ("sqlite:///:memory:", True),
    ("sqlite:///file::memory:?cache=shared&uri=true", True),
    ("sqlite:///file:auditmem?mode=memory&cache=shared&uri=true", True),
    ("sqlite:///file:audit.db?mode=rwc&uri=true", False),
])
def test_detect_memory_sqlite_urls(url, expected):
    engine = create_engine(url)
    try:
        assert project_access_audit._is_memory_sqlite(engine) is expected
    finally:
        engine.dispose()


@pytest.mark.parametrize("finish", ["rollback", "commit", "close"])
def test_memory_sqlite_audit_never_waits_for_occupied_pool(monkeypatch, finish):
    load_all_models()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.5)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add(User(id=1, username="private-name", password="x", role="user", status=1))
    held, release = Event(), Event()
    warnings = []
    monkeypatch.setattr(project_access_audit, "_fallback", lambda reason, denial: warnings.append(reason))

    def occupy():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            held.set()
            assert release.wait(5)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor, factory() as db:
            actor = db.get(User, 1)

            def occupy_before_audit(_session, transaction):
                if transaction.parent is None and not held.is_set():
                    executor.submit(occupy)
                    assert held.wait(3)

            event.listen(db, "after_transaction_end", occupy_before_audit)
            project_access_audit.record_project_denial(db, actor, 123, "none")
            start = monotonic()
            getattr(db, finish)()
            elapsed = monotonic() - start
            release.set()
            assert elapsed < 0.2
            assert warnings == ["sqlite_memory_not_persisted"]
    finally:
        release.set()
        engine.dispose()


def test_queue_capacity_and_bounded_shutdown_do_not_wait_for_writer():
    started, release = Event(), Event()
    written = []

    def writer(item):
        started.set()
        assert release.wait(5)
        written.append(item)

    queue = _DeferredAuditQueue(capacity=1, writer=writer)
    try:
        assert queue.submit("one")
        assert started.wait(2)
        assert queue.submit("two")
        start = monotonic()
        assert queue.submit("overflow") is False
        assert queue.shutdown(timeout=0.05) is False
        assert monotonic() - start < 0.5
    finally:
        release.set()
    assert queue.shutdown(timeout=2) is True
    assert written == ["one", "two"]


def test_writer_exception_is_observable_and_worker_continues(monkeypatch):
    finished = Event()
    failures = []

    def writer(item):
        if item == "bad":
            raise RuntimeError("secret input must not appear in log")
        finished.set()

    monkeypatch.setattr("app.services.project_access_audit._report_worker_failure", failures.append)
    queue = _DeferredAuditQueue(capacity=2, writer=writer)
    assert queue.submit("bad")
    assert queue.submit("good")
    assert finished.wait(2)
    assert queue.shutdown(timeout=2) is True
    assert failures == ["RuntimeError"]


def test_resume_after_shutdown_starts_a_new_worker():
    processed = []
    queue = _DeferredAuditQueue(capacity=2, writer=processed.append)
    assert queue.submit("before")
    assert queue.shutdown(timeout=2) is True
    assert queue.submit("stopped") is False
    queue.resume()
    assert queue.submit("after")
    assert queue.shutdown(timeout=2) is True
    assert processed == ["before", "after"]


def test_resume_after_timed_out_shutdown_reuses_the_running_worker():
    entered, release = Event(), Event()
    processed = []

    def writer(item):
        if item == "before":
            entered.set()
            assert release.wait(5)
        processed.append(item)

    queue = _DeferredAuditQueue(capacity=2, writer=writer)
    try:
        assert queue.submit("before")
        assert entered.wait(2)
        assert queue.shutdown(timeout=0.05) is False
        queue.resume()
        assert queue.submit("after")
    finally:
        release.set()
    assert queue.shutdown(timeout=2) is True
    assert processed == ["before", "after"]


def test_database_failure_reports_sanitized_minimum_facts(monkeypatch):
    finished = Event()
    fallbacks = []
    errors = []

    def writer(_item):
        raise RuntimeError("secret database URL and private contents")

    def fallback(reason, denial):
        fallbacks.append((reason, denial.actor_id, denial.project_id))
        finished.set()

    monkeypatch.setattr("app.services.project_access_audit._report_worker_failure", errors.append)
    monkeypatch.setattr("app.services.project_access_audit._fallback", fallback)
    queue = _DeferredAuditQueue(capacity=1, writer=writer)
    denial = _Denial(actor_id=1, actor_name="user", project_id=2, role="none", created_at=datetime.utcnow())
    assert queue.submit((None, denial))
    assert finished.wait(2)
    assert queue.shutdown(timeout=2) is True
    assert errors == ["RuntimeError"]
    assert fallbacks == [("database_write_failed", 1, 2)]


def test_process_can_exit_after_bounded_shutdown_with_stuck_writer():
    code = """
from threading import Event
from app.services.project_access_audit import _DeferredAuditQueue
started, blocked = Event(), Event()
def writer(_):
    started.set()
    blocked.wait()
queue = _DeferredAuditQueue(capacity=1, writer=writer)
assert queue.submit('pending')
assert started.wait(2)
assert not queue.shutdown(timeout=0.05)
print('bounded-exit')
"""
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=3, check=False)
    assert completed.returncode == 0, completed.stderr
    assert "bounded-exit" in completed.stdout
