"""Resource flags mirror existing rights; viewer never widens report privacy."""

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import issue_service, project_service, rbac_service, report_service, review_service


def seed(db, role="viewer", *, authored=False):
    owner = User(id=7201, username="resource-owner", password="local", role="user", status=1)
    actor = User(id=7202, username="resource-actor", password="local", role="user", status=1)
    project = Project(id=7201, user_id=owner.id, project_name="shared source", status="active")
    member = ProjectMember(project_id=project.id, user_id=actor.id, role_in_project=role)
    task = ReviewTask(id=7201, project_id=project.id, user_id=actor.id if authored else owner.id,
                      task_name="local task", review_type="standard", status="success", total_files=1,
                      processed_files=1, score=88, execution_token="local-lease")
    issue = ReviewIssue(id=7201, task_id=task.id, file_name="local.py", issue_type="代码规范",
                        severity="低", title="local claim", description="local", status="unfixed")
    db.add_all([owner, actor, project, member, task, issue]); db.commit()
    return owner, actor, project, task, issue


@pytest.mark.parametrize("role,can_execute,can_write", [
    ("viewer", False, False), ("reviewer", True, False), ("owner", True, True),
])
def test_project_task_and_issue_flags_follow_project_role_without_global_grants(
    db, monkeypatch, role, can_execute, can_write,
):
    _, actor, project, task, issue = seed(db, role)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_args: True)
    projects = [project_service.get_project(db, actor, project.id),
                project_service.list_projects(db, actor)["items"][0]]
    for item in projects:
        assert item["can_execute"] is can_execute
        assert item["can_update"] is can_write
        assert item["can_delete"] is can_write
    tasks = [review_service.get_task_detail(db, actor, task.id),
             review_service.list_tasks(db, actor)["items"][0]]
    for item in tasks:
        assert item["can_execute"] is can_execute
        assert item["can_cancel"] is can_write
    detail = issue_service.get_issue(db, actor, issue.id)
    assert detail.can_handle is can_write
    assert detail.can_execute is can_execute
    listed = issue_service.list_issues(db, actor)["items"][0]
    assert listed["can_handle"] is can_write
    assert listed["can_execute"] is can_execute
    task_issue = review_service.list_task_issues(db, actor, task.id)["items"][0]
    assert task_issue.can_handle is can_write
    assert task_issue.can_execute is can_execute


@pytest.mark.parametrize("role,can_delete", [("viewer", False), ("reviewer", True)])
def test_report_author_delete_cap_preserves_reviewer_and_denies_viewer(db, monkeypatch, role, can_delete):
    _, actor, _, task, _ = seed(db, role, authored=True)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_args: True)
    assert report_service.list_reports(db, actor)["items"][0]["can_delete"] is can_delete
    assert report_service.get_report_detail(db, actor, task.id)["can_delete"] is can_delete
    if not can_delete:
        with pytest.raises(ForbiddenError):
            report_service.delete_report(db, actor, task.id)
        db.refresh(task)
        assert task.status == "success"
    else:
        report_service.delete_report(db, actor, task.id)
        assert task.status == "deleted"


def test_viewer_project_visibility_does_not_grant_others_private_report(db, monkeypatch):
    _, actor, project, task, _ = seed(db, "viewer")
    task.review_type = "sandbox_test"; task.summary = "owner-private-report"; db.commit()
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_args: True)
    assert report_service.list_reports(db, actor)["items"] == []
    with pytest.raises(NotFoundError):
        report_service.get_report_detail(db, actor, task.id)
    detail = review_service.get_task_detail(db, actor, task.id)
    assert detail["summary"] is None
    assert detail["score"] is None
    assert detail["can_view_report"] is False
    assert project_service.get_project(db, actor, project.id)["recent_tasks"][0]["score"] is None


def test_missing_global_report_delete_permission_keeps_cap_false(db, monkeypatch):
    _, actor, _, task, _ = seed(db, "reviewer", authored=True)
    monkeypatch.setattr(rbac_service, "check_permission", lambda *_args: False)
    assert report_service.list_reports(db, actor)["items"][0]["can_delete"] is False
    assert report_service.get_report_detail(db, actor, task.id)["can_delete"] is False


def test_new_project_response_keeps_owner_execution_capability(db):
    from app.schemas.project import ProjectIn, ProjectOut
    owner, _, _, _, _ = seed(db)
    created = project_service.create_project(db, owner, ProjectIn(project_name="new local owner scope"))
    assert ProjectOut.model_validate(created).can_execute is True
