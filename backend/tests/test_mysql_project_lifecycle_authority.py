"""Current authorization for project metadata and deletion under MySQL RR."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from test_mysql_pentest_revoked_project_access import _seed
from test_mysql_project_write_authority import _make

from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.security import create_access_token
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.enterprise_project import EnterpriseProjectGrant
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectUpdateIn
from app.services import project_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("change", ["owner", "project_hidden", "group_role", "group_status", "member"])
def test_project_write_rechecks_current_authority_after_rr_snapshot(fresh_chain, operation, change):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=change in {"group_role", "group_status", "member"}, group_role="leader")
    with factory() as stale:
        actor = stale.get(User, data["actor"])
        stale.get(Project, data["project"])
        if "company" in data:
            stale.query(EnterpriseMember).filter_by(enterprise_id=data["company"], user_id=actor.id).first()
        with factory.begin() as db:
            if change == "owner":
                db.get(Project, data["project"]).user_id = data["owner"]
            elif change == "project_hidden":
                db.get(Project, data["project"]).status = "quarantined"
            elif change == "group_role":
                group_member = db.query(EnterpriseGroupMember).filter_by(group_id=data["group"], user_id=actor.id).one()
                group_member.role = "member"
            elif change == "group_status":
                db.get(EnterpriseGroup, data["group"]).status = "inactive"
            else:
                member = db.query(EnterpriseMember).filter_by(enterprise_id=data["company"], user_id=actor.id).one()
                member.status = "removed"
        with pytest.raises((ForbiddenError, NotFoundError, ConflictError, ValidationError)):
            if operation == "delete":
                project_service.delete_project(stale, actor, data["project"])
            else:
                project_service.update_project(
                    stale, actor, data["project"], ProjectUpdateIn(description="not allowed")
                )


def test_revoked_enterprise_editor_cannot_update_after_rr_snapshot(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, grant_role="editor")
    with factory() as stale:
        actor = stale.get(User, data["actor"])
        stale.get(Project, data["project"])
        stale.query(EnterpriseProjectGrant).filter_by(project_id=data["project"], user_id=actor.id).first()
        with factory.begin() as db:
            grant = db.query(EnterpriseProjectGrant).filter_by(project_id=data["project"], user_id=actor.id).one()
            grant.status = "inactive"
        with pytest.raises((ForbiddenError, NotFoundError, ConflictError)):
            project_service.update_project(stale, actor, data["project"], ProjectUpdateIn(description="not allowed"))


def test_project_owner_may_update_and_delete(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
    with factory() as db:
        actor = db.get(User, data["actor"])
        changed = project_service.update_project(db, actor, data["project"], ProjectUpdateIn(description="permitted"))
        assert changed.description == "permitted"
        project_service.delete_project(db, actor, data["project"])
        assert db.get(Project, data["project"]).status == "deleted"


def test_enterprise_editor_may_edit_metadata_but_not_change_lifecycle(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, grant_role="editor")
    with factory() as db:
        actor = db.get(User, data["actor"])
        changed = project_service.update_project(db, actor, data["project"], ProjectUpdateIn(description="permitted"))
        assert changed.description == "permitted"
        with pytest.raises(ForbiddenError):
            project_service.update_project(db, actor, data["project"], ProjectUpdateIn(status="archived"))
        with pytest.raises(ForbiddenError):
            project_service.delete_project(db, actor, data["project"])


def test_enterprise_leader_may_change_lifecycle(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, group_role="leader")
    with factory() as db:
        actor = db.get(User, data["actor"])
        changed = project_service.update_project(db, actor, data["project"], ProjectUpdateIn(status="archived"))
        assert changed.status == "archived"
        project_service.delete_project(db, actor, data["project"])
        assert db.get(Project, data["project"]).status == "deleted"


def test_competing_project_owner_change_fails_closed_without_waiting(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
    held, release = Event(), Event()

    def competing_change():
        with factory.begin() as db:
            project = db.query(Project).filter_by(id=data["project"]).with_for_update().one()
            project.user_id = data["owner"]
            db.flush()
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(competing_change)
        try:
            assert held.wait(10)
            with factory() as db:
                actor = db.get(User, data["actor"])
                with pytest.raises(ConflictError):
                    project_service.update_project(
                        db, actor, data["project"], ProjectUpdateIn(description="not allowed")
                    )
        finally:
            release.set()
        worker.result(timeout=10)


def test_project_write_holds_authority_until_commit(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
    held, release, transfer_started, transfer_finished = Event(), Event(), Event(), Event()
    original = project_service._require_project_mutation

    def pause_after_authorization(*args, **kwargs):
        project = original(*args, **kwargs)
        held.set()
        assert release.wait(10)
        return project

    monkeypatch.setattr(project_service, "_require_project_mutation", pause_after_authorization)

    def write():
        with factory() as db:
            return project_service.update_project(
                db, db.get(User, data["actor"]), data["project"], ProjectUpdateIn(description="allowed")
            ).id

    def transfer():
        with factory.begin() as db:
            transfer_started.set()
            project = db.query(Project).filter_by(id=data["project"]).with_for_update().one()
            project.user_id = data["owner"]
        transfer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write)
        try:
            assert held.wait(10)
            changer = pool.submit(transfer)
            assert transfer_started.wait(10)
            assert not transfer_finished.wait(0.5)
        finally:
            release.set()
        assert writer.result(timeout=10) == data["project"]
        changer.result(timeout=10)
        assert transfer_finished.is_set()


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("change", ["owner", "token_version"])
def test_authenticated_owner_loses_write_after_change(fresh_chain, operation, change):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, other_id, _, project_id, _, _ = _seed(factory)
    token = create_access_token(owner_id, "user", token_version=0)
    with factory() as stale:
        actor = authenticate_access_token(token, stale)
        stale.get(Project, project_id)
        with factory.begin() as db:
            if change == "owner":
                db.get(Project, project_id).user_id = other_id
            else:
                db.get(User, owner_id).token_version += 1
        with pytest.raises((NotFoundError, ForbiddenError, AuthError)):
            if operation == "delete":
                project_service.delete_project(stale, actor, project_id)
            else:
                project_service.update_project(stale, actor, project_id, ProjectUpdateIn(description="denied"))
