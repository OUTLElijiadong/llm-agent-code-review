import json

import pytest

from app.core.exceptions import ForbiddenError
from app.services.deepseek_responses_runtime import RunCheckpoint
from app.services.enterprise_assistant_runtime_service import _assert_checkpoint_team_results_safe


def _checkpoint(tool_name, output, *, call_id="call-1", include_call=True):
    items = []
    if include_call:
        items.append({"type": "function_call", "name": tool_name, "call_id": call_id, "arguments": "{}"})
    items.append({"type": "function_call_output", "call_id": call_id, "output": json.dumps(output)})
    return RunCheckpoint(run_id="run", model="model", transcript=items, tools=[])


def test_legacy_team_result_cannot_be_replayed_or_published():
    checkpoint = _checkpoint("get_enterprise_agent_team", {
        "status": "success", "output": {"team_id": 4, "objective": "受限内容", "tasks": []},
    })
    with pytest.raises(ForbiddenError):
        _assert_checkpoint_team_results_safe(checkpoint)


def test_safe_team_reference_and_unrelated_tool_remain_usable():
    _assert_checkpoint_team_results_safe(_checkpoint("get_enterprise_agent_team", {
        "status": "success", "output": {"location": "请在当前会话点击「团队进度」查看"},
    }))
    _assert_checkpoint_team_results_safe(_checkpoint("search_enterprise_knowledge", {
        "status": "success", "output": {"content": "企业知识"},
    }))


def test_unknown_tool_output_source_cannot_be_assumed_safe():
    checkpoint = _checkpoint("get_enterprise_agent_team", {"status": "success", "output": "受限内容"},
                             include_call=False)
    with pytest.raises(ForbiddenError):
        _assert_checkpoint_team_results_safe(checkpoint)


def test_duplicate_call_id_cannot_hide_legacy_team_result():
    checkpoint = _checkpoint("get_enterprise_agent_team", {
        "status": "success", "output": {"objective": "私人团队内容"},
    })
    checkpoint.last_response = {"output": [{
        "type": "function_call", "name": "search_enterprise_knowledge", "call_id": "call-1", "arguments": "{}",
    }]}
    with pytest.raises(ForbiddenError):
        _assert_checkpoint_team_results_safe(checkpoint)


def test_unexecuted_team_call_has_no_old_result_to_replay():
    checkpoint = RunCheckpoint(run_id="run", model="model", tools=[], transcript=[{
        "type": "function_call", "name": "get_enterprise_agent_team", "call_id": "x", "arguments": "{}",
    }])
    _assert_checkpoint_team_results_safe(checkpoint)
