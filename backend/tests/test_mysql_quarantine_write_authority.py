"""Quarantine writes must use current project authorization under MySQL RR."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from tempfile import SpooledTemporaryFile
from threading import Event
from time import monotonic, sleep
from uuid import uuid4
from zipfile import ZipFile

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile
from test_mysql_project_write_authority import _make

from app.api.v1 import code_files
from app.core.dependencies import authenticate_access_token
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.security import create_access_token
from app.models.code_file import CodeFile
from app.models.enterprise_org import EnterpriseGroupMember
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import code_file_service, project_source_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("change", ["owner", "hidden", "grant", "group_role"])
def test_quarantine_rechecks_project_authority_after_old_snapshot(fresh_chain, monkeypatch, change):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    enterprise = change in {"grant", "group_role"}
    with factory.begin() as db:
        item = _make(db, enterprise=enterprise,
                     group_role="leader" if change == "group_role" else "member",
                     grant_role="editor" if change == "grant" else None)
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", lambda *_a, **_k: ("infected", 1, "{}"))
    with factory() as stale:
        actor = stale.get(User, item["actor"])
        stale.get(Project, item["project"])
        if enterprise:
            stale.query(EnterpriseProject).filter_by(project_id=item["project"]).first()
        with factory.begin() as update:
            if change == "owner":
                update.get(Project, item["project"]).user_id = item["owner"]
            elif change == "hidden":
                update.get(Project, item["project"]).status = "quarantined"
            elif change == "grant":
                update.query(EnterpriseProjectGrant).filter_by(
                    project_id=item["project"], user_id=item["actor"],
                ).one().status = "inactive"
            else:
                update.query(EnterpriseGroupMember).filter_by(
                    group_id=item["group"], user_id=item["actor"],
                ).one().role = "member"
        with pytest.raises((NotFoundError, ForbiddenError, ValidationError)):
            project_source_service.ingest_source_archive_bytes(
                stale, actor, item["project"], raw=raw.getvalue(), filename="main.zip",
            )
    with factory() as db:
        assert db.query(ProjectSourceArchive).filter_by(project_id=item["project"]).count() == 0


def test_folder_quarantine_reauthorizes_project_after_rollback(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    with factory.begin() as db:
        role = db.query(Role).filter_by(code="user").one()
        permission = db.query(Permission).filter_by(code="file:upload").one()
        if not db.query(RolePermission.id).filter_by(role_id=role.id, permission_id=permission.id).first():
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        actor = User(username=f"guard_{suffix}", password="x", role="user", status=1, token_version=0)
        other = User(username=f"guard_other_{suffix}", password="x", role="user", status=1, token_version=0)
        db.add_all([actor, other])
        db.flush()
        db.add(UserRole(user_id=actor.id, role_id=role.id))
        project = Project(user_id=actor.id, project_name=f"guard_{suffix}", status="active")
        db.add(project)
        db.flush()
        actor_id, other_id, project_id = actor.id, other.id, project.id
    with factory() as db:
        user = authenticate_access_token(create_access_token(actor_id, "user"), db)
        actor = code_files._snapshot_folder_actor(user)

    ready, release = Event(), Event()
    original_reauthorize = code_files._FolderCommitGuard.after_rollback

    def reauthorize_then_pause(guard):
        original_reauthorize(guard)
        # Establish the same old RR snapshot as the preliminary quarantine
        # existence queries before the owner changes in the other transaction.
        guard.db.query(ProjectSourceArchive.id).filter_by(project_id=project_id).first()
        ready.set()
        assert release.wait(10)

    def infected(*_args, **_kwargs):
        raise code_file_service.MaliciousUploadError("样本", "test")

    monkeypatch.setattr(code_files._FolderCommitGuard, "after_rollback", reauthorize_then_pause)
    monkeypatch.setattr(code_file_service, "_validate_upload_security", infected)
    monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", lambda *_a, **_k: ("infected", 1, "{}"))
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr("main.py", "print(1)")

    def upload():
        with SpooledTemporaryFile() as file, factory() as db:
            file.write(payload.getvalue())
            file.seek(0)
            return code_files._upload_folder_items(db, None, project_id, [UploadFile(file, filename="main.zip")],
                                                   actor=actor)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(upload)
            assert ready.wait(10)
            with factory.begin() as db:
                db.get(Project, project_id).user_id = other_id
            release.set()
            assert task.result(timeout=10)["success_count"] == 0
        with factory() as db:
            assert db.query(ProjectSourceArchive).filter_by(project_id=project_id).count() == 0
    finally:
        release.set()


def test_quarantine_detects_source_file_committed_after_snapshot(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        item = _make(db)
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", lambda *_a, **_k: ("infected", 1, "{}"))
    with factory() as stale:
        actor = stale.get(User, item["actor"])
        stale.get(Project, item["project"])
        assert not stale.query(CodeFile.id).filter_by(project_id=item["project"]).first()
        with factory() as writer:
            user = writer.get(User, item["actor"])
            code_file_service._check_project_access(writer, user, item["project"])
            code_file_service._create_file(writer, user, item["project"], "old.py", "old.py", "python", "x=1", "test")
        with pytest.raises(ConflictError):
            project_source_service.ingest_source_archive_bytes(
                stale, actor, item["project"], raw=raw.getvalue(), filename="main.zip",
            )
    with factory() as db:
        assert db.query(CodeFile).filter_by(project_id=item["project"]).count() == 1
        assert db.query(ProjectSourceArchive).filter_by(project_id=item["project"]).count() == 0


def test_quarantine_detects_archive_committed_after_snapshot(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        item = _make(db)
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", lambda *_a, **_k: ("infected", 1, "{}"))
    with factory() as stale:
        actor = stale.get(User, item["actor"])
        stale.get(Project, item["project"])
        assert not stale.query(ProjectSourceArchive.id).filter_by(project_id=item["project"]).first()
        with factory() as writer:
            project_source_service.ingest_source_archive_bytes(
                writer, writer.get(User, item["actor"]), item["project"], raw=raw.getvalue(), filename="main.zip",
            )
        with pytest.raises(ConflictError):
            project_source_service.ingest_source_archive_bytes(
                stale, actor, item["project"], raw=raw.getvalue(), filename="main.zip",
            )
    with factory() as db:
        assert db.query(ProjectSourceArchive).filter_by(project_id=item["project"]).count() == 1


def test_quarantine_lock_serializes_owner_change(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        item = _make(db)
    raw = BytesIO()
    with ZipFile(raw, "w") as archive:
        archive.writestr("main.py", "print(1)")
    started, release = Event(), Event()
    writer_connection = []

    def slow_scan(*_args, **_kwargs):
        started.set()
        assert release.wait(10)
        return "infected", 1, "{}"

    monkeypatch.setattr(project_source_service, "_scan_quarantined_zip", slow_scan)

    def upload():
        with factory() as db:
            return project_source_service.ingest_source_archive_bytes(
                db, db.get(User, item["actor"]), item["project"], raw=raw.getvalue(), filename="main.zip",
            )

    def change_owner():
        with factory.begin() as db:
            writer_connection.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            db.query(Project).filter_by(id=item["project"]).update({Project.user_id: item["owner"]})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            uploading = pool.submit(upload)
            assert started.wait(10)
            changing = pool.submit(change_owner)
            deadline = monotonic() + 5
            waiting = False
            while monotonic() < deadline:
                if writer_connection:
                    with fresh_chain.connect() as connection:
                        waiting = bool(connection.execute(text(
                            "SELECT 1 FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.THREAD_ID = w.REQUESTING_THREAD_ID "
                            "WHERE t.PROCESSLIST_ID = :connection_id LIMIT 1"
                        ), {"connection_id": writer_connection[0]}).first())
                    if waiting:
                        break
                sleep(0.05)
            assert waiting, "owner change must wait for the quarantine project lock"
            release.set()
            uploading.result(timeout=10)
            changing.result(timeout=10)
        with factory() as db:
            assert db.get(Project, item["project"]).user_id == item["owner"]
            assert db.query(ProjectSourceArchive).filter_by(project_id=item["project"]).count() == 1
    finally:
        release.set()
