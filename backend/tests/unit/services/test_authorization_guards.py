"""对象级授权回归测试。"""
import pytest

from app.agents.discussion_bus import DiscussionSession
from app.api.v1.discussion import start_discussion
from app.api.v1.ws_discussion import _can_access_session
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.rbac import Role, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_rule import ReviewRule
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.rule import RuleUpdateIn
from app.services import issue_service, review_service, rule_service


def _user(db, username: str, role: str = "user") -> User:
    """创建测试用户。

    Args:
        db: 测试数据库会话。
        username: 用户名。
        role: 用户角色。

    Returns:
        User: 已入库用户。
    """
    row = User(username=username, password="x", role=role, status=1)
    db.add(row)
    db.flush()
    if role in {"admin", "reviewer", "super_admin"}:
        role_row = db.query(Role).filter(Role.code == role).one_or_none()
        if role_row is None:
            role_row = Role(name=role, code=role, status="active", is_builtin=1)
            db.add(role_row)
            db.flush()
        db.add(UserRole(user_id=row.id, role_id=role_row.id))
    db.commit()
    db.refresh(row)
    return row


def _project(db, owner: User) -> Project:
    """创建测试项目。

    Args:
        db: 测试数据库会话。
        owner: 项目所有者。

    Returns:
        Project: 已入库项目。
    """
    row = Project(user_id=owner.id, project_name=f"{owner.username}-proj", language="python", status="active")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _code_file(db, project: Project) -> CodeFile:
    """创建测试代码文件。

    Args:
        db: 测试数据库会话。
        project: 所属项目。

    Returns:
        CodeFile: 已入库代码文件。
    """
    row = CodeFile(
        project_id=project.id,
        file_name="app.py",
        file_path="app.py",
        language="python",
        content="print('ok')\n",
        size_bytes=12,
        line_count=1,
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _task_with_issue(db, owner: User, project: Project, code_file: CodeFile) -> tuple[ReviewTask, ReviewIssue]:
    """创建测试审查任务和问题。

    Args:
        db: 测试数据库会话。
        owner: 任务所有者。
        project: 所属项目。
        code_file: 关联文件。

    Returns:
        tuple[ReviewTask, ReviewIssue]: 任务与问题。
    """
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name="review",
        review_type="standard",
        status="success",
        total_files=1,
        total_issues=1,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    issue = ReviewIssue(
        task_id=task.id,
        file_id=code_file.id,
        file_name=code_file.file_name,
        issue_type="安全漏洞",
        severity="高",
        title="越权问题",
        description="desc",
        status="unfixed",
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return task, issue


def test_issue_detail_requires_task_owner(db):
    """问题详情必须校验所属任务 owner。"""
    owner = _user(db, "owner")
    other = _user(db, "other")
    project = _project(db, owner)
    code_file = _code_file(db, project)
    _task, issue = _task_with_issue(db, owner, project, code_file)

    assert issue_service.get_issue(db, owner, issue.id).id == issue.id
    with pytest.raises(NotFoundError):
        issue_service.get_issue(db, other, issue.id)


def test_task_issues_require_task_owner(db):
    """任务问题列表不能被其他用户按 task_id 枚举。"""
    owner = _user(db, "task-owner")
    other = _user(db, "task-other")
    project = _project(db, owner)
    code_file = _code_file(db, project)
    task, _issue = _task_with_issue(db, owner, project, code_file)

    data = review_service.list_task_issues(db, owner, task.id)
    assert data["total"] == 1
    with pytest.raises(NotFoundError):
        review_service.list_task_issues(db, other, task.id)


def test_rule_mutation_requires_owner_or_admin(db):
    """规则写操作必须限制为 owner 或管理员。"""
    owner = _user(db, "rule-owner")
    other = _user(db, "rule-other")
    admin = _user(db, "rule-admin", role="admin")
    custom = ReviewRule(
        user_id=owner.id,
        rule_code="owner_rule",
        rule_name="Owner Rule",
        rule_type="security",
        rule_content="检查",
        enabled=1,
        is_builtin=0,
        language="*",
        severity="中",
        sort_order=1,
    )
    builtin = ReviewRule(
        user_id=None,
        rule_code="builtin_rule",
        rule_name="Builtin Rule",
        rule_type="security",
        rule_content="检查",
        enabled=1,
        is_builtin=1,
        language="*",
        severity="高",
        sort_order=2,
    )
    db.add_all([custom, builtin])
    db.commit()

    with pytest.raises(ForbiddenError):
        rule_service.toggle_rule(db, other, custom.id, 0)
    with pytest.raises(ForbiddenError):
        rule_service.toggle_rule(db, other, builtin.id, 0)

    rule_service.toggle_rule(db, admin, builtin.id, 0)
    db.refresh(builtin)
    assert builtin.enabled == 0

    rule_service.update_rule(db, owner, custom.id, RuleUpdateIn(rule_name="Owner Rule v2"))
    db.refresh(custom)
    assert custom.rule_name == "Owner Rule v2"


def test_discussion_start_requires_project_owner(db, monkeypatch):
    """讨论审预检必须与普通审查一样校验项目归属。"""
    owner = _user(db, "disc-owner")
    other = _user(db, "disc-other")
    project = _project(db, owner)
    code_file = _code_file(db, project)

    registered = []
    monkeypatch.setattr("app.api.v1.discussion.register_pending", lambda **kwargs: registered.append(kwargs))
    with pytest.raises(NotFoundError):
        start_discussion(project_id=project.id, file_id=code_file.id, review_type="full", db=db, user=other)
    assert not registered


@pytest.mark.parametrize("content,binary", [("", 0), (" \t\n\u3000", 0), ("encoded", 1)])
def test_discussion_preflight_rejects_empty_scan_before_registration(db, monkeypatch, content, binary):
    owner = _user(db, "empty-disc-owner")
    project = _project(db, owner)
    code_file = _code_file(db, project)
    code_file.content = content
    code_file.is_binary = binary
    db.commit()
    registered = []
    monkeypatch.setattr("app.api.v1.discussion.register_pending", lambda **kwargs: registered.append(kwargs))
    with pytest.raises(ValidationError, match="有效非空"):
        start_discussion(project_id=project.id, file_id=code_file.id, review_type="full", db=db, user=owner)
    assert not registered


def test_discussion_session_access_requires_owner_or_admin(db):
    """WebSocket 订阅必须绑定讨论会话 owner。"""
    owner = _user(db, "ws-owner")
    other = _user(db, "ws-other")
    admin = _user(db, "ws-admin", role="admin")
    session = DiscussionSession(session_id="disc_x", task_id=1, file_name="app.py", owner_user_id=owner.id)

    assert _can_access_session(owner, session.owner_user_id, db) is True
    assert _can_access_session(admin, session.owner_user_id, db) is True
    assert _can_access_session(other, session.owner_user_id, db) is False
    assert _can_access_session(owner, 0, db) is False
