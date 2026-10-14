"""团队业务结果可完整读取，摘要有界且不越过账号和脱敏边界。"""
import json

import pytest

from app.models.agent_team import AgentTeamTask
from app.models.user import User
from app.services import agent_team_service
from app.services.agent_team_summary import summarize_dependencies
from tests.unit.services.test_site_agent_service import actor, claimed_team  # noqa: F401


def test_large_result_retains_owner_scoped_paged_original(db, actor):
    created, claim = claimed_team(db, actor)
    row = db.get(AgentTeamTask, claim["task_id"])
    original = {"data": {"items": [{"id": i, "title": f"条目{i}", "body": "完整内容" * 1000,
                                     "api_key": "sk-secret-original-value"} for i in range(35)]}}
    row.result_json = json.dumps(original)
    db.commit()
    detail = agent_team_service.get_team(db, actor, created["team_id"])
    result = detail["tasks"][0]["result"]
    assert len(json.dumps(result).encode()) < 17000
    assert result["result_reference"]["task_id"] == row.id
    first = agent_team_service.get_team_result_page(
        db, actor, created["team_id"], row.id, result_path="/data/items", offset=0, limit=20,
    )
    assert [item["id"] for item in first["items"]] == list(range(20))
    assert first["has_more"] and first["next_offset"] == 20
    leaf = agent_team_service.get_team_result_page(
        db, actor, created["team_id"], row.id, result_path="/data/items/0/body", offset=2000,
    )
    assert leaf["text"] == ("完整内容" * 1000)[2000:4000]
    assert "sk-secret-original-value" not in json.dumps(first)
    other = User(username="other-projection-owner", password="unused", role="admin", status=1)
    db.add(other)
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.get_team_result_page(db, other, created["team_id"], row.id)
    with pytest.raises(agent_team_service.AgentTeamValidationError):
        agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id, result_path="../../secret")
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id + 999)
    assert json.loads(row.result_json) == original


def test_reporter_retains_verified_business_items_and_scope():
    output = {"capability": "forum.posts.list", "status_code": 200,
              "data": {"items": [{"id": 44, "title": "版本公告"}], "total": 38}}
    receipt = {"output": output, "readback": output, "page_action": {
        "page": "/forum", "phase": "completed", "mutation": False,
    }}
    report = summarize_dependencies({"verify": {"status": "completed", "result": {
        "status": "completed", "result_receipt": receipt,
    }}})
    assert report["status"] == "completed"
    assert report["business_results"][0]["data"]["items"][0]["title"] == "版本公告"
    assert report["business_results"][0]["verification_scope"] == "same_query_readback"
    assert report["unique_finding_count"] == 0


def test_fixed_tool_contract_and_orchestrator_return_exact_business_leaf(db, actor):
    from app.agents.orchestrator import Orchestrator
    from app.agents.tool_contracts import FixedToolArgumentError, validate_fixed_tool_arguments
    from app.services.agent_responses_service import PrismToolExecutor
    from app.services.deepseek_responses_runtime import ToolCall

    created, claim = claimed_team(db, actor)
    row = db.get(AgentTeamTask, claim["task_id"])
    row.result_json = json.dumps({"data": {"items": [{"id": 44, "title": "真实业务标题"}]},
                                  "api_key": "sk-hidden-value"})
    db.commit()
    args = validate_fixed_tool_arguments("get_agent_team", {
        "team_id": created["team_id"], "task_id": row.id, "result_path": "/data/items", "limit": 3,
    })
    orch = Orchestrator.__new__(Orchestrator)
    orch._db, orch._user = db, actor
    output = PrismToolExecutor._agent_result(
        ToolCall("bounded-result", "get_agent_team", args, "{}"), orch.get_agent_team(**args),
    )
    assert output.status == "success"
    assert output.output["items"] == [{"id": 44, "title": "真实业务标题"}]
    assert orch.get_agent_team(created["team_id"], result_path="/data").success is False
    with pytest.raises(FixedToolArgumentError):
        validate_fixed_tool_arguments("get_agent_team", {"team_id": created["team_id"], "limit": 1000})


@pytest.mark.parametrize("path", ["/api_key", "/data/password", "/data/escaped~1key"])
def test_paged_path_preserves_key_based_redaction_and_pointer_escape(db, actor, path):
    created, claim = claimed_team(db, actor)
    row = db.get(AgentTeamTask, claim["task_id"])
    row.result_json = json.dumps({"api_key": "secret-value", "data": {
        "password": "secret-value", "escaped/key": "安全正文",
    }})
    db.commit()
    output = agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id, result_path=path)
    assert output["text"] == ("安全正文" if path.endswith("~1key") else "[REDACTED]")
