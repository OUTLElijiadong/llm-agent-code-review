"""核心业务服务覆盖率补全测试。

覆盖鉴权、用户管理、项目管理和问题管理中此前未执行的正常、边界、
权限与异常分支。所有数据库操作使用项目公共的内存 SQLite fixture。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import AuthError, ConflictError, ForbiddenError, NotFoundError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Role
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.auth import RegisterIn
from app.schemas.project import ProjectIn, ProjectUpdateIn
from app.services import auth_service, issue_service, project_service, rbac_service, user_service


def _make_user(
    db,
    username: str,
    *,
    role: str = "user",
    status: int = 1,
    password: str = "stored-hash",
    nickname: str | None = None,
) -> User:
    """创建并持久化测试用户。

    Args:
        db: 测试数据库会话。
        username: 唯一用户名。
        role: 用户角色。
        status: 启用状态。
        password: 存储密码值。
        nickname: 可选昵称。

    Returns:
        User: 已持久化并刷新主键的用户。
    """
    user = User(
        username=username,
        password=password,
        email=f"{username}@example.com",
        nickname=nickname or username,
        role=role,
        status=status,
        token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_project(
    db,
    owner: User,
    name: str,
    *,
    language: str = "python",
    status: str = "active",
) -> Project:
    """创建并持久化测试项目。

    Args:
        db: 测试数据库会话。
        owner: 项目所有者。
        name: 项目名称。
        language: 主语言。
        status: 项目状态。

    Returns:
        Project: 已持久化的项目。
    """
    project = Project(
        user_id=owner.id,
        project_name=name,
        description=f"{name} description",
        language=language,
        status=status,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_task(
    db,
    owner: User,
    project: Project,
    name: str,
    *,
    status: str = "success",
    score: int = 80,
    created_at: datetime | None = None,
) -> ReviewTask:
    """创建测试审查任务。

    Args:
        db: 测试数据库会话。
        owner: 任务创建用户。
        project: 所属项目。
        name: 任务名称。
        status: 任务状态。
        score: 审查评分。
        created_at: 可选创建时间。

    Returns:
        ReviewTask: 已持久化任务。
    """
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name=name,
        review_type="standard",
        status=status,
        total_files=1,
        processed_files=1,
        total_issues=0,
        score=score,
    )
    if created_at is not None:
        task.create_time = created_at
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_issue(
    db,
    task: ReviewTask,
    *,
    title: str,
    severity: str = "高",
    issue_type: str = "安全漏洞",
    status: str = "unfixed",
) -> ReviewIssue:
    """创建带完整安全元数据的测试问题。

    Args:
        db: 测试数据库会话。
        task: 所属审查任务。
        title: 问题标题。
        severity: 严重程度。
        issue_type: 问题类型。
        status: 处理状态。

    Returns:
        ReviewIssue: 已持久化的问题。
    """
    issue = ReviewIssue(
        task_id=task.id,
        file_name="app.py",
        line_number=7,
        end_line=9,
        issue_type=issue_type,
        severity=severity,
        title=title,
        description=f"{title} description",
        suggestion="修复建议",
        fixed_code="safe()",
        status=status,
        owasp="A03:2021-Injection",
        cwe="CWE-89",
        evidence="cursor.execute(sql)",
        exploit_scenario="攻击者注入 SQL",
        references_json=["https://owasp.org"],
        confidence=0.92,
        source="static",
        cvss_score=8.8,
        cvss_vector="AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
        compliance_mapping={"iso27001": ["A.8.28"]},
        remediation="使用参数化查询",
        static_rule_hits=2,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


def test_register_creates_user_and_assigns_default_role(db, monkeypatch):
    """注册应哈希密码、使用用户名兜底昵称并分配默认 RBAC 角色。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言注册结果和角色分配调用。
    """
    role = Role(name="普通用户", code="user", status="active", sort=1, is_builtin=1)
    db.add(role)
    db.commit()
    assigned: list[tuple[int, list[int]]] = []
    monkeypatch.setattr(auth_service, "hash_password", lambda raw: f"hashed:{raw}")
    monkeypatch.setattr(
        rbac_service,
        "assign_roles_to_user",
        lambda _db, user_id, role_ids, **_kwargs: assigned.append((user_id, role_ids)),
    )

    user = auth_service.register(
        db,
        RegisterIn(username="new-user", password="secret1", email="new@example.com"),
    )

    assert user.password == "hashed:secret1"
    assert user.nickname == "new-user"
    assert user.role == "user"
    assert assigned == [(user.id, [role.id])]


def test_register_rejects_duplicate_and_allows_missing_default_role(db, monkeypatch):
    """注册应拒绝重复用户名，且缺少默认角色时仍能创建用户。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言冲突异常和无角色分支。
    """
    _make_user(db, "duplicate")
    monkeypatch.setattr(auth_service, "hash_password", lambda raw: f"hashed:{raw}")

    with pytest.raises(ConflictError):
        auth_service.register(
            db,
            RegisterIn(username="duplicate", password="secret1", email="dup@example.com"),
        )

    created = auth_service.register(
        db,
        RegisterIn(username="roleless", password="secret1", nickname="自定义昵称"),
    )
    assert created.nickname == "自定义昵称"


def test_login_covers_invalid_disabled_and_success_paths(db, monkeypatch):
    """登录应区分无效凭证、禁用账号并在成功时更新登录时间和签发令牌。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言异常类型、令牌参数和数据库状态。
    """
    active = _make_user(db, "active", password="active-hash")
    active.token_version = 3
    _make_user(db, "disabled", status=0, password="disabled-hash")
    db.commit()

    monkeypatch.setattr(auth_service, "verify_password", lambda raw, stored: raw == stored.replace("-hash", "-pwd"))
    token_calls: list[tuple[int, str, int]] = []

    def fake_create_access_token(user_id: int, role: str, token_version: int) -> str:
        """记录令牌签发参数并返回固定令牌。

        Args:
            user_id: 用户主键。
            role: 用户角色。
            token_version: 令牌版本。

        Returns:
            str: 固定测试令牌。
        """
        token_calls.append((user_id, role, token_version))
        return "jwt-token"

    monkeypatch.setattr(auth_service, "create_access_token", fake_create_access_token)

    with pytest.raises(AuthError):
        auth_service.login(db, "missing", "anything")
    with pytest.raises(AuthError):
        auth_service.login(db, "active", "wrong")
    with pytest.raises(ForbiddenError):
        auth_service.login(db, "disabled", "disabled-pwd")

    token, logged_in = auth_service.login(db, "active", "active-pwd")
    db.refresh(active)
    assert token == "jwt-token"
    assert logged_in.id == active.id
    assert active.last_login is not None
    assert token_calls == [(active.id, "user", 4)]
    assert active.token_version == 4


def test_change_password_validates_old_password_and_revokes_tokens(db, monkeypatch):
    """修改密码应校验旧密码、写入新哈希并递增令牌版本。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言错误分支和成功副作用。
    """
    user = _make_user(db, "change-password", password="old-hash")
    monkeypatch.setattr(
        auth_service,
        "verify_password",
        lambda raw, stored: raw == "old-password" and stored == "old-hash",
    )
    monkeypatch.setattr(auth_service, "hash_password", lambda raw: f"new-hash:{raw}")

    with pytest.raises(AuthError):
        auth_service.change_password(db, user, "wrong", "new-password")

    auth_service.change_password(db, user, "old-password", "new-password")
    db.refresh(user)
    assert user.password == "new-hash:new-password"
    assert user.token_version == 1


def test_list_users_filters_keyword_role_status_and_paginates(db):
    """用户列表应组合搜索、角色、状态过滤并返回规范分页数据。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言过滤和分页结果。
    """
    _make_user(db, "alpha", nickname="Alpha")
    _make_user(db, "beta", role="reviewer", status=0, nickname="Search Target")
    _make_user(db, "gamma", role="reviewer", status=1)

    filtered = user_service.list_users(
        db,
        keyword="Target",
        role="reviewer",
        status="0",
        page=1,
        page_size=1,
    )
    assert filtered["total"] == 1
    assert [item.username for item in filtered["items"]] == ["beta"]
    assert filtered["pages"] == 1

    active_fallback = user_service.list_users(db, role="reviewer", status="invalid")
    assert [item.username for item in active_fallback["items"]] == ["gamma"]


def test_user_management_mutations_cover_success_and_missing_users(db, monkeypatch):
    """密码重置、状态切换和角色设置应持久化并处理不存在用户。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言状态变化、令牌吊销和缺失资源异常。
    """
    user = _make_user(db, "managed")
    monkeypatch.setattr(user_service, "hash_password", lambda raw: f"hashed:{raw}")

    result = user_service.reset_password(db, user.id)
    db.refresh(user)
    temporary_password = result["temporary_password"]
    assert len(temporary_password) == 24
    assert temporary_password != "123456"
    assert user.password == f"hashed:{temporary_password}"
    assert user.token_version == 1

    user_service.toggle_status(db, user.id, 0)
    db.refresh(user)
    assert user.status == 0
    assert user.token_version == 2

    user_service.toggle_status(db, user.id, 1)
    db.refresh(user)
    assert user.status == 1
    assert user.token_version == 2

    user_service.set_role(db, user.id, "reviewer")
    db.refresh(user)
    assert user.role == "reviewer"

    for operation in (
        lambda: user_service.reset_password(db, 999_001),
        lambda: user_service.toggle_status(db, 999_001, 0),
        lambda: user_service.set_role(db, 999_001, "admin"),
    ):
        with pytest.raises(NotFoundError):
            operation()


def test_project_crud_and_listing_expose_real_aggregates(db):
    """项目服务应创建 owner 关系并返回真实文件数、最近评分和详情。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言 CRUD、聚合字段和软删除行为。
    """
    owner = _make_user(db, "project-owner")
    project = project_service.create_project(
        db,
        owner,
        ProjectIn(project_name="Coverage Project", description="before", language="python"),
    )
    member = db.query(ProjectMember).filter_by(project_id=project.id, user_id=owner.id).one()
    assert member.role_in_project == "owner"

    with pytest.raises(ConflictError):
        project_service.create_project(
            db,
            owner,
            ProjectIn(project_name="Coverage Project", language="python"),
        )

    active_file = CodeFile(
        project_id=project.id,
        file_name="main.py",
        file_path="main.py",
        language="python",
        content="print('ok')\n",
        size_bytes=12,
        line_count=1,
        status="active",
    )
    deleted_file = CodeFile(
        project_id=project.id,
        file_name="old.py",
        file_path="old.py",
        language="python",
        content="pass\n",
        size_bytes=5,
        line_count=1,
        status="deleted",
    )
    db.add_all([active_file, deleted_file])
    now = datetime.now(timezone.utc)
    _make_task(db, owner, project, "older", score=70, created_at=now - timedelta(hours=1))
    latest = _make_task(db, owner, project, "latest", score=96, created_at=now)
    _make_task(db, owner, project, "failed", status="failed", score=0, created_at=now + timedelta(hours=1))
    db.commit()

    listing = project_service.list_projects(
        db,
        owner,
        keyword="Coverage",
        language="python",
        status="active",
        page=1,
        page_size=10,
    )
    assert listing["total"] == 1
    assert listing["items"][0]["file_count"] == 1
    assert listing["items"][0]["score"] == 96
    assert listing["items"][0]["last_review_at"] == latest.create_time

    detail = project_service.get_project(db, owner, project.id)
    assert detail["file_count"] == 1
    assert {item["status"] for item in detail["recent_tasks"]} == {"success", "failed"}

    updated = project_service.update_project(
        db,
        owner,
        project.id,
        ProjectUpdateIn(project_name="Coverage Project v2", description="after", language="go", status="archived"),
    )
    assert (updated.project_name, updated.description, updated.language, updated.status) == (
        "Coverage Project v2",
        "after",
        "go",
        "archived",
    )

    archived_listing = project_service.list_projects(db, owner, status="")
    assert archived_listing["total"] == 1

    project_service.delete_project(db, owner, project.id)
    db.refresh(project)
    assert project.status == "deleted"
    assert project_service.list_projects(db, owner, status="")["total"] == 0


def test_project_listing_handles_empty_result_and_partial_update(db):
    """项目列表空结果和仅更新单字段时应保持稳定。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言空分页及未提供字段保持不变。
    """
    owner = _make_user(db, "empty-owner")
    assert project_service.list_projects(db, owner)["items"] == []

    project = _make_project(db, owner, "Partial", language="java")
    updated = project_service.update_project(
        db,
        owner,
        project.id,
        ProjectUpdateIn(description="only-description"),
    )
    assert updated.project_name == "Partial"
    assert updated.language == "java"
    assert updated.description == "only-description"


def test_issue_service_covers_detail_status_filters_and_batch_update(db):
    """问题服务应校验任务、更新处理人并支持全部筛选和批量状态变更。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言详情、过滤、元数据和批量更新结果。
    """
    owner = _make_user(db, "issue-owner")
    project = _make_project(db, owner, "Issue Project")
    task = _make_task(db, owner, project, "Issue Task")
    primary = _make_issue(db, task, title="SQL 注入", severity="严重", status="unfixed")
    pending = _make_issue(db, task, title="待复审问题", severity="中", issue_type="代码质量", status="pending_review")
    fixed = _make_issue(db, task, title="已修复问题", severity="低", status="fixed")

    assert issue_service.get_issue(db, owner, primary.id).id == primary.id
    with pytest.raises(NotFoundError):
        issue_service.get_issue(db, owner, 999_002)

    default_list = issue_service.list_issues(db, owner)
    assert {item["id"] for item in default_list["items"]} == {primary.id, pending.id}

    all_items = issue_service.list_issues(db, owner, status="all")
    assert {item["id"] for item in all_items["items"]} == {primary.id, pending.id, fixed.id}

    filtered = issue_service.list_issues(
        db,
        owner,
        project_id=project.id,
        task_id=task.id,
        severity="严重",
        issue_type="安全漏洞",
        status="unfixed",
        keyword="SQL",
    )
    assert filtered["total"] == 1
    item = filtered["items"][0]
    assert item["project_name"] == "Issue Project"
    assert item["task_name"] == "Issue Task"
    assert item["owasp"] == "A03:2021-Injection"
    assert item["compliance_mapping"] == {"iso27001": ["A.8.28"]}
    assert item["static_rule_hits"] == 2

    issue_service.update_status(db, owner, primary.id, "fixed")
    db.refresh(primary)
    assert primary.status == "fixed"
    assert primary.handled_by == owner.id
    assert primary.handled_at is not None

    issue_service.batch_update_status(db, owner, [primary.id, pending.id], "ignored")
    db.refresh(primary)
    db.refresh(pending)
    assert primary.status == pending.status == "ignored"


def test_issue_service_rejects_missing_issue_or_deleted_task(db):
    """问题读取和更新应把缺失问题或已删除任务统一视为不存在。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言防枚举异常行为。
    """
    owner = _make_user(db, "deleted-task-owner")
    project = _make_project(db, owner, "Deleted Task Project")
    deleted_task = _make_task(db, owner, project, "Deleted Task", status="deleted")
    issue = _make_issue(db, deleted_task, title="不可见问题")

    with pytest.raises(NotFoundError):
        issue_service.get_issue(db, owner, issue.id)
    with pytest.raises(NotFoundError):
        issue_service.update_status(db, owner, issue.id, "fixed")
    with pytest.raises(NotFoundError):
        issue_service.update_status(db, owner, 999_003, "fixed")
