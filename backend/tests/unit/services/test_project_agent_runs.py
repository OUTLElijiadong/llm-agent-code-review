"""项目级 Agent 运转次数:project_id 提取、项目统计与普通成员 Agent 工作台过滤回归。"""

from __future__ import annotations

from app.models.agent_governance import ToolCallLog
from app.models.user import User
from app.services import agent_service, project_service
from app.services.tool_gateway import _extract_project_id


def test_extract_project_id_from_resource() -> None:
    assert _extract_project_id("project:12", None, "") == 12
    assert _extract_project_id("projects/34", None, "") == 34
    assert _extract_project_id("project#56", None, "") == 56


def test_extract_project_id_from_context() -> None:
    assert _extract_project_id("", {"project_id": 78}, "") == 78
    assert _extract_project_id("", {"arguments": {"project_id": 90}}, "") == 90


def test_extract_project_id_from_input_summary_json() -> None:
    assert _extract_project_id("", None, '{"project_id": 101}') == 101
    assert _extract_project_id("page:/projects", None, "无项目") is None


def test_project_list_returns_agent_run_stats(db, monkeypatch) -> None:
    from app.models.project import Project

    owner = User(username="runowner", password="x", role="user", status=1)
    db.add(owner)
    db.commit()
    project = Project(project_name="运转统计项目", language="php", status="active", user_id=owner.id)
    db.add(project)
    db.commit()
    db.refresh(project)
    # 手工插入两条 tool_call_log(绕过网关)
    for i in range(2):
        db.add(ToolCallLog(
            agent_code="chat_assistant", tool_code="start_review", action="reviews.start",
            resource=f"project:{project.id}", status="success", risk_level="low",
            decision="allow", input_summary="", output_summary="", project_id=project.id,
        ))
    db.commit()

    result = project_service.list_projects(db, owner, page=1, page_size=20)
    target = next(item for item in result["items"] if item["id"] == project.id)
    assert target["agent_run_count"] == 2
    assert target["last_agent_run_at"] is not None


def _fake_catalog(_db=None) -> list[dict]:
    return [
        {"code": "chat_assistant", "source": "builtin", "category": "general"},
        {"code": "test_verifier", "source": "builtin", "category": "sandbox"},
        {"code": "manager", "source": "builtin", "category": "admin"},
        {"code": "operations", "source": "builtin", "category": "admin"},
        {"code": "evolution", "source": "builtin", "category": "admin"},
        {"code": "orchestrator", "source": "builtin", "category": "internal"},
        {"code": "custom_reviewer", "source": "custom", "category": "custom_review"},
    ]


def _seed_member_call(db, user_id: int) -> None:
    """给普通成员插入一条 chat_assistant 调用记录(带 agent_label)。"""
    from app.models.ai_call_log import AiCallLog

    db.add(AiCallLog(
        user_id=user_id,
        agent_label="chat_assistant",
        model_name="deepseek-v4-flash",
        status="success",
    ))
    db.commit()


def test_ordinary_member_only_sees_agents_they_ran(db, monkeypatch) -> None:
    member = User(username="member_run", password="x", role="user", status=1)
    db.add(member)
    db.commit()
    _seed_member_call(db, member.id)
    monkeypatch.setattr(agent_service, "get_runtime_catalog", _fake_catalog)
    rows = agent_service.get_runtime_agents(db, member.id)
    codes = {item["code"] for item in rows}
    # 只显示自己运行过的 agent
    assert codes == {"chat_assistant"}
    # 没运行过的 test_verifier/manager 等不占工位
    assert "test_verifier" not in codes
    assert "manager" not in codes
    # 调用次数回填
    row = next(item for item in rows if item["code"] == "chat_assistant")
    assert row["call_count"] == 1


def test_ordinary_member_with_no_runs_sees_empty_workbench(db, monkeypatch) -> None:
    member = User(username="member_none", password="x", role="user", status=1)
    db.add(member)
    db.commit()
    monkeypatch.setattr(agent_service, "get_runtime_catalog", _fake_catalog)
    rows = agent_service.get_runtime_agents(db, member.id)
    assert rows == []


def test_admin_runtime_keeps_all_agents(db, monkeypatch) -> None:
    monkeypatch.setattr(agent_service, "get_runtime_catalog", _fake_catalog)
    rows = agent_service.get_runtime_agents(db, None)
    codes = {item["code"] for item in rows}
    assert "manager" in codes
    assert "operations" in codes
    assert "orchestrator" in codes
    assert "chat_assistant" in codes
    assert "custom_reviewer" in codes


def test_member_call_count_attribute_to_agent_label(db, monkeypatch) -> None:
    """带 agent_label 的小菱调用精确计入 chat_assistant,不因 model_name 无后缀误归因。"""
    member = User(username="member_label", password="x", role="user", status=1)
    db.add(member)
    db.commit()
    from app.models.ai_call_log import AiCallLog

    db.add(AiCallLog(user_id=member.id, agent_label="chat_assistant", model_name="deepseek-v4-flash", status="success"))
    db.add(AiCallLog(user_id=member.id, agent_label="test_verifier", model_name="deepseek-v4-flash", status="success"))
    db.commit()
    monkeypatch.setattr(agent_service, "get_runtime_catalog", _fake_catalog)
    rows = agent_service.get_runtime_agents(db, member.id)
    by_code = {item["code"]: item for item in rows}
    assert by_code["chat_assistant"]["call_count"] == 1
    assert by_code["test_verifier"]["call_count"] == 1
    # 无后缀 model_name 不应把调用误计入其他 agent
    assert "code_reviewer" not in by_code

