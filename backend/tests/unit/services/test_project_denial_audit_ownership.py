"""A denied project write must not take ownership of the caller's transaction."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import NotFoundError
from app.models.audit_log import AuditLog
from app.models.project import Project
from app.models.user import User
from app.services import project_access_audit
from app.services.project_member_service import require_project_access


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as session:
        yield session
    project_access_audit._queue._queue.join()
    engine.dispose()


def _seed(db):
    owner = User(username="audit-owner", password="x", role="user", status=1)
    outsider = User(username="audit-outsider", password="x", role="user", status=1)
    db.add_all([owner, outsider])
    db.flush()
    project = Project(user_id=owner.id, project_name="protected", status="active")
    other = Project(user_id=owner.id, project_name="before", status="active")
    db.add_all([project, other])
    db.commit()
    return outsider.id, project.id, other.id


@pytest.mark.parametrize("nested", [False, True])
def test_denial_leaves_explicit_transaction_to_its_owner(db, nested):
    actor_id, project_id, other_id = _seed(db)
    with db.begin():
        actor = db.get(User, actor_id)
        other = db.get(Project, other_id)
        other.project_name = "outer change"
        db.flush()
        if nested:
            with pytest.raises(NotFoundError):
                with db.begin_nested():
                    require_project_access(db, project_id, actor, need_write=True, lock=True, nowait=True)
        else:
            with pytest.raises(NotFoundError):
                require_project_access(db, project_id, actor, need_write=True, lock=True, nowait=True)
        assert db.get(Project, other_id).project_name == "outer change"
    project_access_audit._queue._queue.join()
    assert db.get(Project, other_id).project_name == "outer change"
    assert db.query(AuditLog).filter_by(actor_id=actor_id, action="project_access", status="failed").count() == 1


@pytest.mark.parametrize("finish", ["rollback", "close"])
def test_denial_audit_survives_caller_rollback_or_close(db, finish):
    actor_id, project_id, other_id = _seed(db)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    with factory() as caller:
        actor = caller.get(User, actor_id)
        caller.get(Project, other_id).project_name = "must roll back"
        caller.flush()
        for _ in range(2):
            with pytest.raises(NotFoundError):
                require_project_access(caller, project_id, actor, need_write=True, lock=True, nowait=True)
        if finish == "rollback":
            caller.rollback()
    project_access_audit._queue._queue.join()
    db.expire_all()
    assert db.get(Project, other_id).project_name == "before"
    assert db.query(AuditLog).filter_by(actor_id=actor_id, action="project_access", status="failed").count() == 2


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_external_connection_is_never_committed_or_used_for_automatic_audit(db, monkeypatch, finish):
    actor_id, project_id, other_id = _seed(db)
    warnings = []
    monkeypatch.setattr(project_access_audit, "_fallback", lambda reason, denial: warnings.append((reason, denial)))
    engine = db.get_bind()
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Start the real SQLite writer transaction before the SAVEPOINT.
            connection.execute(Project.__table__.update().where(Project.id == other_id).values(project_name="pending"))
            factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False,
                                   join_transaction_mode="create_savepoint")
            with factory() as caller:
                actor = caller.get(User, actor_id)
                caller.get(Project, other_id).project_name = "caller-controlled"
                caller.flush()
                with pytest.raises(NotFoundError):
                    require_project_access(caller, project_id, actor, need_write=True, lock=True, nowait=True)
                assert caller.get(Project, other_id).project_name == "caller-controlled"
                caller.commit()
            assert connection.in_transaction()
            assert connection.execute(Project.__table__.select().where(Project.id == other_id)).first() is not None
            if finish == "commit":
                transaction.commit()
            else:
                transaction.rollback()
        finally:
            if transaction.is_active:
                transaction.rollback()
    db.expire_all()
    assert db.get(Project, other_id).project_name == ("caller-controlled" if finish == "commit" else "before")
    assert db.query(AuditLog).filter_by(actor_id=actor_id, action="project_access", status="failed").count() == 0
    assert [reason for reason, _ in warnings] == ["external_connection"]


def test_same_session_records_each_transaction_once(db):
    actor_id, project_id, _ = _seed(db)
    for expected in (1, 2):
        actor = db.get(User, actor_id)
        with pytest.raises(NotFoundError):
            require_project_access(db, project_id, actor, need_write=True, lock=True, nowait=True)
        db.rollback()
        project_access_audit._queue._queue.join()
        count = db.query(AuditLog).filter_by(actor_id=actor_id, action="project_access", status="failed").count()
        assert count == expected
        db.rollback()


def test_pending_audit_log_does_not_include_private_username(db, monkeypatch):
    actor_id, project_id, _ = _seed(db)
    actor = db.get(User, actor_id)
    actor.username = "sensitive-user-name"
    db.commit()
    messages = []
    monkeypatch.setattr(project_access_audit.logger, "warning", lambda message, *args: messages.append((message, args)))
    with pytest.raises(NotFoundError):
        require_project_access(db, project_id, actor, need_write=True, lock=True, nowait=True)
    db.rollback()
    assert messages
    assert "sensitive-user-name" not in repr(messages)
