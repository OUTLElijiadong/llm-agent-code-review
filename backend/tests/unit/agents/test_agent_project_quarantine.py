"""SecuritySentinel 与 AiPromptAgent 的项目隔离回归测试。"""

from unittest.mock import Mock

import pytest

from app.agents.ai_prompt_agent import AiPromptAgent
from app.agents.base import AgentResult
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User


def _make_user(user_id: int, role: str = "user") -> User:
    return User(
        id=user_id,
        username=f"agent-quarantine-{user_id}",
        password="test-only",
        role=role,
        status=1,
    )


def _seed_graph(db, status: str = "active", id_base: int = 0) -> tuple[Project, ReviewTask, ReviewIssue, CodeFile]:
    owner = _make_user(1 + id_base)
    admin = _make_user(2 + id_base, "admin")
    member = _make_user(3 + id_base)
    stranger = _make_user(4 + id_base)
    project = Project(
        id=1 + id_base,
        user_id=owner.id,
        project_name="agent-quarantine-fixture",
        language="python",
        status=status,
    )
    project_member = ProjectMember(
        project_id=project.id,
        user_id=member.id,
        role_in_project="reviewer",
    )
    task = ReviewTask(
        id=1 + id_base,
        user_id=owner.id,
        project_id=project.id,
        task_name="fixture review",
        review_type="security",
        status="success",
        total_files=1,
        processed_files=1,
    )
    code_file = CodeFile(
        id=1 + id_base,
        project_id=project.id,
        file_name="secret.py",
        file_path="secret.py",
        language="python",
        content="password = 'synthetic-secret'\n",
        size_bytes=31,
        line_count=1,
        raw_size=31,
        status="active",
    )
    issue = ReviewIssue(
        id=1 + id_base,
        task_id=task.id,
        file_id=code_file.id,
        file_name=code_file.file_name,
        line_number=1,
        issue_type="安全漏洞",
        severity="高",
        title="硬编码凭据",
        description="测试用问题，不对应生产数据。",
        suggestion="使用安全配置注入。",
        status="unfixed",
    )
    db.add_all([owner, admin, member, stranger, project, project_member, task, code_file, issue])
    db.commit()
    return project, task, issue, code_file


@pytest.mark.parametrize("status", ["deleted", "quarantined"])
@pytest.mark.parametrize("user_id", [1, 2, 3, 4])
def test_security_sentinel_hidden_project_blocks_all_scopes_before_side_effects(
    db, monkeypatch, status: str, user_id: int,
):
    project, task, _, code_file = _seed_graph(db, status)
    user = db.get(User, user_id)
    agent = SecuritySentinelAgent()
    agent.inject(db, user=user)
    emitted = Mock()
    source_archive = Mock(return_value=False)
    llm = Mock(return_value=[])
    monkeypatch.setattr(agent, "_emit", emitted)
    monkeypatch.setattr(agent, "_llm_findings_for_file", llm)
    monkeypatch.setattr(
        "app.agents.security_sentinel_agent.project_source_service.begin_source_archive_audit",
        source_archive,
    )

    file_result = agent.scan_file(code_file.id, scan_depth="standard")
    task_result = agent.scan_task(task.id)
    project_result = agent.scan_project(project.id)

    assert not file_result.success
    assert not task_result.success
    assert not project_result.success
    assert emitted.call_count == 0
    assert llm.call_count == 0
    assert source_archive.call_count == 0


@pytest.mark.parametrize("status", ["deleted", "quarantined"])
@pytest.mark.parametrize("user_id", [1, 2, 3, 4])
def test_ai_prompt_hidden_project_blocks_issue_task_project_before_model(
    db, monkeypatch, status: str, user_id: int,
):
    project, task, issue, _ = _seed_graph(db, status)
    agent = AiPromptAgent()
    agent.inject(db, user=db.get(User, user_id))
    model_call = Mock(
        return_value=AgentResult(success=True, data="synthetic polished prompt", tokens={}),
    )
    build_prompt = Mock(wraps=agent._build_for_issue)
    monkeypatch.setattr(agent, "call", model_call)
    monkeypatch.setattr(agent, "_build_for_issue", build_prompt)

    issue_result = agent.execute_for_issue(issue.id, use_llm=True)
    task_result = agent.execute_for_task(task.id, use_llm=True)
    project_result = agent.execute_for_project(project.id, use_llm=True)

    assert not issue_result.success
    assert not task_result.success
    assert not project_result.success
    assert model_call.call_count == 0
    assert build_prompt.call_count == 0


def test_active_and_archived_member_access_remains_allowed(db):
    for index, status in enumerate(("active", "archived"), 1):
        id_base = index * 10
        project, task, issue, code_file = _seed_graph(db, status, id_base)
        member = db.get(User, 3 + id_base)

        sentinel = SecuritySentinelAgent()
        sentinel.inject(db, user=member)
        assert sentinel._authz_project(project) is None
        assert sentinel._authz_task(task) is None
        assert sentinel._authz_file(code_file) is None

        prompt = AiPromptAgent()
        prompt.inject(db, user=member)
        assert prompt._authz_issue(issue) is None


def test_hidden_project_is_blocked_for_internal_agent_context(db):
    project, task, issue, code_file = _seed_graph(db, "quarantined")

    sentinel = SecuritySentinelAgent()
    sentinel.inject(db, user=None)
    assert sentinel._authz_project(project) is not None
    assert sentinel._authz_task(task) is not None
    assert sentinel._authz_file(code_file) is not None

    prompt = AiPromptAgent()
    prompt.inject(db, user=None)
    assert prompt._authz_issue(issue) is not None
