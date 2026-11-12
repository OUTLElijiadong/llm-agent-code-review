"""Real MySQL RR authorization for edits to existing project files."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from threading import Event

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile
from test_mysql_project_write_authority import _make

from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import create_access_token
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.enterprise_org import Enterprise, EnterpriseGroupMember
from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant
from app.models.project import Project
from app.models.user import User
from app.schemas.code_file import CodeFileIn
from app.services import code_file_service, project_member_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("action", ["edit", "rename", "delete", "restore"])
@pytest.mark.parametrize("change", ["owner", "hidden", "grant", "leader", "disabled", "logout", "reactivate"])
def test_existing_file_write_rechecks_current_project_authority(fresh_chain, action, change):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=change in {"grant", "leader"},
                     group_role="leader" if change == "leader" else "member",
                     grant_role="editor" if change == "grant" else None)
        row = CodeFile(project_id=data["project"], file_name="source.py", file_path="source.py",
                       language="python", content="before", status="active", version_no=1,
                       is_binary=0, raw_size=6, size_bytes=6, line_count=1)
        db.add(row)
        db.flush()
        file_id = row.id
        db.add(CodeVersion(file_id=file_id, version_no=1, content="before", operator_id=data["actor"],
                           create_time=datetime.now(timezone.utc)))

    with factory() as stale:
        actor = (authenticate_access_token(create_access_token(data["actor"], "user"), stale)
                 if change in {"logout", "reactivate"} else stale.get(User, data["actor"]))
        stale.get(Project, data["project"])
        stale.get(CodeFile, file_id)
        if change in {"grant", "leader"}:
            stale.query(EnterpriseProject).filter_by(project_id=data["project"]).first()
        with factory.begin() as changed:
            if change == "owner":
                changed.get(Project, data["project"]).user_id = data["owner"]
            elif change == "hidden":
                changed.get(Project, data["project"]).status = "quarantined"
            elif change == "grant":
                changed.query(EnterpriseProjectGrant).filter_by(
                    project_id=data["project"], user_id=data["actor"]
                ).one().status = "inactive"
            elif change == "disabled":
                changed.get(User, data["actor"]).status = 0
            elif change == "logout":
                changed.get(User, data["actor"]).token_version += 1
            elif change == "reactivate":
                changed.get(User, data["actor"]).status = 0
                changed.get(User, data["actor"]).token_version += 1
            else:
                changed.query(EnterpriseGroupMember).filter_by(
                    group_id=data["group"], user_id=data["actor"]
                ).one().role = "member"
        if change == "reactivate":
            with factory.begin() as changed:
                current = changed.get(User, data["actor"])
                current.status = 1
                current.token_version += 1
        with pytest.raises((NotFoundError, ForbiddenError, AuthError)):
            if action == "edit":
                code_file_service.update_content(stale, actor, file_id, "after", expected_version=1)
            elif action == "rename":
                code_file_service.rename_file(stale, actor, file_id, "changed.py")
            elif action == "delete":
                code_file_service.delete_file(stale, actor, file_id)
            else:
                code_file_service.restore_version(stale, actor, file_id, 1)
        stale.rollback()

    with factory() as check:
        row = check.get(CodeFile, file_id)
        assert (row.content, row.file_name, row.status, row.version_no) == ("before", "source.py", "active", 1)


@pytest.mark.parametrize("change", ["disabled", "logout", "reactivate"])
@pytest.mark.parametrize("action", ["create", "upload"])
def test_new_file_authorization_rechecks_current_identity(fresh_chain, change, action):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
    with factory() as stale:
        actor = authenticate_access_token(create_access_token(data["actor"], "user"), stale)
        stale.get(Project, data["project"])
        with factory.begin() as changed:
            current = changed.get(User, data["actor"])
            if change == "disabled":
                current.status = 0
            else:
                current.token_version += 1
                if change == "reactivate":
                    current.status = 0
        if change == "reactivate":
            with factory.begin() as changed:
                current = changed.get(User, data["actor"])
                current.status = 1
                current.token_version += 1
        with pytest.raises((ForbiddenError, AuthError)):
            if action == "create":
                code_file_service.create_file(
                    stale, actor, CodeFileIn(project_id=data["project"], file_name="new.py", content="x=1"),
                )
            else:
                code_file_service.upload(
                    stale, actor, data["project"], UploadFile(filename="new.py", file=BytesIO(b"x=1")),
                )
    with factory() as check:
        assert check.query(CodeFile).filter_by(project_id=data["project"]).count() == 0


def test_existing_file_authorized_first_serializes_grant_revocation(fresh_chain):
    from app.services.enterprise_project_service import revoke_project_grant

    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, grant_role="editor")
        row = CodeFile(project_id=data["project"], file_name="source.py", file_path="source.py",
                       language="python", content="before", status="active", version_no=1,
                       is_binary=0, raw_size=6, size_bytes=6, line_count=1)
        db.add(row)
        db.flush()
        file_id = row.id
    started, done = Event(), Event()

    def revoke():
        with factory() as db:
            started.set()
            revoke_project_grant(db, data["company"], data["project"], data["actor"], actor_id=data["owner"])
            done.set()

    with factory() as writer, ThreadPoolExecutor(max_workers=1) as pool:
        actor = writer.get(User, data["actor"])
        code_file_service._get_accessible_file(writer, actor, file_id, need_write=True)
        future = pool.submit(revoke)
        try:
            assert started.wait(3)
            assert not done.wait(0.3)
            assert code_file_service.update_content(writer, actor, file_id, "after", expected_version=1) == 2
        finally:
            writer.rollback()
        future.result(timeout=8)
    with factory() as check:
        with pytest.raises(ForbiddenError):
            code_file_service.update_content(
                check, check.get(User, data["actor"]), file_id, "again", expected_version=2,
            )


def test_existing_file_company_lock_conflict_is_retryable(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, grant_role="editor")
        row = CodeFile(project_id=data["project"], file_name="source.py", file_path="source.py",
                       language="python", content="before", status="active", version_no=1,
                       is_binary=0, raw_size=6, size_bytes=6, line_count=1)
        db.add(row)
        db.flush()
        file_id = row.id
    with factory() as holder, factory() as writer:
        holder.query(Enterprise).filter_by(id=data["company"]).with_for_update().one()
        with pytest.raises(ConflictError, match="权限正在变更"):
            code_file_service.update_content(
                writer, writer.get(User, data["actor"]), file_id, "after", expected_version=1,
            )
        writer.rollback()
        holder.rollback()
    with factory() as check:
        assert check.get(CodeFile, file_id).content == "before"


@pytest.mark.parametrize("offset, allowed", [(-0.001, True), (0, False), (0.001, False)])
def test_new_file_token_expiry_checked_at_write_authorization(fresh_chain, monkeypatch, offset, allowed):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db)
    expires_at = int((datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp())
    with factory() as db:
        user = authenticate_access_token(create_access_token(data["actor"], "user", expires_at=expires_at), db)
        monkeypatch.setattr(project_member_service, "time", lambda: expires_at + offset)
        if allowed:
            assert code_file_service._check_project_access(db, user, data["project"]).id == data["project"]
        else:
            with pytest.raises(AuthError):
                code_file_service._check_project_access(db, user, data["project"])
