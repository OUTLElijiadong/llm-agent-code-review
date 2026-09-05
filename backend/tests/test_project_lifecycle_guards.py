import pytest
from sqlalchemy import text

from app.core.exceptions import ConflictError, NotFoundError
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.schemas.project import ProjectUpdateIn
from app.services import project_service


@pytest.fixture
def project_with_task(db, admin_user):
    project = Project(user_id=admin_user.id, project_name="隔离删除回归", status="active")
    db.add(project)
    db.flush()
    task = ReviewTask(user_id=admin_user.id, project_id=project.id, status="running", total_files=1)
    db.add(task)
    db.commit()
    return project, task


@pytest.mark.parametrize("status", ["pending", "running"])
def test_delete_requires_active_reviews_to_be_cancelled_first(db, admin_user, project_with_task, status):
    project, task = project_with_task
    task.status = status
    db.commit()
    with pytest.raises(ConflictError, match="进行中.*取消"):
        project_service.delete_project(db, admin_user, project.id)
    db.refresh(project)
    db.refresh(task)
    assert project.status == "active"
    assert task.status == status


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "deleted"])
def test_delete_preserves_terminal_review_history(db, admin_user, project_with_task, status):
    project, task = project_with_task
    task.status = status
    db.commit()
    project_service.delete_project(db, admin_user, project.id)
    db.refresh(task)
    assert project.status == "deleted"
    assert task.status == status
    assert db.query(ReviewTask).filter_by(project_id=project.id).count() == 1


@pytest.mark.parametrize("status", ["quarantined", "deleted"])
@pytest.mark.parametrize("operation", ["rename", "activate", "delete"])
def test_stale_project_cache_cannot_mutate_hidden_rows(db, admin_user, project_with_task, status, operation):
    project, task = project_with_task
    task.status = "success"
    db.commit()
    project_id = project.id
    original_name = project.project_name
    db.execute(text("UPDATE project SET status=:status WHERE id=:project_id"),
               {"status": status, "project_id": project_id})
    assert project.status == "active"
    with pytest.raises(NotFoundError):
        if operation == "delete":
            project_service.delete_project(db, admin_user, project_id)
        else:
            payload = (ProjectUpdateIn(status="active") if operation == "activate"
                       else ProjectUpdateIn(project_name="不应写入"))
            project_service.update_project(db, admin_user, project_id, payload)
    db.refresh(project)
    assert project.status == status
    assert project.project_name == original_name
