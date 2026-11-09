"""Rejected member operations must not commit unrelated caller changes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from test_mysql_pentest_revoked_project_access import _seed

from app.core.dependencies import authenticate_access_token
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.project import Project
from app.services import project_access_audit
from app.services import project_member_service as members

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("operation", ["add", "remove", "role"])
@pytest.mark.parametrize("denial", ["no_access", "read_only"])
def test_denied_member_write_does_not_commit_unrelated_pending_project(fresh_chain, operation, denial):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, member, target, project_id, _, _ = _seed(factory)
    outsider, _, _, _, _, _ = _seed(factory)
    unauthorized = outsider if denial == "no_access" else member
    with factory.begin() as db:
        unrelated = Project(user_id=owner, project_name="before", status="active")
        db.add(unrelated)
        db.flush()
        unrelated_id = unrelated.id
    with factory() as caller:
        actor = authenticate_access_token(create_access_token(unauthorized, "user", token_version=0), caller)
        caller.get(Project, unrelated_id).project_name = "must not commit on rejection"
        expected = NotFoundError if denial == "no_access" else ForbiddenError
        with pytest.raises(expected):
            if operation == "add":
                members.add_member(caller, project_id, target, "viewer", operator=actor)
            elif operation == "remove":
                members.remove_member(caller, project_id, member, operator=actor)
            else:
                members.update_member_role(caller, project_id, member, "viewer", operator=actor)
        caller.rollback()
    deadline = monotonic() + 5
    count = 0
    while monotonic() < deadline:
        with factory() as check:
            assert check.get(Project, unrelated_id).project_name == "before"
            count = check.query(AuditLog).filter_by(
                actor_id=unauthorized, action="project_access", status="failed"
            ).count()
        if count:
            break
        sleep(0.02)
    assert count == 1


@pytest.mark.parametrize("nested", [False, True])
def test_denial_does_not_close_outer_transaction_with_single_connection_pool(fresh_chain, nested):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, _, _, project_id, _, _ = _seed(factory)
    outsider, _, _, _, _, _ = _seed(factory)
    with factory.begin() as db:
        other = Project(user_id=owner, project_name="before", status="active")
        db.add(other)
        db.flush()
        other_id = other.id
    engine = create_engine(fresh_chain.url, pool_size=1, max_overflow=0, pool_timeout=1)
    single = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        with single() as caller:
            with caller.begin():
                actor = authenticate_access_token(create_access_token(outsider, "user", token_version=0), caller)
                caller.get(Project, other_id).project_name = "outer survives"
                caller.flush()
                if nested:
                    with pytest.raises(NotFoundError):
                        with caller.begin_nested():
                            members.require_project_access(caller, project_id, actor, need_write=True,
                                                           lock=True, nowait=True)
                else:
                    with pytest.raises(NotFoundError):
                        members.require_project_access(caller, project_id, actor, need_write=True,
                                                       lock=True, nowait=True)
                assert caller.get(Project, other_id).project_name == "outer survives"
        deadline = monotonic() + 5
        count = 0
        while monotonic() < deadline:
            with factory() as check:
                assert check.get(Project, other_id).project_name == "outer survives"
                count = check.query(AuditLog).filter_by(
                    actor_id=outsider, action="project_access", status="failed"
                ).count()
            if count:
                break
            sleep(0.02)
        assert count == 1
    finally:
        engine.dispose()


def test_audit_pool_exhaustion_never_blocks_the_denial_caller(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, _, _, project_id, _, _ = _seed(factory)
    outsider, _, _, _, _, _ = _seed(factory)
    assert owner != outsider
    engine = create_engine(fresh_chain.url, pool_size=1, max_overflow=0, pool_timeout=0.25)
    single = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    held, release, reported = Event(), Event(), Event()
    errors, fallbacks = [], []
    queue = project_access_audit._DeferredAuditQueue(capacity=2, writer=project_access_audit._persist)
    monkeypatch.setattr(project_access_audit, "_queue", queue)
    monkeypatch.setattr(project_access_audit, "_report_worker_failure", errors.append)
    def record_fallback(reason, _denial):
        fallbacks.append(reason)
        reported.set()

    monkeypatch.setattr(project_access_audit, "_fallback", record_fallback)

    def occupy_pool():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            held.set()
            assert release.wait(5)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with single() as caller:
                actor = authenticate_access_token(create_access_token(outsider, "user", token_version=0), caller)
                holder = []

                def take_released_connection(_session, transaction):
                    if transaction.parent is None and not holder:
                        holder.append(executor.submit(occupy_pool))
                        assert held.wait(2)

                # Install before the audit listener so the worker has no spare
                # connection when it attempts the detached audit transaction.
                event.listen(caller, "after_transaction_end", take_released_connection)
                with pytest.raises(NotFoundError):
                    members.require_project_access(caller, project_id, actor, need_write=True, lock=True, nowait=True)
                start = monotonic()
                caller.rollback()
                assert monotonic() - start < 1.0
                assert reported.wait(3)
                assert errors == ["TimeoutError"]
                assert fallbacks == ["database_write_failed"]
                release.set()
                holder[0].result(timeout=3)
    finally:
        release.set()
        queue.shutdown(timeout=2)
        engine.dispose()
