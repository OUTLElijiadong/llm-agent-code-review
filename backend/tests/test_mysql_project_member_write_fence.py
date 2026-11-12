"""Project membership writes must use current authority, not an old RR snapshot."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic

import pytest
import test_fresh_database_chain as chain
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from test_mysql_pentest_revoked_project_access import _seed, _seed_company

from app.api.v1 import project_members
from app.core.database import get_db
from app.core.dependencies import authenticate_access_token
from app.core.error_handlers import register_handlers
from app.core.exceptions import (
    AuthError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models.enterprise_project import EnterpriseProject
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission
from app.models.user import User
from app.services import project_member_service as members

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _apply(db, operation, project_id, target_id, actor):
    if operation == "add":
        return members.add_member(db, project_id, target_id, "viewer", operator=actor)
    if operation == "remove":
        return members.remove_member(db, project_id, target_id, operator=actor)
    return members.update_member_role(db, project_id, target_id, "viewer", operator=actor)


@pytest.mark.parametrize("operation", ["add", "remove", "role"])
@pytest.mark.parametrize("change", ["owner", "hidden", "disabled", "logout"])
def test_membership_writes_recheck_current_actor_and_project(fresh_chain, operation, change):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, member, target, project_id, _, _ = _seed(factory)
    target_id = target if operation == "add" else member
    with factory() as stale:
        actor = authenticate_access_token(create_access_token(owner, "user", token_version=0), stale)
        stale.get(Project, project_id)
        stale.query(ProjectMember).filter_by(project_id=project_id).all()
        with factory.begin() as current:
            if change == "owner":
                current.get(Project, project_id).user_id = target
            elif change == "hidden":
                current.get(Project, project_id).status = "quarantined"
            elif change == "disabled":
                current.get(User, owner).status = 0
            else:
                current.get(User, owner).token_version = 1
        with pytest.raises((AuthError, ForbiddenError, NotFoundError)):
            _apply(stale, operation, project_id, target_id, actor)
    with factory() as db:
        row = db.query(ProjectMember).filter_by(project_id=project_id, user_id=target_id).first()
        assert (row is None) if operation == "add" else (row is not None and row.role_in_project == "reviewer")


@pytest.mark.parametrize("operation", ["add", "remove", "role"])
@pytest.mark.parametrize("actor_present", [True, False])
def test_new_enterprise_binding_never_accepts_legacy_member_mutations(fresh_chain, operation, actor_present):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, member, target, project_id, _, _ = _seed(factory)
    company, group, _ = _seed_company(factory, owner, new_project=False)
    target_id = target if operation == "add" else member
    with factory() as stale:
        actor = stale.get(User, owner) if actor_present else None
        stale.get(Project, project_id)
        assert stale.query(EnterpriseProject).filter_by(project_id=project_id).first() is None
        with factory.begin() as current:
            current.add(EnterpriseProject(
                enterprise_id=company, group_id=group, project_id=project_id, status="active",
            ))
        with pytest.raises(ServiceUnavailableError):
            _apply(stale, operation, project_id, target_id, actor)
    with factory() as db:
        row = db.query(ProjectMember).filter_by(project_id=project_id, user_id=target_id).first()
        assert (row is None) if operation == "add" else (row is not None and row.role_in_project == "reviewer")


@pytest.mark.parametrize("operation", ["remove", "role"])
def test_member_promoted_to_owner_after_old_snapshot_cannot_be_removed_or_demoted(fresh_chain, operation):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, member, _, project_id, _, _ = _seed(factory)
    with factory() as stale:
        actor = stale.get(User, owner)
        stale.query(ProjectMember).filter_by(project_id=project_id, user_id=member).one()
        with factory.begin() as current:
            row = current.query(ProjectMember).filter_by(project_id=project_id, user_id=member).one()
            row.role_in_project = "owner"
        with pytest.raises(BadRequestError):
            _apply(stale, operation, project_id, member, actor)
    with factory() as db:
        assert db.query(ProjectMember).filter_by(project_id=project_id, user_id=member).one().role_in_project == "owner"


def test_target_disabled_after_old_snapshot_cannot_be_added(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, _, target, project_id, _, _ = _seed(factory)
    with factory() as stale:
        actor = stale.get(User, owner)
        assert stale.get(User, target).status == 1
        with factory.begin() as current:
            current.get(User, target).status = 0
        with pytest.raises(NotFoundError):
            _apply(stale, "add", project_id, target, actor)


@pytest.mark.parametrize("operation", ["add", "remove", "role"])
def test_revoked_member_manage_grant_cannot_survive_old_snapshot(fresh_chain, operation):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, member, target, project_id, _, _ = _seed(factory)
    target_id = target if operation == "add" else member
    with factory() as stale:
        actor = authenticate_access_token(create_access_token(owner, "user", token_version=0), stale)
        stale.get(Project, project_id)
        grant = (stale.query(RolePermission).join(Role, Role.id == RolePermission.role_id)
                 .join(Permission, Permission.id == RolePermission.permission_id)
                 .filter(Role.code == "user", Permission.code == PermissionCode.PROJECT_MEMBER_MANAGE).one())
        role_id, permission_id = grant.role_id, grant.permission_id
        try:
            with factory.begin() as current:
                current.query(RolePermission).filter_by(role_id=role_id, permission_id=permission_id).delete()
            with pytest.raises(ForbiddenError):
                _apply(stale, operation, project_id, target_id, actor)
        finally:
            stale.rollback()
            with factory.begin() as restore:
                restore.add(RolePermission(role_id=role_id, permission_id=permission_id))


def test_project_lock_conflict_fails_closed_without_waiting(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, _, target, project_id, _, _ = _seed(factory)
    locked, release = Event(), Event()

    def hold_project():
        with factory.begin() as db:
            db.query(Project).filter_by(id=project_id).with_for_update().one()
            locked.set()
            assert release.wait(8)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(hold_project)
        try:
            assert locked.wait(8)
            with factory() as db:
                actor = authenticate_access_token(create_access_token(owner, "user", token_version=0), db)
                start = monotonic()
                with pytest.raises(ConflictError):
                    members.add_member(db, project_id, target, "viewer", operator=actor)
                assert monotonic() - start < 3
        finally:
            release.set()
        task.result(timeout=8)


def test_cross_add_of_two_owners_never_deadlocks(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    first, second, _, _, _, _ = _seed(factory)
    with factory.begin() as db:
        projects = [Project(user_id=first, project_name="cross-add-one", status="active"),
                    Project(user_id=second, project_name="cross-add-two", status="active")]
        db.add_all(projects)
        db.flush()
        first_project, second_project = (row.id for row in projects)
    ready = Barrier(2, timeout=8)

    def add(actor_id, target_id, project_id):
        with factory() as db:
            # Each request starts with a separately locked actor. Target locks
            # compete in reverse order; NOWAIT must return a bounded conflict.
            db.query(User).filter_by(id=actor_id).with_for_update().one()
            actor = authenticate_access_token(create_access_token(actor_id, "user", token_version=0), db)
            ready.wait()
            try:
                members.add_member(db, project_id, target_id, "viewer", operator=actor)
                return "success"
            except ConflictError:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(add, first, second, first_project)
        two = pool.submit(add, second, first, second_project)
        results = [one.result(timeout=10), two.result(timeout=10)]
    assert all(result in {"success", "conflict"} for result in results)
    assert "conflict" in results


def test_real_jwt_member_api_preserves_owner_flow_and_denies_revoked_token(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, _, target, project_id, _, _ = _seed(factory)
    app = FastAPI()
    register_handlers(app)
    app.include_router(project_members.router, prefix="/projects/{project_id}/members")

    def isolated_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    headers = {"Authorization": f"Bearer {create_access_token(owner, 'user', token_version=0)}"}
    with TestClient(app) as client:
        added = client.post(f"/projects/{project_id}/members", headers=headers,
                            json={"user_id": target, "role_in_project": "viewer"})
        assert added.status_code == 200, added.text
        edited = client.put(f"/projects/{project_id}/members/{target}", headers=headers,
                            json={"role_in_project": "reviewer"})
        assert edited.status_code == 200, edited.text
        removed = client.delete(f"/projects/{project_id}/members/{target}", headers=headers)
        assert removed.status_code == 200, removed.text
        with factory.begin() as db:
            db.get(User, owner).token_version = 1
        denied = client.post(f"/projects/{project_id}/members", headers=headers,
                             json={"user_id": target, "role_in_project": "viewer"})
        assert denied.status_code == 401, denied.text
