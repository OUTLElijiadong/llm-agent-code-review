"""可信数据隔离回归：仅使用测试会话，不读取生产记录。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas.project import ProjectUpdateIn
from app.services import project_member_service, project_service


@pytest.fixture
def quarantine_scope(db):
    users = {}
    for index, role in enumerate(("owner", "member", "outsider", "admin", "super_admin"), 701):
        user = User(
            id=index, username=f"quarantine_fixture_{role}", password="test-only",
            role=role if role in {"admin", "super_admin"} else "user", status=1,
        )
        db.add(user)
        users[role] = user
    projects = {}
    statuses = ("active", "archived", "importing", "import_failed", "deleted", "quarantined")
    for index, status in enumerate(statuses, 9001):
        project = Project(
            id=index, user_id=users["owner"].id, project_name=f"隔离单测_{status}", status=status,
        )
        db.add(project)
        db.add(ProjectMember(project_id=index, user_id=users["member"].id, role_in_project="reviewer"))
        projects[status] = project
    db.add(ProjectMember(project_id=999999, user_id=users["member"].id, role_in_project="reviewer"))
    db.commit()
    return users, projects


@pytest.mark.parametrize("role", ["owner", "member", "outsider", "admin", "super_admin", None])
def test_visible_ids_exclude_hidden_and_orphan_projects(db, quarantine_scope, role):
    users, projects = quarantine_scope
    visible_ids, scope = project_member_service.get_visible_project_ids(db, users.get(role))
    expected = {projects[status].id for status in ("active", "archived", "importing", "import_failed")}
    assert set(visible_ids) == (set() if role == "outsider" else expected)
    assert scope == ("global" if role in {"admin", "super_admin", None} else "self")


@pytest.mark.parametrize("role", ["owner", "member", "outsider", "admin", "super_admin"])
@pytest.mark.parametrize("status", ["deleted", "quarantined", "missing"])
def test_hidden_projects_have_no_membership_or_read_write_access(db, quarantine_scope, role, status):
    users, projects = quarantine_scope
    project_id = projects[status].id if status in projects else 999999
    assert project_member_service.is_project_member(db, project_id, users[role]) == (False, "")
    for need_write in (False, True):
        with pytest.raises(NotFoundError):
            project_member_service.require_project_access(db, project_id, users[role], need_write=need_write)


@pytest.mark.parametrize("role", ["owner", "member", "admin", "super_admin"])
@pytest.mark.parametrize("status", ["", "active", "archived", "quarantined", "deleted", "all", None])
def test_list_status_never_discloses_hidden_projects(db, quarantine_scope, monkeypatch, role, status):
    users, projects = quarantine_scope
    monkeypatch.setattr(project_service, "_agent_run_stats", lambda *args: {})
    result = project_service.list_projects(db, users[role], status=status)
    expected = {project.id for key, project in projects.items() if key not in {"deleted", "quarantined"}}
    if status:
        expected = {projects[status].id} if status in {"active", "archived"} else set()
    assert {item["id"] for item in result["items"]} == expected
    assert result["total"] == len(expected)


def test_list_rechecks_status_even_with_stale_visibility_ids(db, quarantine_scope, monkeypatch):
    users, projects = quarantine_scope
    monkeypatch.setattr(
        project_service, "get_visible_project_ids", lambda *args: ([row.id for row in projects.values()], "global"),
    )
    monkeypatch.setattr(project_service, "_agent_run_stats", lambda *args: {})
    for status in ("", "deleted", "quarantined"):
        result = project_service.list_projects(db, users["admin"], status=status)
        assert not {item["id"] for item in result["items"]} & {projects["deleted"].id, projects["quarantined"].id}


@pytest.mark.parametrize("role", ["owner", "member", "admin", "super_admin"])
@pytest.mark.parametrize("operation", ["get", "activate", "archive", "rename", "delete"])
def test_business_project_operations_cannot_restore_or_mutate_quarantine(db, quarantine_scope, role, operation):
    users, projects = quarantine_scope
    project = projects["quarantined"]
    original_name = project.project_name
    with pytest.raises(NotFoundError):
        if operation == "get":
            project_service.get_project(db, users[role], project.id)
        elif operation == "delete":
            project_service.delete_project(db, users[role], project.id)
        else:
            payload = ProjectUpdateIn(project_name="不应修改") if operation == "rename" else ProjectUpdateIn(
                status="active" if operation == "activate" else "archived",
            )
            project_service.update_project(db, users[role], project.id, payload)
    db.expire_all()
    assert db.get(Project, project.id).status == "quarantined"
    assert db.get(Project, project.id).project_name == original_name


def test_archived_projects_remain_accessible_and_can_be_reactivated(db, quarantine_scope):
    users, projects = quarantine_scope
    project = projects["archived"]
    assert project_member_service.require_project_access(db, project.id, users["member"]) == "reviewer"
    project_service.update_project(db, users["owner"], project.id, ProjectUpdateIn(status="active"))
    assert project.status == "active"


@pytest.mark.parametrize("status", ["quarantined", "deleted", "active\n", "archived\n"])
def test_public_update_schema_rejects_internal_statuses(status):
    with pytest.raises(ValidationError):
        ProjectUpdateIn(status=status)


@pytest.mark.parametrize("operation", ["add", "remove", "role", "ensure_owner"])
def test_member_helpers_do_not_mutate_quarantined_projects(db, quarantine_scope, operation):
    users, projects = quarantine_scope
    project_id = projects["quarantined"].id
    with pytest.raises(NotFoundError):
        if operation == "add":
            project_member_service.add_member(db, project_id, users["outsider"].id)
        elif operation == "remove":
            project_member_service.remove_member(db, project_id, users["member"].id)
        elif operation == "role":
            project_member_service.update_member_role(db, project_id, users["member"].id, "owner")
        else:
            project_member_service.ensure_owner_member(db, project_id, users["owner"].id)


@pytest.mark.parametrize("status", ["deleted", "quarantined"])
def test_member_listing_does_not_expose_hidden_projects(db, quarantine_scope, status):
    _, projects = quarantine_scope
    assert project_member_service.list_members(db, projects[status].id) == []


@pytest.mark.parametrize("role", ["owner", "member", "admin", "super_admin"])
@pytest.mark.parametrize("status", ["deleted", "quarantined"])
def test_file_list_and_creation_gate_hide_projects(db, quarantine_scope, role, status):
    from app.services import code_file_service

    users, projects = quarantine_scope
    for operation in (code_file_service.list_files, code_file_service._check_project_access):
        with pytest.raises(NotFoundError):
            operation(db, users[role], projects[status].id)


@pytest.mark.parametrize("status", ["active", "archived"])
def test_reviewer_can_list_but_cannot_create_files(db, quarantine_scope, status):
    from app.services import code_file_service

    users, projects = quarantine_scope
    project_id = projects[status].id
    assert code_file_service.list_files(db, users["member"], project_id)["total"] == 0
    with pytest.raises(ForbiddenError):
        code_file_service._check_project_access(db, users["member"], project_id)
    assert code_file_service._check_project_access(db, users["owner"], project_id).id == project_id


@pytest.fixture
def file_access_scope(db, quarantine_scope):
    users, projects = quarantine_scope
    users["co_owner"] = User(id=706, username="quarantine_co_owner", password="test-only", role="user", status=1)
    db.add(users["co_owner"])
    files = {}
    for status, project in projects.items():
        db.add(ProjectMember(project_id=project.id, user_id=706, role_in_project="owner"))
        text_file = CodeFile(
            project_id=project.id, file_name="fixture.py", language="python",
            content="fixture_value = 2\n", status="active", version_no=2,
        )
        binary_file = CodeFile(
            project_id=project.id, file_name="fixture.bin", language="binary",
            content="fixture-encoded", status="active", is_binary=1, original_blob=b"fixture-only-bytes",
        )
        db.add_all([text_file, binary_file])
        db.flush()
        for number in (1, 2):
            db.add(CodeVersion(
                file_id=text_file.id, version_no=number, content=f"fixture_value = {number}\n",
                operator_id=users["owner"].id, create_time=text_file.create_time,
            ))
        files[status] = (text_file, binary_file)
    orphan = CodeFile(
        project_id=999999, file_name="orphan.py", language="python", content="fixture\n", status="active",
    )
    db.add(orphan)
    db.commit()
    files["missing"] = (orphan, orphan)
    return users, files


def _read_file_operation(db, actor, files, operation):
    from app.services import code_file_service

    text_file, binary_file = files
    if operation == "detail":
        return code_file_service.get_file(db, actor, text_file.id)
    if operation == "meta":
        return code_file_service.get_file_meta(db, actor, text_file.id)
    if operation == "download":
        return code_file_service.get_binary_content(db, actor, binary_file.id)
    if operation == "versions":
        return code_file_service.list_versions(db, actor, text_file.id)
    return code_file_service.get_version(db, actor, text_file.id, 1)


def _write_file_operation(db, actor, file_id, operation):
    from app.services import code_file_service

    if operation == "update":
        return code_file_service.update_content(db, actor, file_id, "fixture_value = 3\n")
    if operation == "rename":
        return code_file_service.rename_file(db, actor, file_id, "renamed_fixture.py")
    if operation == "delete":
        return code_file_service.delete_file(db, actor, file_id)
    return code_file_service.restore_version(db, actor, file_id, 1)


@pytest.mark.parametrize("role", ["owner", "co_owner", "member", "outsider", "admin", "super_admin"])
@pytest.mark.parametrize("status", ["deleted", "quarantined", "missing"])
@pytest.mark.parametrize("operation", ["detail", "meta", "download", "versions", "version"])
def test_file_read_endpoints_hide_project_for_every_role(db, file_access_scope, role, status, operation):
    users, files = file_access_scope
    with pytest.raises(NotFoundError):
        _read_file_operation(db, users[role], files[status], operation)


@pytest.mark.parametrize("role", ["owner", "co_owner", "member", "admin", "super_admin"])
@pytest.mark.parametrize("status", ["active", "archived"])
def test_file_read_endpoints_allow_visible_members(db, file_access_scope, role, status):
    users, files = file_access_scope
    actor = users[role]
    assert _read_file_operation(db, actor, files[status], "detail").content == "fixture_value = 2\n"
    assert _read_file_operation(db, actor, files[status], "meta")["id"] == files[status][0].id
    assert _read_file_operation(db, actor, files[status], "download") == (b"fixture-only-bytes", "fixture.bin")
    assert _read_file_operation(db, actor, files[status], "versions")["total"] == 2
    assert _read_file_operation(db, actor, files[status], "version").content == "fixture_value = 1\n"


@pytest.mark.parametrize("operation", ["detail", "meta", "download", "versions", "version"])
def test_file_read_endpoints_do_not_reveal_unrelated_projects(db, file_access_scope, operation):
    users, files = file_access_scope
    with pytest.raises(NotFoundError):
        _read_file_operation(db, users["outsider"], files["active"], operation)


@pytest.mark.parametrize("role", ["owner", "co_owner", "member", "admin", "super_admin"])
@pytest.mark.parametrize("status", ["deleted", "quarantined", "missing"])
@pytest.mark.parametrize("operation", ["update", "rename", "delete", "restore"])
def test_file_write_endpoints_hide_project_without_mutation(db, file_access_scope, role, status, operation):
    users, files = file_access_scope
    code_file = files[status][0]
    before = (code_file.content, code_file.file_name, code_file.status, code_file.version_no)
    versions_before = db.query(CodeVersion).filter_by(file_id=code_file.id).count()
    with pytest.raises(NotFoundError):
        _write_file_operation(db, users[role], code_file.id, operation)
    db.expire_all()
    assert (code_file.content, code_file.file_name, code_file.status, code_file.version_no) == before
    assert db.query(CodeVersion).filter_by(file_id=code_file.id).count() == versions_before


@pytest.mark.parametrize("status", ["active", "archived"])
@pytest.mark.parametrize("operation", ["update", "rename", "delete", "restore"])
def test_reviewer_cannot_write_despite_having_read_access(db, file_access_scope, status, operation):
    users, files = file_access_scope
    code_file = files[status][0]
    with pytest.raises(ForbiddenError):
        _write_file_operation(db, users["member"], code_file.id, operation)
    db.expire_all()
    assert (code_file.content, code_file.file_name, code_file.status, code_file.version_no) == (
        "fixture_value = 2\n", "fixture.py", "active", 2,
    )
    assert db.query(CodeVersion).filter_by(file_id=code_file.id).count() == 2


@pytest.mark.parametrize("role", ["owner", "co_owner", "admin", "super_admin"])
@pytest.mark.parametrize("status", ["active", "archived"])
@pytest.mark.parametrize("operation", ["update", "rename", "delete", "restore"])
def test_file_write_endpoints_allow_project_writers(db, file_access_scope, role, status, operation):
    users, files = file_access_scope
    code_file = files[status][0]
    _write_file_operation(db, users[role], code_file.id, operation)
    db.expire_all()
    if operation in {"update", "restore"}:
        assert code_file.version_no == 3
        assert db.query(CodeVersion).filter_by(file_id=code_file.id).count() == 3
        assert code_file.content == f"fixture_value = {3 if operation == 'update' else 1}\n"
    elif operation == "rename":
        assert code_file.file_name == "renamed_fixture.py"
    else:
        assert code_file.status == "deleted"


@pytest.mark.parametrize("operation", ["update", "rename", "delete", "restore"])
def test_file_write_rechecks_project_after_lock_instead_of_trusting_cached_status(
    db, file_access_scope, operation,
):
    users, files = file_access_scope
    code_file = files["active"][0]
    project = db.get(Project, code_file.project_id)
    db.execute(text("UPDATE project SET status='quarantined' WHERE id=:project_id"), {"project_id": project.id})
    db.commit()
    assert project.status == "active"
    with pytest.raises(NotFoundError):
        _write_file_operation(db, users["owner"], code_file.id, operation)
    db.expire_all()
    assert project.status == "quarantined"
    assert code_file.content == "fixture_value = 2\n"
    assert code_file.status == "active"
    assert code_file.version_no == 2
    assert db.query(CodeVersion).filter_by(file_id=code_file.id).count() == 2
