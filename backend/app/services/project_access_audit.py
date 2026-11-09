"""Record denied project writes without taking ownership of caller transactions.

MySQL/file-backed audits are eventually consistent and bounded; they are not a
durable queue. A process crash or an unavailable audit store can lose pending
database entries. Sanitized fallback logs make those cases observable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from queue import Empty, Full, Queue
from threading import Lock, Thread
from typing import Callable

from loguru import logger
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.audit_log import AuditLog

_PENDING_KEY = "project_denial_audit_pending"
_LISTENER_KEY = "project_denial_audit_listener"
_SESSION_PENDING_LIMIT = 128


@dataclass(frozen=True)
class _Denial:
    actor_id: int
    actor_name: str | None
    project_id: int
    role: str
    created_at: datetime


def _report_worker_failure(error_type: str) -> None:
    # Do not print database URLs, SQL, source content or raw exception text.
    logger.error("[project-denial-audit] 数据库审计写入失败 error_type={}", error_type)


def _persist(item: tuple[Engine, _Denial]) -> None:
    engine, denial = item
    role_label = "审查员" if denial.role == "reviewer" else "只读成员"
    detail = (f"无项目访问权限被拒绝 project_id={denial.project_id}" if denial.role == "none"
              else f"{role_label}写权限被拒绝 project_id={denial.project_id}")
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as work:
        work.add(AuditLog(
            actor_id=denial.actor_id,
            actor_name=denial.actor_name,
            action="project_access",
            target_type="project",
            target_id=str(denial.project_id),
            detail=detail,
            status="failed",
            create_time=denial.created_at,
        ))
        work.commit()


class _DeferredAuditQueue:
    """One daemon worker, a bounded queue and no caller Session ownership."""

    def __init__(self, *, capacity: int, writer: Callable) -> None:
        self._queue = Queue(maxsize=capacity)
        self._writer = writer
        self._lock = Lock()
        self._stopping = False
        self._thread: Thread | None = None

    def submit(self, item) -> bool:
        with self._lock:
            if self._stopping:
                return False
            if self._thread is None or not self._thread.is_alive():
                self._thread = Thread(target=self._run, name="project-denial-audit", daemon=True)
                try:
                    self._thread.start()
                except Exception as exc:
                    self._thread = None
                    _report_worker_failure(type(exc).__name__)
                    return False
            try:
                self._queue.put_nowait(item)
            except Full:
                return False
            return True

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except Empty:
                with self._lock:
                    if self._stopping and self._queue.empty():
                        self._thread = None
                        return
                continue
            try:
                self._writer(item)
            except Exception as exc:  # audit failure cannot alter the denied operation
                _report_worker_failure(type(exc).__name__)
                if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], _Denial):
                    _fallback("database_write_failed", item[1])
            finally:
                self._queue.task_done()

    def resume(self) -> None:
        with self._lock:
            self._stopping = False

    def shutdown(self, *, timeout: float) -> bool:
        with self._lock:
            self._stopping = True
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()


_queue = _DeferredAuditQueue(capacity=128, writer=_persist)


def start_project_denial_audit() -> None:
    _queue.resume()


def stop_project_denial_audit(*, timeout: float = 2.0) -> None:
    if not _queue.shutdown(timeout=timeout):
        logger.warning("[project-denial-audit] 停机时仍有未完成审计，可能丢失")


def _fallback(reason: str, denial: _Denial) -> None:
    logger.warning("[project-denial-audit] 未能登记数据库审计 reason={} actor_id={} project_id={} role={}",
                   reason, denial.actor_id, denial.project_id, denial.role)


def _is_memory_sqlite(engine: Engine) -> bool:
    if engine.dialect.name != "sqlite":
        return False
    database = engine.url.database or ""
    return (database in ("", ":memory:") or database.startswith("file::memory:")
            or (database.startswith("file:") and engine.url.query.get("mode") == "memory"))


def _after_transaction_end(session: Session, transaction) -> None:
    if transaction.parent is not None:
        return
    pending = session.info.pop(_PENDING_KEY, [])
    if not pending:
        return
    try:
        bind = session.get_bind()
    except Exception as exc:
        _report_worker_failure(type(exc).__name__)
        for denial in pending:
            _fallback("binding_unavailable", denial)
        return
    if isinstance(bind, Connection) or not isinstance(bind, Engine):
        # A Session bound to an externally managed Connection has not proved
        # its owner's transaction ended. Never write through that connection.
        for denial in pending:
            _fallback("external_connection", denial)
        return
    for denial in pending:
        if _is_memory_sqlite(bind):
            # A thread-local in-memory database cannot be reliably shared with
            # a worker, and checking out a connection here can block callers.
            _fallback("sqlite_memory_not_persisted", denial)
        elif not _queue.submit((bind, denial)):
            _fallback("queue_full_or_stopping", denial)


def record_project_denial(db: Session, user, project_id: int, role: str) -> None:
    """Snapshot minimal facts, and persist only after the outer transaction ends."""
    denial = _Denial(
        actor_id=int(user.id),
        actor_name=str(user.username)[:80] if user.username else None,
        project_id=int(project_id),
        role=role if role in {"none", "viewer", "reviewer"} else "other",
        created_at=datetime.utcnow(),
    )
    # This sanitized event remains observable even if the process exits
    # before the deferred database write can run.
    logger.warning("[project-denial-audit] 拒绝项目写入 actor_id={} project_id={} role={}",
                   denial.actor_id, denial.project_id, denial.role)
    bind = db.get_bind()
    if isinstance(bind, Connection) or not isinstance(bind, Engine):
        _fallback("external_connection", denial)
        return
    pending = db.info.setdefault(_PENDING_KEY, [])
    if len(pending) >= _SESSION_PENDING_LIMIT:
        _fallback("session_pending_full", denial)
        return
    pending.append(denial)
    if not db.info.get(_LISTENER_KEY):
        # Removing a listener during SQLAlchemy dispatch mutates its deque.
        # Install one reusable, session-scoped listener; it processes each
        # pending batch once and is discarded with the Session.
        event.listen(db, "after_transaction_end", _after_transaction_end)
        db.info[_LISTENER_KEY] = True
