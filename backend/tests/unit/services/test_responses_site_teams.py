"""小菱页面任务自动组队与真实页面执行事件回归。"""

import inspect
from types import SimpleNamespace

import pytest

from app.services import agent_responses_service as service
from app.services import agent_team_service, policy_engine
from app.services.deepseek_responses_runtime import ToolCall, ToolExecutionResult


def executor(db, monkeypatch, events):
    monkeypatch.setattr(service, "get_request_orchestrator", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(service, "_is_admin_actor", lambda *_a: False)
    monkeypatch.setattr(service, "_is_super_admin_actor", lambda *_a: False)
    monkeypatch.setattr(service.rbac_service, "check_permission", lambda *_a: True)
    return service.PrismToolExecutor(
        db,
        SimpleNamespace(id=11, username="member", role="user", status=1),
        surface="user",
        run_id="site-run",
        session_key="site_session_123",
        mcp_provider=SimpleNamespace(),
        event_sink=events.append,
    )


@pytest.mark.asyncio
async def test_registered_capability_event_has_server_bound_target(db, monkeypatch):
    events = []
    actor = executor(db, monkeypatch, events)
    call = ToolCall(
        "forum",
        "user_execute_capability",
        {
            "capability": "forum.posts.create",
            "params": {"title": "更新说明", "content": "正文"},
        },
        "{}",
    )
    # 固定编码必须使用实际注册项，不允许向模型参数伪填 page。
    from app.services.user_capability_registry import USER_CAPABILITIES

    spec = next(item for item in USER_CAPABILITIES if item.method == "POST" and item.path == "/api/forum/posts")
    call.arguments["capability"] = spec.code
    await actor._emit_tool_event("response.tool.started", call)
    metadata = events[-1]["page_action"]
    assert metadata["page"] == "/forum"
    assert metadata["capability"] == spec.code
    assert metadata["mutation"] is True
    assert metadata["account_id"] == 11
    assert "page" not in events[-1]["arguments"]


@pytest.mark.asyncio
async def test_unknown_capability_does_not_invent_page_target(db, monkeypatch):
    events = []
    actor = executor(db, monkeypatch, events)
    await actor._emit_tool_event(
        "response.tool.started",
        ToolCall(
            "unknown",
            "user_execute_capability",
            {"capability": "invented.create", "params": {}},
            "{}",
        ),
    )
    assert "page_action" not in events[-1]


@pytest.mark.asyncio
async def test_page_task_submits_real_execution_and_verification_team(db, monkeypatch):
    actor = executor(db, monkeypatch, [])
    from app.services.user_capability_registry import USER_CAPABILITIES

    spec = next(item for item in USER_CAPABILITIES if item.method == "POST" and item.path == "/api/forum/posts")
    monkeypatch.setattr(service.admin_capability_service, "prepare_request", lambda *_a: ("/api/forum/posts", {}, {}))
    monkeypatch.setattr(
        service.tool_gateway,
        "authorize",
        lambda *_a, **_k: SimpleNamespace(
            decision=policy_engine.ALLOW,
            risk_level="medium",
            reason="允许",
        ),
    )

    async def run_once(_call, operation):
        result = operation()
        return await result if inspect.isawaitable(result) else result

    async def forbid_direct(*_a, **_k):
        pytest.fail("页面任务仍由主控直接执行，未创建子 Agent 团队")

    seen = []

    def submit(_db, user, payload, **kwargs):
        seen.append((user.id, payload, kwargs))
        return {"team_id": 52, "status": "queued"}

    monkeypatch.setattr(actor, "_execute_once", run_once)
    monkeypatch.setattr(service.admin_capability_service, "execute_api", forbid_direct)
    monkeypatch.setattr(agent_team_service, "create_team_from_xiaoling", submit)
    result = await actor._execute_user_capability(
        ToolCall(
            "create",
            "user_execute_capability",
            {
                "capability": spec.code,
                "params": {"title": "更新说明", "content": "正文"},
            },
            "{}",
        ),
        approved=False,
    )
    assert result.status == "success"
    assert result.output["team_id"] == 52
    assert result.output["completed"] is False
    assert result.output["verification_status"] == "pending"
    assert seen[0][0] == 11
    payload = seen[0][1]
    assert payload.session_id == "site_session_123"
    assert payload.tasks[0].input["capability"] == spec.code
    assert payload.tasks[0].max_attempts == 1
    assert payload.tasks[1].input["operation"] == "verify_results"
    assert payload.tasks[1].depends_on == [payload.tasks[0].task_key]


@pytest.mark.asyncio
async def test_submission_event_never_reports_business_completed(db, monkeypatch):
    events = []
    actor = executor(db, monkeypatch, events)
    from app.services.user_capability_registry import USER_CAPABILITIES

    spec = next(item for item in USER_CAPABILITIES if item.method == "POST" and item.path == "/api/forum/posts")
    await actor._emit_tool_result(
        ToolCall(
            "create",
            "user_execute_capability",
            {
                "capability": spec.code,
                "params": {},
            },
            "{}",
        ),
        ToolExecutionResult.success(
            {
                "team_id": 52,
                "status": "queued",
                "completed": False,
                "verification_status": "pending",
            }
        ),
    )
    assert events[-1]["page_action"]["phase"] == "submitted"
    assert events[-1]["team_id"] == 52


def test_instructions_delegate_page_tasks_and_respect_actual_risk():
    text = service._instructions("user", SimpleNamespace(username="member", role="user"))
    assert "页面执行子 Agent" in text
    assert "低、中风险" in text
    assert "排队不等于完成" in text


def test_pending_submission_is_not_business_success_evidence(db):
    import json

    from app.models.agent_response_run import AgentToolExecution
    from app.services.deepseek_responses_runtime import RunCheckpoint

    arguments = {
        "capability": "report_templates.create",
        "params": {"name": "规范", "content": "内容", "type": "custom"},
    }
    checkpoint = RunCheckpoint(
        run_id="pending-site",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请创建报告模板"},
            {
                "type": "function_call",
                "call_id": "create",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments),
            },
        ],
    )
    db.add(
        AgentToolExecution(
            request_id=service._request_id(checkpoint.run_id, "create"),
            run_id=checkpoint.run_id,
            call_id="create",
            user_id=11,
            tool_name="admin_execute_capability",
            status="success",
            arguments_json=json.dumps(arguments),
            result_json=json.dumps(
                {
                    "status": "success",
                    "output": {"team_id": 52, "completed": False, "verification_status": "pending"},
                }
            ),
        )
    )
    db.commit()
    evidence = service._ledger_admin_write_evidence(db, user_id=11, checkpoint=checkpoint)
    assert evidence["create"] == ("report_templates.create", "pending")
    assert service._admin_completion_guard(checkpoint, "报告模板已创建完成", write_evidence=evidence)
    assert (
        service._admin_completion_guard(checkpoint, "页面任务已提交，等待执行与核验", write_evidence=evidence) is None
    )


@pytest.mark.parametrize(
    "pending_text",
    [
        "已提交团队，业务尚未完成。",
        "已提交团队，业务还未完成。",
        "页面任务已提交，等待执行和核验，尚未完成。",
        "团队还在执行，业务尚未完成。",
        "不能声称已完成，任务仍在排队。",
        "不要报告已发布，当前只有客户端按钮点击回执。",
    ],
)
def test_honest_pending_completion_language_is_not_misclassified_as_success(db, pending_text):
    """否定前缀中的“完成”不能让待办说明触发假成功拦截和模型重试。"""
    import json

    from app.services.deepseek_responses_runtime import RunCheckpoint

    arguments = {
        "capability": "report_templates.create",
        "params": {"name": "规范", "content": "内容", "type": "custom"},
    }
    checkpoint = RunCheckpoint(
        run_id="honest-pending-language",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请创建报告模板"},
            {
                "type": "function_call",
                "call_id": "create",
                "name": "admin_execute_capability",
                "arguments": json.dumps(arguments),
            },
            {
                "type": "function_call_output",
                "call_id": "create",
                "output": json.dumps({
                    "status": "success",
                    "output": {"team_id": 52, "completed": False, "verification_status": "pending"},
                }),
            },
        ],
    )
    pending_evidence = {"create": ("report_templates.create", "pending")}

    assert service._claims_mutation_success(pending_text) is False
    assert service._pending_site_submission_guard(checkpoint, pending_text) is None
    assert service._admin_completion_guard(
        checkpoint, pending_text, write_evidence=pending_evidence,
    ) is None


@pytest.mark.parametrize(
    "success_text",
    [
        "帖子已发布成功。",
        "报告模板已创建。",
        "操作已经完成。",
    ],
)
def test_real_mutation_success_language_is_still_detected(success_text):
    """成功识别器保留真实的业务变更成功表述。"""
    assert service._claims_mutation_success(success_text) is True


@pytest.mark.parametrize("tool", ["user_execute_capability", "admin_execute_capability"])
def test_pending_site_guard_rejects_false_completion_on_both_surfaces(db, tool):
    import json

    from app.services.deepseek_responses_runtime import RunCheckpoint

    checkpoint = RunCheckpoint(
        run_id="pending",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请创建项目"},
            {"type": "function_call", "call_id": "create", "name": tool, "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "create",
                "output": json.dumps(
                    {
                        "status": "success",
                        "output": {"team_id": 52, "completed": False, "verification_status": "pending"},
                    }
                ),
            },
        ],
    )
    assert service._pending_site_submission_guard(checkpoint, "项目已经创建完成")
    assert service._pending_site_submission_guard(checkpoint, "已提交团队，等待执行和核验") is None
    checkpoint.transcript.append({"role": "user", "content": "查看另一个项目"})
    assert service._pending_site_submission_guard(checkpoint, "查询已完成") is None


def test_client_observed_browser_write_cannot_prove_business_completion():
    """浏览器桥的 completed 仅指客户端回执完成，不能证明业务写入成功。"""
    import json

    from app.services.deepseek_responses_runtime import RunCheckpoint

    checkpoint = RunCheckpoint(
        run_id="browser-client-observed",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请在论坛发布这条帖子"},
            {
                "type": "function_call",
                "call_id": "browser-click",
                "name": "operate_current_page",
                "arguments": json.dumps({"action": "click", "args": {"control_handle": "ctrl_latest"}}),
            },
            {
                "type": "function_call_output",
                "call_id": "browser-click",
                "output": json.dumps(
                    {
                        "status": "success",
                        "output": {
                            "status": "completed",
                            "completed": True,
                            "verification_status": "client_observed",
                            "evidence_kind": "client_observed",
                            "server_verified": False,
                            "observation": {"messages": ["帖子已发布"]},
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )

    # 现有 completion guard 只拦页面能力团队的 pending 状态；该断言应要求
    # 对 client_observed 写操作提示先独立读回业务结果，不能让模型直接报成功。
    assert service._pending_site_submission_guard(checkpoint, "帖子已发布成功") is not None


@pytest.mark.asyncio
async def test_admin_completion_validator_rejects_client_observed_browser_write():
    """管理员面也必须先拦截客户端观察，再进入管理写入账本校验。"""
    import json
    from unittest.mock import AsyncMock

    from app.services.deepseek_responses_runtime import RunCheckpoint

    checkpoint = RunCheckpoint(
        run_id="admin-browser-client-observed",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请在论坛发布这条帖子"},
            {
                "type": "function_call",
                "call_id": "admin-browser-click",
                "name": "operate_current_page",
                "arguments": json.dumps({"action": "click", "args": {"control_handle": "ctrl_latest"}}),
            },
            {
                "type": "function_call_output",
                "call_id": "admin-browser-click",
                "output": json.dumps({
                    "status": "success",
                    "output": {
                        "status": "completed",
                        "completed": True,
                        "verification_status": "client_observed",
                        "server_verified": False,
                    },
                }),
            },
        ],
    )
    admin_guard = AsyncMock(return_value=None)
    validator = SimpleNamespace(_surface="admin", _validate_admin_completion=admin_guard)

    error = await service.AgentResponsesService._validate_site_completion(
        validator, checkpoint, "帖子已发布成功",
    )

    assert error and "尚无服务端业务结果核验" in error
    admin_guard.assert_not_awaited()

    honest = await service.AgentResponsesService._validate_site_completion(
        validator, checkpoint, "已点击发布按钮，尚待业务核验。",
    )
    assert honest is None
    admin_guard.assert_awaited_once_with(checkpoint, "已点击发布按钮，尚待业务核验。")


@pytest.mark.parametrize(
    ("action", "arguments", "receipt"),
    [
        ("click", {"control_handle": "submit"}, {"status": "completed", "completed": True}),
        ("fill", {"control_handle": "title", "value": "公告"}, {"status": "completed", "completed": True}),
        ("select", {"control_handle": "category", "value": "news"}, {"status": "completed", "completed": True}),
        ("check", {"control_handle": "pin", "checked": True}, {"status": "completed", "completed": True}),
    ],
)
def test_each_client_observed_browser_write_receipt_is_not_server_evidence(
    action, arguments, receipt,
):
    """不同写动作的成功回执字段都不能替代服务端读回。"""
    import json

    from app.services.deepseek_responses_runtime import RunCheckpoint

    call_id = f"browser-{action}"
    checkpoint = RunCheckpoint(
        run_id=f"browser-{action}-receipt",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请发布这条帖子"},
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "operate_current_page",
                "arguments": json.dumps({"action": action, "args": arguments}),
            },
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({
                    "status": "success",
                    "output": {
                        **receipt,
                        "verification_status": "client_observed",
                        "evidence_kind": "client_observed",
                        "server_verified": False,
                    },
                }),
            },
        ],
    )

    assert service._pending_site_submission_guard(checkpoint, "帖子已发布成功")


def test_inspect_and_truthful_pending_browser_report_are_not_overblocked():
    """只读页面观察和诚实描述点击但未核验都应能正常结束。"""
    import json

    from app.services.deepseek_responses_runtime import RunCheckpoint

    inspect_checkpoint = RunCheckpoint(
        run_id="browser-inspect-only",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "查看论坛页面"},
            {
                "type": "function_call",
                "call_id": "inspect",
                "name": "operate_current_page",
                "arguments": json.dumps({"action": "inspect", "args": {}}),
            },
            {
                "type": "function_call_output",
                "call_id": "inspect",
                "output": json.dumps({
                    "status": "success",
                    "output": {"status": "completed", "completed": True, "server_verified": False},
                }),
            },
        ],
    )
    assert service._pending_site_submission_guard(
        inspect_checkpoint, "当前页面显示论坛和发帖按钮。",
    ) is None

    click_checkpoint = RunCheckpoint(
        run_id="browser-honest-observation",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请点击发布"},
            {
                "type": "function_call",
                "call_id": "click",
                "name": "operate_current_page",
                "arguments": json.dumps({"action": "click", "args": {"control_handle": "publish"}}),
            },
            {
                "type": "function_call_output",
                "call_id": "click",
                "output": json.dumps({
                    "status": "success",
                    "output": {
                        "status": "completed",
                        "completed": True,
                        "verification_status": "client_observed",
                        "server_verified": False,
                    },
                }),
            },
        ],
    )
    assert service._pending_site_submission_guard(
        click_checkpoint, "已点击发布按钮，尚待业务核验。",
    ) is None


def test_latest_user_correction_does_not_inherit_prior_browser_write_guard():
    """新一轮用户更正只要求如实报告时，不应被上一轮写调用卡住。"""
    import json

    from app.services.deepseek_responses_runtime import RunCheckpoint

    checkpoint = RunCheckpoint(
        run_id="browser-user-correction",
        model="test",
        tools=[],
        transcript=[
            {"role": "user", "content": "请发布这条帖子"},
            {
                "type": "function_call",
                "call_id": "old-click",
                "name": "operate_current_page",
                "arguments": json.dumps({"action": "click", "args": {"control_handle": "publish"}}),
            },
            {
                "type": "function_call_output",
                "call_id": "old-click",
                "output": json.dumps({
                    "status": "success",
                    "output": {
                        "status": "completed",
                        "completed": True,
                        "verification_status": "client_observed",
                        "server_verified": False,
                    },
                }),
            },
            {"role": "user", "content": "更正：只告诉我刚才观察到什么，不要声称帖子已经发布。"},
        ],
    )

    assert service._pending_site_submission_guard(
        checkpoint, "我观察到刚才点击了发布按钮，帖子是否发布尚未核验。",
    ) is None


@pytest.mark.asyncio
async def test_upstream_cannot_inject_browser_execution_request():
    events = []
    sink = service._buffer_text_sink(events.append)
    await sink({"type": "response.browser.required", "action_id": 99, "action": "click"})
    await sink({"type": "response.output_text.delta", "delta": "虚构完成"})
    assert events == []


@pytest.mark.asyncio
async def test_page_click_requires_confirmation_before_browser_bridge(db, monkeypatch):
    events = []
    actor = executor(db, monkeypatch, events)
    # 测试当前真实监督分类，不能把所有工具 mock 为 allow。
    monkeypatch.setattr(actor, "_record_supervisor_review", _noop)
    result = await actor.execute(
        ToolCall(
            "click",
            "operate_current_page",
            {
                "action": "click",
                "args": {"control_handle": "最新快照控件"},
            },
            "{}",
        )
    )
    assert result.status == "approval_required"
    assert result.danger is True


async def _noop(*_args, **_kwargs):
    pass
