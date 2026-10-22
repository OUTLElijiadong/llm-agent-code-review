"""沿真实工具引用完整读取团队摘要与错误，兼容任务结果且保持权限隔离。"""
# ruff: noqa: F811 -- 复用已有账号fixture。
import json

import pytest

from app.agents.orchestrator import Orchestrator
from app.agents.tool_contracts import FixedToolArgumentError, validate_fixed_tool_arguments
from app.models.agent_mesh import AgentMeshMessage
from app.models.agent_team import AgentTeam, AgentTeamTask
from app.models.user import User
from app.services import agent_team_service
from tests.unit.services.test_site_agent_service import actor, claimed_team  # noqa: F401


def _tool_read(db, user, reference, **overrides):
    arguments = {key: value for key, value in reference.items() if key != "tool"}
    arguments.update(overrides)
    arguments = validate_fixed_tool_arguments(reference["tool"], arguments)
    orch = Orchestrator.__new__(Orchestrator)
    orch._db, orch._user = db, user
    response = orch.get_agent_team(**arguments)
    assert response.success, response.error
    return response.data


def _read_text(db, user, reference):
    chunks = []
    offset = 0
    for _ in range(100):
        page = _tool_read(db, user, reference, offset=offset)
        assert "text" in page, "结果引用必须进入原值分页，不能再次返回团队详情投影"
        chunks.append(page["text"])
        if not page["has_more"]:
            assert page["next_offset"] is None
            assert sum(map(len, chunks)) == page["total"]
            return "".join(chunks)
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    pytest.fail("分页引用未在有限步数内结束")


@pytest.mark.parametrize("source", ["detail", "notification"])
def test_team_summary_reference_reassembles_full_business_tail(db, actor, source):
    created, claim = claimed_team(db, actor)
    team = db.get(AgentTeam, created["team_id"])
    task = db.get(AgentTeamTask, claim["task_id"])
    text = "团队业务摘要/第一行\n逐项核验中文🙂证据。" * 1300 + "【仅原团队摘要末尾可见】"
    original = {"summary": text, "completed_tasks": 1, "api_key": "never-public-team-key"}
    team.status, team.summary_json = "failed", json.dumps(original, ensure_ascii=False)
    task.result_json = json.dumps({"summary": "最后成员的不同结果"})
    db.commit()
    if source == "detail":
        projected = agent_team_service.get_team(db, actor, team.id)["summary"]
    else:
        agent_team_service._persist_terminal_team_notification(db, team, now=agent_team_service._now())
        db.commit()
        notice = db.query(AgentMeshMessage).filter_by(message_id=f"team-{team.id}-terminal-0-result").one()
        projected = json.loads(notice.payload_json)["summary"]
    assert projected["summary_truncated"] is True
    reference = projected["result_reference"]
    assert "task_id" not in reference
    root = _tool_read(db, actor, reference)
    assert "items" in root, "根引用不能自引用到再次截断的get_team详情"
    assert root["items"]["api_key"] == "[REDACTED]"
    nested = root["items"]["summary"]["result_reference"]
    assert nested["result_scope"] == "team_summary" and "task_id" not in nested
    assert _read_text(db, actor, nested) == text
    assert json.loads(team.summary_json) == original
    assert json.loads(task.result_json) == {"summary": "最后成员的不同结果"}


def test_team_error_reference_reads_long_keys_values_and_redacts_before_paging(db, actor):
    created, _claim = claimed_team(db, actor)
    team = db.get(AgentTeam, created["team_id"])
    key = "长错误字段/~" * 180
    error = "真实业务失败；" * 1500 + "【失败尾部】"
    original = {key: {"message": error, "password": "never-public-team-password"}}
    team.status, team.error_json = "failed", json.dumps(original, ensure_ascii=False)
    db.commit()
    agent_team_service._persist_terminal_team_notification(db, team, now=agent_team_service._now())
    db.commit()
    notice = db.query(AgentMeshMessage).filter_by(message_id=f"team-{team.id}-terminal-0-result").one()
    projected = json.loads(notice.payload_json)["error"]
    root = _tool_read(db, actor, projected["result_reference"])
    assert "entries" in root, "错误引用必须进入完整错误分页"
    entry = root["entries"][0]
    assert entry["key_reference"]["result_scope"] == "team_error"
    assert _read_text(db, actor, entry["key_reference"]) == key
    value = _tool_read(db, actor, entry["value"]["result_reference"])
    assert value["items"]["password"] == "[REDACTED]"
    assert _read_text(db, actor, value["items"]["message"]["result_reference"]) == error
    assert json.loads(team.error_json) == original


@pytest.mark.parametrize("scope", ["team_summary", "team_error"])
def test_team_scope_rejects_foreign_owner_and_wrong_surface(db, actor, scope):
    created, _claim = claimed_team(db, actor)
    team = db.get(AgentTeam, created["team_id"])
    other = User(username=f"foreign-{scope}", password="unused", role="admin", status=1)
    db.add(other)
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.get_team_result_page(db, other, team.id, result_scope=scope)
    team.surface = "admin"
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamAccessError):
        agent_team_service.get_team_result_page(db, actor, team.id, result_scope=scope)


def test_task_default_and_explicit_scope_remain_compatible(db, actor):
    created, claim = claimed_team(db, actor)
    task = db.get(AgentTeamTask, claim["task_id"])
    task.result_json = json.dumps({"body": "任务结果" * 1500 + "【任务尾部】"})
    db.commit()
    old_reference = {"tool": "get_agent_team", "team_id": created["team_id"],
                     "task_id": task.id, "result_path": "/body"}
    assert _read_text(db, actor, old_reference) == json.loads(task.result_json)["body"]
    assert _tool_read(db, actor, old_reference) == _tool_read(db, actor, old_reference, result_scope="task")
    assert "tasks" in _tool_read(db, actor, {"tool": "get_agent_team", "team_id": created["team_id"]})


@pytest.mark.parametrize("kwargs", [
    {"result_scope": "unknown"}, {"result_scope": "team_summary", "task_id": 1},
    {"result_scope": "team_error", "task_id": 1}, {"result_scope": "task"},
    {"result_scope": "team_summary", "result_path": "/missing"},
    {"result_scope": "team_error", "result_indices": [-1]},
])
def test_scope_service_rejects_invalid_or_ambiguous_locators(db, actor, kwargs):
    created, _claim = claimed_team(db, actor)
    with pytest.raises(agent_team_service.AgentTeamValidationError):
        agent_team_service.get_team_result_page(db, actor, created["team_id"], **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"result_scope": "unknown"}, {"result_scope": "team_summary", "task_id": 1},
    {"result_scope": "team_error", "task_id": 1},
])
def test_scope_tool_contract_rejects_invalid_or_ambiguous_source(kwargs):
    with pytest.raises(FixedToolArgumentError):
        validate_fixed_tool_arguments("get_agent_team", {"team_id": 1, **kwargs})
