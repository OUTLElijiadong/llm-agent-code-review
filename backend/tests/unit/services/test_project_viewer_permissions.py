"""Project viewer is a resource restriction, not a replacement global role."""

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User
from app.schemas.project_member import MemberAddIn, MemberRoleUpdateIn
from app.services import project_member_service as members


def seed(db, role="viewer"):
    owner = User(id=7101, username="viewer-owner", password="local", role="user", status=1)
    actor = User(id=7102, username="viewer-actor", password="local", role="user", status=1)
    project = Project(id=7101, user_id=owner.id, project_name="local read scope", status="active")
    member = ProjectMember(project_id=project.id, user_id=actor.id, role_in_project=role)
    db.add_all([owner, actor, project, member]); db.commit()
    return owner, actor, project, member


def execution(db, project, actor):
    helper = getattr(members, "require_project_execution", None)
    assert callable(helper), "project execution needs a separate role allowlist"
    return helper(db, project.id, actor)


def test_member_schemas_accept_viewer_without_changing_existing_default():
    assert MemberAddIn(user_id=1).role_in_project == "reviewer"
    assert MemberAddIn(user_id=1, role_in_project="viewer").role_in_project == "viewer"
    assert MemberRoleUpdateIn(role_in_project="viewer").role_in_project == "viewer"


def test_viewer_reads_current_project_and_denies_write_execution_and_other_project(db):
    owner, actor, project, _ = seed(db)
    assert members.require_project_access(db, project.id, actor) == "viewer"
    assert members.get_visible_project_ids(db, actor) == ([project.id], "self")
    with pytest.raises(ForbiddenError):
        members.require_project_access(db, project.id, actor, need_write=True)
    with pytest.raises(ForbiddenError):
        execution(db, project, actor)
    other = Project(id=7102, user_id=owner.id, project_name="not shared", status="active")
    db.add(other); db.commit()
    with pytest.raises(NotFoundError):
        members.require_project_access(db, other.id, actor)


@pytest.mark.parametrize("role", ["reviewer", "owner"])
def test_existing_project_roles_keep_execution_and_write_limits(db, role):
    _, actor, project, _ = seed(db, role)
    assert execution(db, project, actor) == role
    if role == "owner":
        assert members.require_project_access(db, project.id, actor, need_write=True) == role
    else:
        with pytest.raises(ForbiddenError):
            members.require_project_access(db, project.id, actor, need_write=True)


def test_actual_project_owner_and_admin_keep_existing_access(db):
    owner, _, project, _ = seed(db)
    admin = User(id=7103, username="local-admin", password="local", role="admin", status=1)
    db.add(admin); db.commit()
    assert execution(db, project, owner) == "owner"
    assert execution(db, project, admin) == "admin"


@pytest.mark.parametrize("role", ["unrecognized", "admin", "", "VIEWER"])
def test_unknown_persisted_member_role_is_not_a_read_or_write_grant(db, role):
    _, actor, project, _ = seed(db, role)
    assert members.is_project_member(db, project.id, actor) == (False, "")
    assert members.get_visible_project_ids(db, actor) == ([], "self")
    with pytest.raises(NotFoundError):
        members.require_project_access(db, project.id, actor)
    with pytest.raises(NotFoundError):
        members.require_project_access(db, project.id, actor, need_write=True)


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("invalid_role", ["unrecognized", "admin", ""])
def test_direct_member_mutators_reject_unknown_role_without_changing_rows(db, operation, invalid_role):
    owner, actor, project, member = seed(db, "reviewer")
    if operation == "add":
        target = User(id=7103, username="extra-local", password="local", role="user", status=1)
        db.add(target); db.commit()
        with pytest.raises(BadRequestError):
            members.add_member(db, project.id, target.id, invalid_role, operator=owner)
        assert db.query(ProjectMember).filter_by(user_id=target.id).count() == 0
    else:
        with pytest.raises(BadRequestError):
            members.update_member_role(db, project.id, actor.id, invalid_role, operator=owner)
        db.refresh(member)
        assert member.role_in_project == "reviewer"


def test_existing_session_observes_role_downgrade_before_execution(db):
    _, actor, project, cached = seed(db, "reviewer")
    assert members.is_project_member(db, project.id, actor) == (True, "reviewer")
    with Session(bind=db.get_bind(), expire_on_commit=False) as writer:
        writer.query(ProjectMember).filter_by(id=cached.id).update({"role_in_project": "viewer"})
        writer.commit()
    assert cached.role_in_project == "reviewer"
    assert members.is_project_member(db, project.id, actor) == (True, "viewer")
    with pytest.raises(ForbiddenError):
        execution(db, project, actor)


def test_owner_can_add_viewer_then_update_reviewer_to_viewer(db):
    owner, actor, project, member = seed(db, "reviewer")
    members.update_member_role(db, project.id, actor.id, "viewer", operator=owner)
    db.refresh(member)
    assert member.role_in_project == "viewer"
    target = User(id=7103, username="extra-viewer", password="local", role="user", status=1)
    db.add(target); db.commit()
    added = members.add_member(db, project.id, target.id, "viewer", operator=owner)
    assert added.role_in_project == "viewer"
