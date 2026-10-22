"""圆桌预检必须用当前读确认总线独立事务写入的账号账本行。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Query, Session, sessionmaker

from app.agents.discussion_bus import DiscussionBus
from app.api.v1 import discussion as discussion_api
from app.api.v1 import ws_discussion
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.roundtable import RoundtableSession
from app.models.user import User


class _RepeatableReadSession:
    """模拟 RR 一致性读旧快照、FOR UPDATE 当前读看见已提交行。"""

    def __init__(self, session: Session) -> None:
        self._session = session
        self.ledger_reads: list[str] = []

    def query(self, entity, *entities, **kwargs):
        query = self._session.query(entity, *entities, **kwargs)
        if entity is RoundtableSession:
            return _RoundtableLedgerQuery(query, self)
        return query

    def __getattr__(self, name: str):
        return getattr(self._session, name)


class _RoundtableLedgerQuery:
    def __init__(self, query: Query, owner: _RepeatableReadSession) -> None:
        self._query = query
        self._owner = owner
        self._is_current_read = False

    def filter_by(self, **kwargs):
        self._query = self._query.filter_by(**kwargs)
        return self

    def filter(self, *criteria):
        self._query = self._query.filter(*criteria)
        return self

    def with_for_update(self, *args, **kwargs):
        self._is_current_read = True
        self._query = self._query.with_for_update(*args, **kwargs)
        return self

    def populate_existing(self):
        self._query = self._query.populate_existing()
        return self

    def one_or_none(self):
        if not self._is_current_read:
            self._owner.ledger_reads.append("snapshot-miss")
            return None
        self._owner.ledger_reads.append("current-read")
        return self._query.one_or_none()


class _PersistentBus(DiscussionBus):
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        foreign_owner_id: int | None = None,
        concurrent_terminal: tuple[str, str] | None = None,
    ):
        super().__init__()
        self.enable_persistence(session_factory)
        self._foreign_owner_id = foreign_owner_id
        self._concurrent_terminal = concurrent_terminal

    def create_session(self, *args, **kwargs):
        session = super().create_session(*args, **kwargs)
        if self._foreign_owner_id is not None:
            with self._session_maker().begin() as ledger_db:
                row = ledger_db.get(RoundtableSession, session.session_id)
                assert row is not None
                row.owner_user_id = self._foreign_owner_id
        if self._concurrent_terminal is not None:
            status, terminal_status = self._concurrent_terminal
            now = datetime.now(timezone.utc)
            with self._session_maker().begin() as ledger_db:
                row = ledger_db.get(RoundtableSession, session.session_id)
                assert row is not None
                row.status = status
                row.progress = {
                    **dict(row.progress or {}),
                    "phase": "completed" if terminal_status == "success" else terminal_status,
                    "terminal_status": terminal_status,
                    "terminal_error": "committed by concurrent worker",
                    "concurrent_terminal_marker": "preserve-this-write",
                }
                row.closed_at = now
                row.updated_at = now
        return session


def _authorized_owner(db: Session) -> tuple[User, Project, CodeFile]:
    owner = User(username="roundtable-preflight-owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(
        user_id=owner.id,
        project_name="roundtable-preflight-project",
        language="python",
        status="active",
    )
    db.add(project)
    db.flush()
    code_file = CodeFile(
        project_id=project.id,
        file_name="sample.py",
        file_path="sample.py",
        language="python",
        content="print('ok')\n",
        size_bytes=12,
        line_count=1,
        status="active",
    )
    db.add(code_file)
    db.commit()
    db.refresh(owner)
    db.refresh(project)
    db.refresh(code_file)
    return owner, project, code_file


def _run_preflight(
    db,
    monkeypatch,
    *,
    foreign_owner_id: int | None = None,
    concurrent_terminal: tuple[str, str] | None = None,
):
    owner, project, code_file = _authorized_owner(db)
    session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    bus = _PersistentBus(
        session_factory,
        foreign_owner_id=foreign_owner_id,
        concurrent_terminal=concurrent_terminal,
    )
    rr_db = _RepeatableReadSession(db)
    registered: list[dict] = []
    monkeypatch.setattr(discussion_api, "_bus_for_db", lambda _db: bus)
    monkeypatch.setattr(discussion_api, "register_pending", lambda **kwargs: registered.append(kwargs))
    monkeypatch.setattr(
        "app.services.rbac_service.check_permission",
        lambda _db, user_id, permission: user_id == owner.id,
    )

    result = discussion_api.start_discussion(
        project_id=project.id,
        file_id=code_file.id,
        review_type="full",
        db=rr_db,
        user=owner,
    )
    return owner, result, bus, rr_db, registered, session_factory


@pytest.mark.parametrize(
    ("status", "terminal_status"),
    [("cancelled", "cancelled"), ("failed", "failed"), ("concluded", "success")],
)
def test_preflight_current_read_does_not_overwrite_concurrent_terminal(
    db, monkeypatch, status, terminal_status,
):
    owner, project, code_file = _authorized_owner(db)
    session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    bus = _PersistentBus(
        session_factory,
        concurrent_terminal=(status, terminal_status),
    )
    rr_db = _RepeatableReadSession(db)
    registered: list[dict] = []
    monkeypatch.setattr(discussion_api, "_bus_for_db", lambda _db: bus)
    monkeypatch.setattr(discussion_api, "register_pending", lambda **kwargs: registered.append(kwargs))
    monkeypatch.setattr(discussion_api, "_fail_pending_discussion", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.rbac_service.check_permission",
        lambda _db, user_id, permission: user_id == owner.id,
    )

    try:
        result = discussion_api.start_discussion(
            project_id=project.id,
            file_id=code_file.id,
            review_type="full",
            db=rr_db,
            user=owner,
        )
    except RuntimeError:
        result = None

    assert rr_db.ledger_reads == ["current-read"]
    assert registered == []

    with session_factory() as ledger_db:
        row = ledger_db.query(RoundtableSession).one()

    assert row.status == status
    assert row.progress["phase"] == ("completed" if terminal_status == "success" else terminal_status)
    assert row.progress["terminal_status"] == terminal_status
    assert row.progress["terminal_error"] == "committed by concurrent worker"
    assert row.progress["concurrent_terminal_marker"] == "preserve-this-write"
    assert row.closed_at is not None

    session_id, session = next(iter(bus._sessions.items()))
    assert result is None or result.data.get("status") != "pending"
    assert session.session_id == session_id
    assert session.status == status
    assert session.terminal_status == terminal_status
    assert session.progress["concurrent_terminal_marker"] == "preserve-this-write"
    assert session.cancel_event.is_set()


def test_preflight_current_read_persists_owner_and_complete_pending_progress(db, monkeypatch):
    owner, result, bus, rr_db, registered, session_factory = _run_preflight(db, monkeypatch)

    assert rr_db.ledger_reads == ["current-read"]
    assert len(registered) == 1
    assert registered[0]["user_id"] == owner.id

    with session_factory() as ledger_db:
        row = ledger_db.get(RoundtableSession, result.data["session_id"])

    assert row is not None
    assert row.owner_user_id == owner.id
    assert row.status == "pending"
    assert row.agents == result.data["agents"]
    assert row.progress == bus.get_session(result.data["session_id"]).progress
    assert row.progress == {
        **row.progress,
        "phase": "pending",
        "stage": "pending",
        "stage_message": "等待圆桌连接启动；当前任务已排队，尚未消耗 Agent 讨论调用",
        "completed_units": 0,
        "total_units": result.data["expected_turns"],
        "expected_turns": result.data["expected_turns"],
    }


def test_preflight_current_read_does_not_accept_another_owners_ledger_row(db, monkeypatch):
    owner, project, code_file = _authorized_owner(db)
    foreign_owner = User(username="roundtable-preflight-foreign", password="x", role="user", status=1)
    db.add(foreign_owner)
    db.commit()
    session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    bus = _PersistentBus(session_factory, foreign_owner_id=foreign_owner.id)
    rr_db = _RepeatableReadSession(db)
    registered: list[dict] = []
    monkeypatch.setattr(discussion_api, "_bus_for_db", lambda _db: bus)
    monkeypatch.setattr(discussion_api, "register_pending", lambda **kwargs: registered.append(kwargs))
    monkeypatch.setattr(
        "app.services.rbac_service.check_permission",
        lambda _db, user_id, permission: user_id == owner.id,
    )
    monkeypatch.setattr(discussion_api, "_fail_pending_discussion", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="圆桌预检会话未能写入当前账号账本"):
        discussion_api.start_discussion(
            project_id=project.id,
            file_id=code_file.id,
            review_type="full",
            db=rr_db,
            user=owner,
        )

    assert rr_db.ledger_reads == ["current-read"]
    assert registered == []


@pytest.mark.parametrize("report_status", ["cancelled", "success", "failed"])
def test_late_failure_preserves_committed_report_terminal(report_status):
    """A late failure must publish the row-locked report terminal, not its own status."""
    session = SimpleNamespace(
        session_id="roundtable-cancel-race",
        owner_user_id=71,
        task_id=812,
        report_task_id=812,
        terminal_status="",
    )
    emitted: list[tuple[str, dict]] = []
    task = SimpleNamespace(status=report_status, error_message=f"committed {report_status}")

    class _Query:
        def filter_by(self, **_kwargs):
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            return task

    class _Db:
        def query(self, _model):
            return _Query()

    class _Transaction:
        def __enter__(self):
            return _Db()

        def __exit__(self, *_exc):
            return False

    class _Maker:
        def begin(self):
            return _Transaction()

    class _Bus:
        _persist = True

        def _session_maker(self):
            return _Maker()

        def get_session(self, _session_id):
            return session

        def publish_control(self, _session_id, action, payload):
            emitted.append((action, payload))

        def close_session(self, _session_id):
            emitted.append(("session_closed", {}))

    ws_discussion._fail_pending_discussion(
        _Bus(), session.session_id, "late worker error", status="failed",
    )

    assert emitted == [
        ("done", {
            "task_id": 812,
            "status": report_status,
            "error": f"committed {report_status}",
        }),
        ("session_closed", {}),
    ]
