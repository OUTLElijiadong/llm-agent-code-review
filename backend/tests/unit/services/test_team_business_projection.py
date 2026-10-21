"""团队业务结果可完整读取，摘要有界且不越过账号和脱敏边界。"""
# ruff: noqa: F811 -- 跨文件共享pytest fixture名与参数名保持一致。
import json
from copy import deepcopy

import pytest

from app.models.agent_team import AgentTeamTask
from app.models.user import User
from app.services import agent_team_service
from app.services.agent_team_projection import project_team_result
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
    receipt = {"request_params": {}, "output": output,
               "readback": {**deepcopy(output), "request_params": {}}, "page_action": {
                   "page": "/forum", "phase": "completed", "mutation": False,
               }}
    report = summarize_dependencies({"verify": {"status": "completed", "result": {
        "status": "completed", "result_receipt": receipt,
    }}})
    assert report["status"] == "completed"
    assert report["business_results"][0]["data"]["items"][0]["title"] == "版本公告"
    assert report["business_results"][0]["verification_scope"] == "same_query_readback"
    business_check = next(check for check in report["claim_checks"] if check["kind"] == "business_result")
    assert business_check["status"] == "supported"
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


@pytest.mark.parametrize("value", [
    {"f" * 20000: "safe"},
    {"summary": "长" * 200, "title": "题" * 200, "body": "x" * 5000},
    {str(i) + "中" * 100: {"body": "末" * 1000} for i in range(20)},
])
def test_structure_descriptor_itself_obeys_utf8_budget(value):
    output = project_team_result(value, team_id=123, task_id=456, budget=1024)
    assert len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    assert output["result_reference"]["task_id"] == 456


def test_long_dictionary_keys_are_fully_retrievable_without_oversized_pointer(db, actor):
    created, claim = claimed_team(db, actor)
    row = db.get(AgentTeamTask, claim["task_id"])
    key = "中文长字段/" * 3000
    body = "完整正文" * 1000
    original = {"data": {key: {"body": body, "password": "never-public"}}}
    row.result_json = json.dumps(original)
    db.commit()
    page = agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id,
                                                   result_path="/data")
    assert len(json.dumps(page, ensure_ascii=False).encode()) < 4000
    entry = page["entries"][0]
    value_ref = entry["value"]["result_reference"]
    from app.agents.orchestrator import Orchestrator
    from app.agents.tool_contracts import validate_fixed_tool_arguments
    orch = Orchestrator.__new__(Orchestrator)
    orch._db, orch._user = db, actor
    assert orch.get_agent_team(**validate_fixed_tool_arguments("get_agent_team", {
        key: value for key, value in value_ref.items() if key != "tool"
    })).success
    recovered = ""
    key_ref = entry["key_reference"]
    while True:
        result = agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id,
            result_indices=key_ref["result_indices"], result_part="key", offset=len(recovered))
        recovered += result["text"]
        if not result["has_more"]:
            break
    assert recovered == key
    leaf = agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id,
        result_indices=value_ref["result_indices"] + [0], offset=2000)
    assert leaf["text"] == body[2000:4000]
    hidden = agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id,
        result_indices=value_ref["result_indices"] + [1])
    assert hidden["text"] == "[REDACTED]"
    assert json.loads(row.result_json) == original


@pytest.mark.parametrize("kwargs", [
    {"result_indices": [-1]}, {"result_indices": [True]}, {"result_indices": [999]},
    {"result_indices": [0] * 33}, {"result_indices": [0], "result_path": "/data"},
    {"result_part": "key"}, {"result_part": "unknown"},
])
def test_index_result_locator_rejects_invalid_input(db, actor, kwargs):
    created, claim = claimed_team(db, actor)
    with pytest.raises(agent_team_service.AgentTeamValidationError):
        agent_team_service.get_team_result_page(db, actor, created["team_id"], claim["task_id"], **kwargs)


def test_key_reference_traverses_intermediate_arrays_and_rejects_foreign_owner(db, actor):
    created, claim = claimed_team(db, actor)
    row = db.get(AgentTeamTask, claim["task_id"])
    row.result_json = json.dumps({"data": [{"字段/名": "值"}]})
    db.commit()
    output = agent_team_service.get_team_result_page(db, actor, created["team_id"], row.id,
        result_indices=[0, 0, 0], result_part="key")
    assert output["text"] == "字段/名"
    other = User(username="other-locator-owner", password="unused", role="admin", status=1)
    db.add(other)
    db.commit()
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.get_team_result_page(db, other, created["team_id"], row.id,
            result_indices=[0, 0, 0], result_part="key")
