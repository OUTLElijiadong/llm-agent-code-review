"""小菱查询审查问题必须处理服务返回的真实 ORM 记录。"""
import pytest

from app.agents.review_orchestrator_agent import ReviewOrchestratorAgent
from app.core.exceptions import NotFoundError
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User


def _rows(db):
    owner = User(username="issue_tool_owner", password="isolated", role="user", status=1)
    outsider = User(username="issue_tool_outsider", password="isolated", role="user", status=1)
    db.add_all([owner, outsider])
    db.flush()
    project = Project(user_id=owner.id, project_name="tool review", status="active")
    db.add(project)
    db.flush()
    task = ReviewTask(user_id=owner.id, project_id=project.id, status="success")
    db.add(task)
    db.flush()
    issue = ReviewIssue(task_id=task.id, file_name="source.py", line_number=7, severity="高",
                        issue_type="安全漏洞", title="不安全拼接", description="test", status="unfixed")
    db.add(issue)
    db.commit()
    return owner, outsider, task, issue


def test_list_issues_serializes_nonempty_orm_page(db):
    owner, _, task, issue = _rows(db)
    agent = ReviewOrchestratorAgent()
    agent.inject(db, owner)
    result = agent.list_issues(task.id)
    assert result.success is True
    assert result.data == {"total": 1, "items": [{
        "id": issue.id, "file_name": "source.py", "line_number": 7,
        "severity": "高", "issue_type": "安全漏洞", "title": "不安全拼接", "status": "unfixed",
    }]}


def test_list_issues_preserves_project_access_check(db):
    _, outsider, task, _ = _rows(db)
    agent = ReviewOrchestratorAgent()
    agent.inject(db, outsider)
    with pytest.raises(NotFoundError):
        agent.list_issues(task.id)
