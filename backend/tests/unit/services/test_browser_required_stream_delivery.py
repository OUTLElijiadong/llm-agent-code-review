"""浏览器桥真实 SSE 出站回归：签名只在可信边界校验，不在公开队列二次丢失。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.v1 import agent_responses as api
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.services import agent_browser_service as browser
from app.services import agent_responses_service as service
from app.services.deepseek_responses_runtime import RuntimeResult, ToolCall


async def collect_events(response):
    body = ""
    async for chunk in response.body_iterator:
        body += chunk.decode() if isinstance(chunk, bytes) else chunk
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["user", "admin"])
@pytest.mark.parametrize("action,args", [("inspect", {}), ("navigate", {"route": "/forum"})])
async def test_signed_browser_event_survives_complete_api_queue_and_encoding(monkeypatch, surface, action, args):
    """必须经过端点 sink→queue→encode，单独 public_required_event 测试不足以覆盖。"""
    run_id = "run_delivery_browser_required"
    row = SimpleNamespace(
        id=17, run_id=run_id, session_key="browser_delivery_session", surface=surface,
        user_id=7, action=action, arguments_json=json.dumps(args),
        observation_snapshot_id="a" * 32,
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).replace(tzinfo=None),
        status="queued",
    )
    required = browser._signed_required(row, "browser-delivery-nonce")

    class FakeService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self, _messages, *, run_id, event_sink):
            await event_sink({"type": "response.created", "response": {"id": "provider-response-id"}})
            await event_sink(required)
            # 未签名事件及篡改已有签名都不得因此得到执行权限。
            await event_sink({key: value for key, value in required.items() if key != "_browser_signature"})
            await event_sink({**required, "args": {"route": "https://untrusted.invalid"}})
            return RuntimeResult(
                run_id=run_id, status="completed", output_text="已返回页面观察", response={"output": []}, rounds=1,
            )

    monkeypatch.setattr(api, "AgentResponsesService", FakeService)
    monkeypatch.setattr(api, "_is_admin_actor", lambda *_args: True)
    response = await api.stream_agent_response(
        api.AgentResponsesRequest(
            surface=surface, session_id=row.session_key, run_id=run_id,
            messages=[{"role": "user", "content": "只读取当前页面"}],
        ),
        db=object(), user=SimpleNamespace(id=7, role="admin" if surface == "admin" else "user"),
    )
    events = await collect_events(response)
    delivered = [event for event in events if event["type"] == "response.browser.required"]
    assert len(delivered) == 1, events
    assert delivered[0]["run_id"] == run_id
    assert delivered[0]["action"] == action and delivered[0]["args"] == args
    assert delivered[0]["client_nonce"] == required["client_nonce"]
    assert "_browser_signature" not in delivered[0]
    assert not any(event["type"] == "error" for event in events)
    created = [event for event in events if event["type"] == "response.created"]
    assert len(created) == 1 and created[0]["response"]["id"] == run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["user", "admin"])
@pytest.mark.parametrize("action_status,is_completed", [
    ("failed", False), ("cancelled", False), ("indeterminate", False), ("user_required", False),
    ("completed", False), ("completed", True),
])
async def test_browser_action_outcome_drives_tool_lifecycle_without_losing_observation(
    db, monkeypatch, surface, action_status, is_completed,
):
    """正常返回一个失败回执不等于页面工具成功，恢复时仍须保留原观察。"""
    events = []
    monkeypatch.setattr(service, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(service, "_is_admin_actor", lambda *_args: surface == "admin")
    monkeypatch.setattr(service, "_is_super_admin_actor", lambda *_args: False)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_args: True)
    actor = service.PrismToolExecutor(
        db, SimpleNamespace(id=7, username="browser-member", role=surface, status=1),
        surface=surface, run_id="run_browser_outcome", session_key="browser_outcome_session",
        mcp_provider=SimpleNamespace(), event_sink=events.append,
    )
    error = "请本人在页面安全输入或选择文件" if action_status == "user_required" else "浏览器未确认完成观察"
    observed = {
        "action_id": 17, "status": action_status, "completed": is_completed,
        "verification_status": "client_observed", "server_verified": False,
        "observation": {"route": "/dashboard", "controls": []},
        "error": "" if is_completed else error,
        "retryable": False,
    }

    async def request(*_args, **_kwargs):
        return observed

    async def record(*_args, **_kwargs):
        pass

    async def once(_call, operation):
        return await operation()

    monkeypatch.setattr(browser, "request_action", request)
    monkeypatch.setattr(actor, "_record_supervisor_review", record)
    monkeypatch.setattr(actor, "_execute_once", once)
    call = ToolCall("browser-outcome", "operate_current_page", {"action": "inspect", "args": {}}, "{}")
    result = await actor.execute(call)
    assert result.output == observed
    assert result.status == ("success" if is_completed else "error")
    assert result.output["server_verified"] is False
    await actor._emit_tool_result(call, result)
    assert events[-1]["type"] == (
        "response.tool.completed" if is_completed else "response.tool.failed"
    )
    if not is_completed:
        assert error in result.error
    if action_status == "user_required":
        assert result.failure_kind == "user_required"


@pytest.mark.parametrize("action_status,is_completed", [
    ("failed", False), ("cancelled", False), ("indeterminate", False), ("user_required", False),
    ("completed", False), ("completed", True),
])
@pytest.mark.parametrize("has_ledger", [True, False])
def test_legacy_browser_output_failure_recovers_truthfully_without_rewriting_ledger(
    db, action_status, is_completed, has_ledger,
):
    """旧版曾把失败观察包成 success；重载不能将该条目改显示成完成。"""
    run_id = "run_legacy_browser_outcome"
    error = "请本人在页面安全输入或选择文件" if action_status == "user_required" else "浏览器未确认完成观察"
    action_output = {
        "status": action_status, "completed": is_completed, "server_verified": False,
        "error": "" if is_completed else error,
    }
    legacy_result = {"status": "success", "output": action_output}
    checkpoint = {
        "transcript": [
            {"role": "user", "content": "读取当前页面"},
            {"type": "function_call", "call_id": "legacy-browser", "name": "operate_current_page",
             "arguments": json.dumps({"action": "inspect", "args": {}})},
            {"type": "function_call_output", "call_id": "legacy-browser", "output": json.dumps(legacy_result)},
        ],
    }
    run = AgentResponseRun(
        run_id=run_id, user_id=7, surface="user", session_key="legacy_browser_session",
        status="completed", checkpoint_json=json.dumps(checkpoint),
    )
    db.add(run)
    db.flush()
    row = None
    if has_ledger:
        row = AgentToolExecution(
            request_id="legacy-browser-receipt", run_id=run_id, call_id="legacy-browser", user_id=7,
            tool_name="operate_current_page", status="success",
            arguments_json=json.dumps({"action": "inspect", "args": {}}), result_json=json.dumps(legacy_result),
        )
        db.add(row)
    db.commit()
    events = api._public_completed_tool_events(db, run, checkpoint)
    expected = "response.tool.completed" if is_completed else "response.tool.failed"
    assert [event["type"] for event in events] == ["response.tool.started", expected]
    if not is_completed:
        assert error in events[-1]["error"]
    if row is not None:
        db.refresh(row)
        assert row.status == "success" and json.loads(row.result_json) == legacy_result
