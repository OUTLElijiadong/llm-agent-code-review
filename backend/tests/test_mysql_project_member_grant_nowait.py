"""Member mutations must fail promptly if the actor's live grant is locked."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
import test_fresh_database_chain as chain
import test_mysql_reviewer_refund_approval as reviewer_cases
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from test_mysql_pentest_revoked_project_access import _seed

from app.api.v1 import project_members
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import authenticate_access_token
from app.core.error_handlers import register_handlers
from app.core.exceptions import ConflictError
from app.core.permission_codes import PermissionCode
from app.core.security import create_access_token
from app.models.billing_ledger import BillingOrder
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission
from app.models.user import User
from app.services import project_member_service as members

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("blocked_row", ["actor", "grant"])
@pytest.mark.parametrize("operation", ["add", "remove", "role"])
def test_member_grant_lock_conflict_returns_retryable_without_wait(fresh_chain, blocked_row, operation):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, existing, target, project_id, _, _ = _seed(factory)
    locked, release = Event(), Event()
    with factory() as caller:
        caller.execute(text("SET SESSION innodb_lock_wait_timeout=3"))
        actor = authenticate_access_token(create_access_token(owner, "user", token_version=0), caller)

        def hold():
            with factory.begin() as db:
                if blocked_row == "actor":
                    db.query(User).filter_by(id=owner).with_for_update().one()
                else:
                    (db.query(RolePermission).join(Role, Role.id == RolePermission.role_id)
                     .join(Permission, Permission.id == RolePermission.permission_id)
                     .filter(Role.code == "user", Permission.code == PermissionCode.PROJECT_MEMBER_MANAGE)
                     .with_for_update().one())
                locked.set()
                assert release.wait(8)

        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(hold)
            try:
                assert locked.wait(8)
                start = monotonic()
                with pytest.raises(ConflictError) as error:
                    if operation == "add":
                        members.add_member(caller, project_id, target, "viewer", operator=actor)
                    elif operation == "remove":
                        members.remove_member(caller, project_id, existing, operator=actor)
                    else:
                        members.update_member_role(caller, project_id, existing, "viewer", operator=actor)
                assert error.value.code == 40901
                assert monotonic() - start < 1.5, "实时权限锁应立即冲突，不应等待锁超时"
            finally:
                release.set()
            task.result(timeout=8)
    with factory() as db:
        actor = authenticate_access_token(create_access_token(owner, "user", token_version=0), db)
        if operation == "add":
            assert members.add_member(db, project_id, target, "viewer", operator=actor).user_id == target
        elif operation == "remove":
            assert members.remove_member(db, project_id, existing, operator=actor)
        else:
            changed = members.update_member_role(db, project_id, existing, "viewer", operator=actor)
            assert changed.role_in_project == "viewer"


def test_real_jwt_member_api_reports_actor_lock_conflict(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    owner, _, target, project_id, _, _ = _seed(factory)
    app = FastAPI()
    register_handlers(app)
    app.include_router(project_members.router, prefix="/projects/{project_id}/members")

    def isolated_db():
        with factory() as db:
            db.execute(text("SET SESSION innodb_lock_wait_timeout=3"))
            yield db

    app.dependency_overrides[get_db] = isolated_db
    locked, release = Event(), Event()

    def hold():
        with factory.begin() as db:
            db.query(User).filter_by(id=owner).with_for_update().one()
            locked.set()
            assert release.wait(8)

    headers = {"Authorization": f"Bearer {create_access_token(owner, 'user', token_version=0)}"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(hold)
        try:
            assert locked.wait(8)
            with TestClient(app) as client:
                start = monotonic()
                response = client.post(f"/projects/{project_id}/members", headers=headers,
                                       json={"user_id": target, "role_in_project": "viewer"})
                assert response.status_code == 409, response.text
                assert response.json()["code"] == 40901
                assert monotonic() - start < 1.5
        finally:
            release.set()
        task.result(timeout=8)


def test_reviewer_entitlement_lock_is_also_nowait(fresh_chain, monkeypatch):
    monkeypatch.setattr(settings, "billing_entitlement_enforcement_ready", True)
    factory = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    reviewer_id, _, _ = reviewer_cases._seed(factory)
    _, _, target, _, _, _ = _seed(factory)
    with factory.begin() as db:
        role = db.query(Role).filter_by(code="reviewer", status="active").one()
        permission = db.query(Permission).filter_by(code=PermissionCode.PROJECT_MEMBER_MANAGE).one()
        if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=permission.id).first():
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        project = Project(user_id=reviewer_id, project_name="reviewer member management", status="active")
        db.add(project)
        db.flush()
        project_id = project.id
    locked, release = Event(), Event()
    with factory() as caller:
        caller.execute(text("SET SESSION innodb_lock_wait_timeout=3"))
        actor = authenticate_access_token(create_access_token(reviewer_id, "reviewer", token_version=0), caller)

        def hold():
            with factory.begin() as db:
                db.query(BillingOrder).filter_by(user_id=reviewer_id, product="reviewer_month").with_for_update().one()
                locked.set()
                assert release.wait(8)

        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(hold)
            try:
                assert locked.wait(8)
                start = monotonic()
                with pytest.raises(ConflictError) as error:
                    members.add_member(caller, project_id, target, "viewer", operator=actor)
                assert error.value.code == 40901
                assert monotonic() - start < 1.5
            finally:
                release.set()
            task.result(timeout=8)
    with factory() as db:
        actor = authenticate_access_token(create_access_token(reviewer_id, "reviewer", token_version=0), db)
        assert members.add_member(db, project_id, target, "viewer", operator=actor).user_id == target
