"""Project authorization for issue/task writes must be current under MySQL RR."""

from __future__ import annotations

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from test_mysql_pentest_revoked_project_access import _seed
from test_mysql_project_write_authority import _make

from app.core.dependencies import authenticate_access_token
from app.core.exceptions import AuthError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.security import create_access_token
from app.models import load_all_models
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.enterprise_project import EnterpriseProjectGrant
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import issue_service, review_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _call(db, actor, operation, task_id, issue_id):
    if operation == "issue_status":
        issue_service.update_status(db, actor, issue_id, "fixed")
    elif operation == "issue_decision":
        issue_service.review_decision(db, actor, issue_id, "accepted")
    elif operation == "task_cancel":
        review_service.cancel_task(db, actor, task_id)
    else:
        review_service.delete_task(db, actor, task_id)


@pytest.mark.parametrize("operation", ["issue_status", "issue_decision", "task_cancel", "task_delete"])
@pytest.mark.parametrize("change", ["owner", "token_version", "task_project"])
def test_project_and_token_changes_invalidate_review_writes(fresh_chain, operation, change):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, other_id, _, project_id, _, _ = _seed(factory)
    with factory.begin() as db:
        task = ReviewTask(user_id=owner_id, project_id=project_id, task_name="scope probe", status="running")
        db.add(task)
        db.flush()
        issue = ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="synthetic")
        other = Project(user_id=other_id, project_name=f"other-scope-{task.id}", status="active")
        db.add_all([issue, other])
        db.flush()
        task_id, issue_id, other_project_id = task.id, issue.id, other.id
    token = create_access_token(owner_id, "user", token_version=0)
    with factory() as stale:
        actor = authenticate_access_token(token, stale)
        stale.get(Project, project_id)
        stale.get(ReviewTask, task_id)
        stale.get(ReviewIssue, issue_id)
        with factory.begin() as db:
            if change == "owner":
                db.get(Project, project_id).user_id = other_id
            elif change == "token_version":
                db.get(User, owner_id).token_version += 1
            else:
                db.get(ReviewTask, task_id).project_id = other_project_id
        with pytest.raises((AuthError, ConflictError, ForbiddenError, NotFoundError, ValidationError)):
            _call(stale, actor, operation, task_id, issue_id)


@pytest.mark.parametrize("operation", ["issue_status", "issue_decision", "task_cancel", "task_delete"])
@pytest.mark.parametrize("change", ["grant", "group_role", "group_status", "member"])
def test_enterprise_revocation_invalidates_review_writes(fresh_chain, operation, change):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        data = _make(db, enterprise=True, group_role="leader" if change == "group_role" else "member",
                     grant_role="editor" if change != "group_role" else None)
        task = ReviewTask(user_id=data["actor"], project_id=data["project"], status="running")
        db.add(task)
        db.flush()
        issue = ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="synthetic")
        db.add(issue)
        db.flush()
        task_id, issue_id = task.id, issue.id
    with factory() as stale:
        actor = stale.get(User, data["actor"])
        stale.get(Project, data["project"])
        stale.get(ReviewTask, task_id)
        stale.get(ReviewIssue, issue_id)
        stale.query(EnterpriseMember).filter_by(enterprise_id=data["company"], user_id=actor.id).first()
        with factory.begin() as db:
            if change == "grant":
                grant = db.query(EnterpriseProjectGrant).filter_by(project_id=data["project"], user_id=actor.id).one()
                grant.status = "inactive"
            elif change == "group_role":
                member = db.query(EnterpriseGroupMember).filter_by(group_id=data["group"], user_id=actor.id).one()
                member.role = "member"
            elif change == "group_status":
                db.get(EnterpriseGroup, data["group"]).status = "inactive"
            else:
                member = db.query(EnterpriseMember).filter_by(enterprise_id=data["company"], user_id=actor.id).one()
                member.status = "removed"
        with pytest.raises((AuthError, ConflictError, ForbiddenError, NotFoundError, ValidationError)):
            _call(stale, actor, operation, task_id, issue_id)


@pytest.mark.parametrize("operation", ["issue_status", "issue_decision"])
def test_issue_reassigned_to_another_task_does_not_use_old_authorization(fresh_chain, operation):
    load_all_models()
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    owner_id, other_id, _, project_id, _, _ = _seed(factory)
    with factory.begin() as db:
        task = ReviewTask(user_id=owner_id, project_id=project_id, status="running")
        other_project = Project(user_id=other_id, project_name="other-issue-scope", status="active")
        db.add_all([task, other_project])
        db.flush()
        issue = ReviewIssue(task_id=task.id, issue_type="probe", severity="低", description="synthetic")
        other_task = ReviewTask(user_id=other_id, project_id=other_project.id, status="running")
        db.add_all([issue, other_task])
        db.flush()
        task_id, issue_id, other_task_id = task.id, issue.id, other_task.id
    with factory() as stale:
        actor = authenticate_access_token(create_access_token(owner_id, "user", token_version=0), stale)
        stale.get(ReviewIssue, issue_id)
        stale.get(ReviewTask, task_id)
        with factory.begin() as db:
            db.get(ReviewIssue, issue_id).task_id = other_task_id
        with pytest.raises((AuthError, ConflictError, ForbiddenError, NotFoundError)):
            _call(stale, actor, operation, task_id, issue_id)
