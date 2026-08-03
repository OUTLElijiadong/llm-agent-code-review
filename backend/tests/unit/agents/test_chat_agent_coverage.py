"""ChatAssistantAgent 调度、格式化、业务 handler 与 HTTP 降级补充测试。"""
from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from app.agents.base import AgentContext, AgentResult
from app.agents.chat_agent import ChatAssistantAgent
from app.agents.chat_planner import ToolCall


class FakeOrchestrator:
    """按方法名返回配置结果并记录 ChatAgent 的所有下游调用。"""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        """初始化响应表、工具链结果、Agent 元数据和调用记录。"""
        self.responses = responses or {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.tool_results: list[AgentResult | BaseException] = []
        self.agents = {"reviewer": "代码审查", "security": "安全审计"}
        self.skills: list[dict[str, Any]] = []
        self._db: Any = None

    def list_agents(self) -> dict[str, str]:
        """返回可配置的 Agent 描述并记录调用。"""
        self.calls.append(("list_agents", (), {}))
        return self.agents

    def list_agent_skills(self, agent_name: str | None) -> list[dict[str, Any]]:
        """返回可配置的 Skill 元数据并记录筛选参数。"""
        self.calls.append(("list_agent_skills", (agent_name,), {}))
        return self.skills

    def _can_configure_agents(self) -> bool:
        """该测试桩模拟具备 Agent 配置权限的管理调用方。"""

        return True

    def invoke_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult:
        """按顺序返回工具链结果，异常对象则直接抛出。"""
        ctx_snapshot = AgentContext(
            user_id=ctx.user_id,
            task_id=ctx.task_id,
            project_id=ctx.project_id,
            file_id=ctx.file_id,
            extra=dict(ctx.extra),
        )
        self.calls.append(
            (
                "invoke_tool",
                (),
                {"tool_name": tool_name, "arguments": arguments, "ctx": ctx_snapshot},
            ),
        )
        if not self.tool_results:
            raise AssertionError("未配置 invoke_tool 结果")
        result = self.tool_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """为其余 Orchestrator handler 动态生成有记录的可调用方法。"""
        if name.startswith("__"):
            raise AttributeError(name)

        def invoke(*args: Any, **kwargs: Any) -> Any:
            """记录动态方法调用并返回配置值或调用配置工厂。"""
            self.calls.append((name, args, kwargs))
            if name not in self.responses:
                raise AssertionError(f"未配置 Orchestrator.{name} 响应")
            value = self.responses[name]
            return value(*args, **kwargs) if callable(value) else value

        return invoke


class FakePlanner:
    """返回预设调用链或抛出预设规划异常。"""

    def __init__(self, outcome: list[ToolCall] | BaseException) -> None:
        """保存规划结果并初始化调用记录。"""
        self.outcome = outcome
        self.calls: list[tuple[dict[str, Any], str, AgentContext | None]] = []

    def plan(
        self,
        intent: dict[str, Any],
        user_message: str = "",
        ctx: AgentContext | None = None,
    ) -> list[ToolCall]:
        """记录规划输入，异常配置则抛出，否则返回调用链副本。"""
        self.calls.append((intent, user_message, ctx))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return list(self.outcome)


class FakeHttpResponse:
    """模拟 httpx.Response 的状态码与 JSON 响应。"""

    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        """保存状态码和响应体。"""
        self.status_code = status_code
        self.body = body or {}

    def json(self) -> dict[str, Any]:
        """返回配置的 JSON 对象。"""
        return self.body


class FakeHttpClient:
    """按队列返回 HTTP 响应或抛出网络异常，并记录请求。"""

    def __init__(
        self,
        outcomes: list[FakeHttpResponse | BaseException],
        requests: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        """保存共享响应队列、请求记录和 Client 构造参数。"""
        self.outcomes = outcomes
        self.requests = requests
        self.kwargs = kwargs

    def __enter__(self) -> "FakeHttpClient":
        """进入同步 httpx Client 上下文。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """退出同步 Client 上下文且不吞掉异常。"""
        return None

    def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        """记录 POST 请求并消费下一项响应或异常。"""
        self.requests.append({"url": url, "client": self.kwargs, **kwargs})
        if not self.outcomes:
            raise AssertionError("未配置 HTTP 响应")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ChatAssistantAgent:
    """创建不向全局 EventBus 发送事件的 ChatAssistantAgent。"""
    chat_agent = ChatAssistantAgent()

    def ignore_emit(*args: Any, **kwargs: Any) -> None:
        """屏蔽与当前测试断言无关的全局事件副作用。"""
        return None

    monkeypatch.setattr(chat_agent, "_emit", ignore_emit)
    return chat_agent


def _ok(data: Any, *, model: str = "fake-model", duration_ms: int = 7) -> AgentResult:
    """构造成功的 AgentResult，统一填充模型、耗时与 token。"""
    return AgentResult(
        success=True,
        data=data,
        model=model,
        duration_ms=duration_ms,
        tokens={"total": 12},
    )


def _failed(message: str = "downstream failed") -> AgentResult:
    """构造失败的 AgentResult 供传播路径测试。"""
    return AgentResult(success=False, error=message)


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[FakeHttpResponse | BaseException],
) -> list[dict[str, Any]]:
    """替换 httpx.Client 并返回共享请求记录列表。"""
    import httpx

    requests: list[dict[str, Any]] = []

    def client_factory(**kwargs: Any) -> FakeHttpClient:
        """为每次重试创建共享响应队列的 fake Client。"""
        return FakeHttpClient(outcomes, requests, **kwargs)

    monkeypatch.setattr(
        "app.agents.chat_agent.pin_public_http_url",
        lambda url: __import__("app.utils.public_http", fromlist=["PinnedPublicUrl"]).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    monkeypatch.setattr(httpx, "Client", client_factory)
    return requests


def test_system_prompt_lists_agents_and_empty_execute_fails(
    agent: ChatAssistantAgent,
) -> None:
    """系统提示词应包含已注入 Agent，空消息列表应立即返回参数错误。"""
    assert "当前可调度的Agent" not in agent.system_prompt
    orchestrator = FakeOrchestrator()
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    prompt = agent.system_prompt
    result = agent.execute([])

    assert "reviewer: 代码审查" in prompt
    assert "security: 安全审计" in prompt
    assert result.success is False
    assert result.error == "消息列表为空"


def test_execute_clarifies_missing_fields_before_planning(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关键意图缺字段时应先返回 Clarify，不调用第二层规划器。"""
    planner = FakePlanner(AssertionError("planner should not run"))
    agent._planner = planner  # type: ignore[assignment]

    def classify(last_msg: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """固定返回缺少 project_id 的删除意图。"""
        return {"intent": "delete_project", "reason": "删除", "payload": {}}

    monkeypatch.setattr(agent, "_classify_intent", classify)
    result = agent.execute([{"role": "user", "content": "删除项目"}], AgentContext(user_id=5))

    assert result.success is True
    assert result.data["clarify"]["questions"][0]["key"] == "project_id"
    assert planner.calls == []


@pytest.mark.parametrize(
    ("tool_name", "arguments", "intent_name", "question_type"),
    [
        ("create_project", {"project_name": "demo"}, "create_project", "confirm"),
        ("delete_project", {"project_id": 7}, "delete_project", "danger_confirm"),
        ("start_review", {"project_id": 7}, "start_review", "confirm"),
        ("audit_security_for_file", {"file_id": 3}, "security_audit", "confirm"),
        ("audit_security_for_task", {"task_id": 5}, "security_audit", "confirm"),
        ("audit_security_for_project", {"project_id": 7}, "security_audit", "confirm"),
    ],
)
def test_execute_plan_cannot_bypass_write_confirmation(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
    intent_name: str,
    question_type: str,
) -> None:
    """只读分类被 Planner 改写成写工具时，执行器必须阻断真实调用。"""
    orchestrator = FakeOrchestrator()
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]
    agent._planner = FakePlanner([ToolCall(tool_name, arguments, "错误规划")])  # type: ignore[assignment]
    monkeypatch.setattr(
        agent,
        "_classify_intent",
        lambda *_args, **_kwargs: {
            "intent": "list_projects",
            "reason": "只读查询",
            "payload": {},
        },
    )

    result = agent.execute([{"role": "user", "content": "查看项目"}], AgentContext(user_id=5))

    assert result.success is True
    assert result.data["clarify"]["intent"] == intent_name
    assert result.data["clarify"]["questions"][0]["type"] == question_type
    assert not any(call[0] == "invoke_tool" for call in orchestrator.calls)


def test_execute_double_layer_success_and_chat_bypasses_planner(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非聊天意图应执行规划链，普通聊天应直接走固定 fallback。"""
    orchestrator = FakeOrchestrator()
    orchestrator.tool_results = [_ok("规划完成")]
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]
    planner = FakePlanner([ToolCall("detect_language", {"name": "demo"}, "识别语言")])
    agent._planner = planner  # type: ignore[assignment]

    def classify_tool(last_msg: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """返回无需澄清的语言检测意图。"""
        return {"intent": "detect_language", "reason": "检测", "payload": {}}

    monkeypatch.setattr(agent, "_classify_intent", classify_tool)
    result = agent.execute([{"role": "user", "content": "识别语言"}])

    assert result.success is True
    assert result.data == "规划完成"
    assert planner.calls[0][1] == "识别语言"
    assert agent._last_plan_steps[0]["tool_name"] == "detect_language"

    def classify_chat(last_msg: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """返回普通聊天意图。"""
        return {"intent": "chat", "reason": "闲聊", "payload": {}}

    def chat_result(messages: list[dict[str, Any]], ctx: AgentContext | None) -> AgentResult:
        """返回可识别的聊天 fallback 结果。"""
        return _ok("直接聊天")

    monkeypatch.setattr(agent, "_classify_intent", classify_chat)
    monkeypatch.setattr(agent, "_handle_chat", chat_result)
    chat = agent.execute([{"role": "user", "content": "你好"}])

    assert chat.data == "直接聊天"
    assert len(planner.calls) == 1
    assert agent._last_plan_steps == []


@pytest.mark.parametrize(
    "planning_error",
    [TimeoutError("slow"), ValueError("invalid plan"), RuntimeError("unexpected")],
)
def test_execute_planner_errors_fall_back_to_single_dispatch(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
    planning_error: BaseException,
) -> None:
    """规划超时、非法结果和未知异常均应降级到单层 handler。"""
    agent._planner = FakePlanner(planning_error)  # type: ignore[assignment]

    def classify(last_msg: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """固定返回非聊天意图以触发第二层规划。"""
        return {"intent": "list_agents", "reason": "查看", "payload": {}}

    def fallback(
        intent: dict[str, Any],
        messages: list[dict[str, Any]],
        ctx: AgentContext | None,
    ) -> AgentResult:
        """返回可观察的单层降级结果。"""
        return _ok(f"fallback:{intent['intent']}")

    monkeypatch.setattr(agent, "_classify_intent", classify)
    monkeypatch.setattr(agent, "_dispatch_single", fallback)

    result = agent.execute([{"role": "user", "content": "列出 Agent"}])

    assert result.data == "fallback:list_agents"
    assert agent._last_plan_steps == []


def test_double_layer_switch_reads_runtime_setting(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """双层调度开关应实时读取配置值。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "chat_double_layer_enabled", False)
    assert agent._double_layer_enabled() is False
    monkeypatch.setattr(settings, "chat_double_layer_enabled", True)
    assert agent._double_layer_enabled() is True


def test_execute_plan_requires_orchestrator_and_empty_plan_uses_chat(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入编排器时计划失败；空计划在已注入时回退普通聊天。"""
    missing = agent._execute_plan([], [{"role": "user", "content": "hi"}], None)
    assert missing.success is False
    assert missing.error == "Orchestrator 未注入"

    agent.set_orchestrator(FakeOrchestrator())  # type: ignore[arg-type]

    def chat_result(messages: list[dict[str, Any]], ctx: AgentContext | None) -> AgentResult:
        """返回空计划 fallback 结果。"""
        return _ok("empty-plan-chat")

    monkeypatch.setattr(agent, "_handle_chat", chat_result)
    fallback = agent._execute_plan([], [{"role": "user", "content": "hi"}], None)
    assert fallback.data == "empty-plan-chat"


def test_execute_plan_chains_previous_output_and_returns_string(
    agent: ChatAssistantAgent,
) -> None:
    """多步计划应把前一步输出注入上下文并透传最后的字符串结果。"""
    orchestrator = FakeOrchestrator()
    orchestrator.tool_results = [
        _ok({"project_id": 7}),
        _ok("最终回复", model="last-model", duration_ms=4),
    ]
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]
    plan = [
        ToolCall("detect_language", {"name": "demo"}, "先识别"),
        ToolCall("list_projects", {"page": 1}, "再查询"),
    ]

    result = agent._execute_plan(plan, [{"role": "user", "content": "执行"}], None)

    assert result.success is True
    assert result.data == "最终回复"
    assert result.model == "last-model"
    tool_calls = [call for call in orchestrator.calls if call[0] == "invoke_tool"]
    assert tool_calls[0][2]["ctx"].extra["step_index"] == 0
    assert tool_calls[0][2]["ctx"].extra["prev_output"] is None
    assert tool_calls[1][2]["ctx"].extra["step_index"] == 1
    assert tool_calls[1][2]["ctx"].extra["prev_output"] == {"project_id": 7}
    assert [step["tool_name"] for step in agent._last_plan_steps] == [
        "detect_language",
        "list_projects",
    ]


def test_execute_plan_formats_structured_result_and_stops_on_exception(
    agent: ChatAssistantAgent,
) -> None:
    """结构化结果应附带步骤树，工具异常应转为失败并终止后续步骤。"""
    orchestrator = FakeOrchestrator()
    orchestrator.tool_results = [_ok({"content": "结构化完成", "value": 1})]
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    success = agent._execute_plan(
        [ToolCall("dashboard_summary", {}, "汇总")],
        [{"role": "user", "content": "汇总"}],
        AgentContext(),
    )
    assert success.success is True
    assert "结构化完成" in success.data["content"]
    assert success.data["plan_steps"][0]["success"] is True

    orchestrator.tool_results = [_ok({"first": True}), RuntimeError("tool exploded")]
    failure = agent._execute_plan(
        [ToolCall("first", {}, "第一步"), ToolCall("second", {}, "第二步")],
        [{"role": "user", "content": "执行"}],
        AgentContext(),
    )
    assert failure.success is False
    assert failure.data["failed_at_step"] == 2
    assert "tool exploded" in failure.error
    assert len(failure.data["plan_steps"]) == 2


def test_format_plan_result_supports_dict_string_and_json_payload(
    agent: ChatAssistantAgent,
) -> None:
    """计划摘要应渲染成功/失败图标、理由及三种常见结果形态。"""
    steps = [
        {"step_index": 0, "tool_name": "a", "duration_ms": 3, "success": True, "reason": "A"},
        {"step_index": 1, "tool_name": "b", "duration_ms": 4, "success": False, "reason": ""},
    ]
    dict_text = agent._format_plan_result(steps, _ok({"content": "正文"}))
    string_text = agent._format_plan_result(steps, _ok("文本"))
    json_text = agent._format_plan_result(steps, _ok([{"id": 1}]))

    assert "✅" in dict_text and "❌" in dict_text and "正文" in dict_text
    assert "文本" in string_text
    assert '"id": 1' in json_text


def test_dispatch_helpers_route_known_and_reject_unknown_intents(
    agent: ChatAssistantAgent,
) -> None:
    """固定路由和 Clarify 回填路由应支持已知 intent 并明确拒绝未知 intent。"""
    orchestrator = FakeOrchestrator()
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    listed = agent._dispatch_single(
        {"intent": "list_agents", "payload": {}},
        [{"role": "user", "content": "agents"}],
        None,
    )
    unsupported = agent.dispatch_with_payload("not_supported", {}, None)
    clarified = agent.dispatch_with_payload("list_agents", {}, None)

    assert listed.success is True
    assert "reviewer" in listed.data
    assert clarified.success is True
    assert unsupported.success is False
    assert unsupported.error == "不支持的 intent: not_supported"


def test_classify_intent_uses_recent_context_and_falls_back(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """意图分类只发送最近三轮上下文，并在非字典或异常时回退 chat。"""
    prompts: list[str] = []

    def classify_success(prompt: str) -> AgentResult:
        """记录分类 prompt 并返回结构化意图。"""
        prompts.append(prompt)
        return _ok({"intent": "dashboard", "reason": "统计", "payload": {}})

    monkeypatch.setattr(agent, "call_json", classify_success)
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
        {"role": "user", "content": "fourth"},
    ]
    result = agent._classify_intent("fourth", messages)
    assert result["intent"] == "dashboard"
    assert "first" not in prompts[0]
    assert "second" in prompts[0] and "fourth" in prompts[0]

    def classify_non_dict(prompt: str) -> AgentResult:
        """返回成功但非字典结果以验证 fallback。"""
        return _ok(["bad"])

    monkeypatch.setattr(agent, "call_json", classify_non_dict)
    assert agent._classify_intent("x", messages)["intent"] == "chat"

    def classify_error(prompt: str) -> AgentResult:
        """抛出分类异常以验证异常降级。"""
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(agent, "call_json", classify_error)
    assert agent._classify_intent("x", messages)["reason"] == "fallback"


def test_ai_prompt_required_and_all_scope_handlers(
    agent: ChatAssistantAgent,
) -> None:
    """AI 提示词必填规则与 issue/task/project 三类委派应正确传参和格式化。"""
    assert agent._ai_prompt_required({}) == ["scope"]
    assert agent._ai_prompt_required({"scope": "issue"}) == ["target_tool", "issue_id"]
    assert agent._ai_prompt_required({"scope": "task"}) == ["target_tool", "task_id"]
    assert agent._ai_prompt_required({"scope": "project"}) == ["target_tool", "project_id"]
    assert agent._ai_prompt_required({"scope": "bad"}) == ["scope"]

    prompts = [
        {
            "target_label": "Cursor",
            "title": f"修复 {index}",
            "file_path": f"src/{index}.py",
            "lines": f"L{index}",
            "prompt_text": "x" * (650 if index == 0 else 20),
        }
        for index in range(4)
    ]
    response = _ok({"prompts": prompts, "summary": "已生成修复包"})
    orchestrator = FakeOrchestrator(
        {
            "generate_ai_prompt_for_issue": response,
            "generate_ai_prompt_for_task": response,
            "generate_ai_prompt_for_project": response,
        },
    )
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    issue = agent._handle_generate_ai_prompt(
        {"payload": {"scope": "issue", "issue_id": "3", "target_tool": "cursor"}},
        None,
    )
    task = agent._handle_generate_ai_prompt(
        {
            "payload": {
                "scope": "task",
                "task_id": "4",
                "target_tool": "chatgpt",
                "severity": "high",
            },
        },
        None,
    )
    project = agent._handle_generate_ai_prompt(
        {
            "payload": {
                "scope": "project",
                "project_id": "5",
                "target_tool": "claude_code",
                "top_n": "8",
            },
        },
        None,
    )
    unknown = agent._handle_generate_ai_prompt({"payload": {"scope": "other"}}, None)

    assert issue.success and task.success and project.success
    assert "还有 1 条提示词" in issue.data
    assert "\n..." in issue.data
    assert unknown.error == "未知 scope: other"
    task_call = next(call for call in orchestrator.calls if call[0] == "generate_ai_prompt_for_task")
    assert task_call[2]["severity_filter"] == "high"
    project_call = next(
        call for call in orchestrator.calls if call[0] == "generate_ai_prompt_for_project"
    )
    assert project_call[2]["top_n"] == 8

    orchestrator.responses["generate_ai_prompt_for_issue"] = _failed("prompt failed")
    failed = agent._handle_generate_ai_prompt(
        {"payload": {"scope": "issue", "issue_id": 1}},
        None,
    )
    assert failed.error == "prompt failed"


def test_security_audit_all_scopes_and_rich_findings(
    agent: ChatAssistantAgent,
) -> None:
    """安全审计应覆盖三类 scope、严重度统计、标签、截断和数据流摘要。"""
    findings = [
        {
            "severity": severity,
            "title": f"风险 {index}",
            "file_path": f"src/{index}.py",
            "lines": f"L{index}",
            "owasp": "A01" if index == 0 else "",
            "cwe": "CWE-79" if index == 0 else "",
        }
        for index, severity in enumerate(["严重", "高", "中", "低", "未知", "高"])
    ]
    response = _ok(
        {
            "findings": findings,
            "risk_score": 42,
            "summary": "发现高风险路径",
            "threat_model": {"data_flows": [{"source": "input", "sink": "exec"}]},
        },
    )
    orchestrator = FakeOrchestrator(
        {
            "audit_security_for_file": response,
            "audit_security_for_task": response,
            "audit_security_for_project": response,
        },
    )
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    file_result = agent._handle_security_audit(
        {"payload": {"scope": "file", "file_id": "1", "scan_depth": "deep"}},
        None,
    )
    task_result = agent._handle_security_audit(
        {"payload": {"scope": "task", "task_id": "2"}},
        None,
    )
    project_result = agent._handle_security_audit(
        {
            "payload": {
                "scope": "project",
                "project_id": "3",
                "top_n": "12",
                "trace_dataflow": False,
            },
        },
        None,
    )
    unknown = agent._handle_security_audit({"payload": {"scope": "bad"}}, None)

    assert file_result.success and task_result.success and project_result.success
    assert "🚨" in file_result.data
    assert "A01 CWE-79" in file_result.data
    assert "还有 1 处风险" in file_result.data
    assert "1 条可达攻击路径" in file_result.data
    assert unknown.error == "未知 scope: bad"

    orchestrator.responses["audit_security_for_task"] = _failed("audit failed")
    assert agent._handle_security_audit(
        {"payload": {"scope": "task", "task_id": 2}},
        None,
    ).error == "audit failed"


def test_evolution_and_skill_handlers_cover_validation_and_rich_results(
    agent: ChatAssistantAgent,
) -> None:
    """进化与 Skill handler 应校验参数、传播失败并渲染列表和指标。"""
    evolution_data = {
        "summary": "已从反馈中蒸馏规则",
        "proposals": [
            {"title": "规则一", "description": "说明"},
            "文本建议",
            {"rule_name": "规则三", "summary": "摘要"},
            {"name": "规则四"},
            {"title": "规则五"},
            {"title": "规则六"},
        ],
        "applied_count": 2,
    }
    skill_data = {
        "message": "主动检查完成",
        "findings": [
            {"title": "异常一", "description": "详情"},
            "普通项",
            {"name": "异常三"},
            {"type": "异常四"},
            {"title": "异常五"},
            {"title": "异常六"},
        ],
        "metrics": {"checked": 8, "failed": 1},
    }
    orchestrator = FakeOrchestrator(
        {
            "trigger_evolution": _ok(evolution_data),
            "invoke_skill": _ok(skill_data),
        },
    )
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    invalid_window = agent._handle_evolution_trigger(
        {"payload": {"window_days": "bad"}},
        AgentContext(user_id=1),
    )
    negative_window = agent._handle_evolution_trigger(
        {"payload": {"agent_name": "reviewer", "window_days": -3}},
        None,
    )
    assert invalid_window.success and negative_window.success
    assert "窗口 90 天" in invalid_window.data
    assert "还有 1 条" in invalid_window.data
    assert "已应用: **2**" in invalid_window.data

    missing = agent._handle_agent_skill_invoke({"payload": {"agent_name": "reviewer"}}, None)
    invoked = agent._handle_agent_skill_invoke(
        {
            "payload": {
                "agent_name": "reviewer",
                "skill_name": "reviewer.proactive",
                "action": "scan",
                "params": {"limit": 5},
            },
        },
        AgentContext(user_id=2),
    )
    assert missing.success is False
    assert invoked.success is True
    assert "findings (6)" in invoked.data
    assert "还有 1 条" in invoked.data
    assert "`checked`: 8" in invoked.data
    invoke_call = next(call for call in orchestrator.calls if call[0] == "invoke_skill")
    assert invoke_call[2]["params"] == {"action": "scan", "limit": 5}
    assert "Proactive Skill" in invoked.data

    orchestrator.responses["trigger_evolution"] = _failed("evolution failed")
    orchestrator.responses["invoke_skill"] = _failed("skill failed")
    assert agent._handle_evolution_trigger({"payload": {}}, None).error == "evolution failed"
    assert agent._handle_agent_skill_invoke(
        {
            "payload": {
                "agent_name": "reviewer",
                "skill_name": "reviewer.self_improve",
                "params": "ignored",
            },
        },
        None,
    ).error == "skill failed"


def test_agent_status_handles_empty_grouped_records_and_record_error(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent 状态应渲染空态、分组 Skill、调用记录并降级记录查询异常。"""
    from app.services import skill_service

    orchestrator = FakeOrchestrator()
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]
    empty = agent._handle_agent_status({"payload": {}}, None)
    assert "未找到 Skill" in empty.data

    orchestrator.skills = [
        {
            "agent_name": "alpha",
            "name": "alpha.self_improve",
            "type": "self_improvement",
            "description": "进化",
            "invocable": True,
        },
        {
            "agent_name": "beta",
            "name": "beta.proactive",
            "type": "proactive",
            "description": "巡检",
            "invocable": False,
        },
    ]
    orchestrator._db = object()

    def list_records(**kwargs: Any) -> list[dict[str, Any]]:
        """返回一条稳定的 Skill 调用记录。"""
        return [
            {
                "skill_name": "alpha.self_improve",
                "agent_name": "alpha",
                "trigger_type": "manual",
                "success": True,
                "duration_ms": 9,
                "create_time": "now",
            },
        ]

    monkeypatch.setattr(skill_service, "list_recent_records", list_records)
    detailed = agent._handle_agent_status(
        {"payload": {"agent_name": "alpha", "detail": "all"}},
        None,
    )
    assert "alpha.self_improve" in detailed.data
    assert "beta.proactive" in detailed.data
    assert "最近调用记录 (1 条)" in detailed.data

    def fail_records(**kwargs: Any) -> list[dict[str, Any]]:
        """模拟调用记录数据库查询失败。"""
        raise RuntimeError("records unavailable")

    monkeypatch.setattr(skill_service, "list_recent_records", fail_records)
    degraded = agent._handle_agent_status(
        {"payload": {"detail": "records"}},
        None,
    )
    assert "读取调用记录失败: records unavailable" in degraded.data


def test_core_agent_handlers_render_successful_results(
    agent: ChatAssistantAgent,
) -> None:
    """语言、项目分析和代码审查 handler 应格式化下游成功结果。"""
    issues = [
        {
            "severity": severity,
            "issue_type": f"类型{index}",
            "line": index + 1,
            "description": "描述",
        }
        for index, severity in enumerate(
            ["severe", "high", "medium", "low", "unknown"] * 2,
        )
    ]
    orchestrator = FakeOrchestrator(
        {
            "detect_language": _ok(
                {
                    "language_name": "Python",
                    "language": "python",
                    "confidence": 0.98,
                    "reason": "扩展名",
                },
            ),
            "analyze_project": _ok(
                {
                    "project_name": "Demo",
                    "language_name": "Python",
                    "language": "python",
                    "description": "示例项目",
                },
            ),
            "review_code": _ok(json.dumps({"issues": issues}, ensure_ascii=False)),
        },
    )
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    language = agent._handle_detect_language(
        {"payload": {"project_name": "demo", "description": "api"}},
        None,
    )
    analyzed = agent._handle_analyze_project(
        {"payload": {"folder_name": "demo", "file_names": ["a.py", "b.py"]}},
        None,
    )
    no_code = agent._handle_review_code({"payload": {}}, None)
    reviewed = agent._handle_review_code(
        {"payload": {"code": "a = 1\nprint(a)", "language": "python", "file_name": "a.py"}},
        None,
    )

    assert "Python" in language.data and "0.98" in language.data
    assert "分析文件数: 2" in analyzed.data
    assert no_code.success is False
    assert "共发现 10 个问题" in reviewed.data
    assert "还有 2 个问题未显示" in reviewed.data

    orchestrator.responses["detect_language"] = _failed("detect failed")
    assert agent._handle_detect_language({"payload": {}}, None).error == "detect failed"


def test_platform_service_handlers_render_lists_and_mutations(
    agent: ChatAssistantAgent,
) -> None:
    """项目、任务、问题、文件、仪表盘、规则、报告与启动审查应渲染关键字段。"""
    responses = {
        "list_projects": _ok(
            {
                "total": 1,
                "items": [
                    {
                        "id": 1,
                        "project_name": "Demo",
                        "language": "python",
                        "file_count": 2,
                        "status": "active",
                    },
                ],
            },
        ),
        "create_project": _ok(
            {"id": 2, "project_name": "New", "language": "go", "status": "active"},
        ),
        "delete_project": _ok({"deleted": True}),
        "list_review_tasks": _ok(
            {
                "total": 1,
                "items": [
                    {
                        "id": 3,
                        "task_name": "Quick",
                        "score": 91,
                        "total_issues": 2,
                        "status": "completed",
                    },
                ],
            },
        ),
        "list_review_issues": _ok(
            {
                "total": 1,
                "items": [
                    {
                        "severity": "high",
                        "issue_type": "security",
                        "title": "SQL 注入",
                        "file_name": "db.py",
                        "line_number": 8,
                    },
                ],
            },
        ),
        "list_code_files": _ok(
            {
                "total": 1,
                "items": [{"file_name": "main.py", "language": "python", "line_count": 20}],
            },
        ),
        "dashboard_summary": _ok(
            {
                "total_projects": 2,
                "total_tasks": 3,
                "avg_score": 88,
                "total_issues": 4,
                "severe_issues": 1,
                "high_issues": 1,
                "medium_issues": 1,
                "low_issues": 1,
            },
        ),
        "list_rules": _ok(
            {
                "total": 2,
                "items": [
                    {
                        "rule_name": "Security",
                        "rule_type": "security",
                        "is_builtin": True,
                        "enabled": True,
                    },
                    {
                        "rule_name": "Style",
                        "rule_type": "style",
                        "is_builtin": False,
                        "enabled": False,
                    },
                ],
            },
        ),
        "list_reports": _ok(
            {
                "total": 1,
                "items": [
                    {
                        "task_id": 3,
                        "project_name": "Demo",
                        "score": 91,
                        "review_type": "quick",
                    },
                ],
            },
        ),
        "start_review": _ok({"task_id": 9, "total_files": 2, "status": "pending"}),
    }
    orchestrator = FakeOrchestrator(responses)
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    results = [
        agent._handle_list_projects(None),
        agent._handle_create_project(
            {"payload": {"project_name": "New", "description": "d", "language": "go"}},
            None,
        ),
        agent._handle_delete_project({"payload": {"project_id": 2}}, None),
        agent._handle_list_review_tasks({"payload": {"project_id": 1}}, None),
        agent._handle_list_review_issues({"payload": {"task_id": 3}}, None),
        agent._handle_list_code_files({"payload": {"project_id": 1}}, None),
        agent._handle_dashboard(None),
        agent._handle_list_rules(None),
        agent._handle_list_reports(None),
        agent._handle_start_review(
            {
                "payload": {
                    "project_id": 1,
                    "file_ids": [101, 102],
                    "review_type": "quick",
                    "task_name": "Run",
                },
            },
            None,
        ),
    ]

    assert all(result.success for result in results)
    rendered = "\n".join(str(result.data) for result in results)
    assert "Demo" in rendered
    assert "SQL 注入" in rendered
    assert "main.py" in rendered
    assert "平均评分: **88**" in rendered
    assert "审查任务已启动" in rendered
    start_call = next(call for call in orchestrator.calls if call[0] == "start_review")
    assert start_call[2]["file_ids"] == [101, 102]

    assert agent._handle_delete_project({"payload": {}}, None).success is False
    assert agent._handle_list_review_issues({"payload": {}}, None).success is False
    assert agent._handle_list_code_files({"payload": {}}, None).success is False
    assert agent._handle_start_review({"payload": {}}, None).success is False


def test_list_handlers_render_empty_states_and_propagate_failures(
    agent: ChatAssistantAgent,
) -> None:
    """列表 handler 应显示空态，并原样传播下游失败。"""
    empty = _ok({"total": 0, "items": []})
    orchestrator = FakeOrchestrator(
        {
            "list_projects": empty,
            "list_review_tasks": empty,
            "list_review_issues": empty,
            "list_code_files": empty,
            "list_rules": empty,
            "list_reports": empty,
        },
    )
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    assert "还没有项目" in agent._handle_list_projects(None).data
    assert "暂无审查记录" in agent._handle_list_review_tasks({"payload": {}}, None).data
    assert "暂无问题记录" in agent._handle_list_review_issues(
        {"payload": {"task_id": 1}}, None,
    ).data
    assert "暂无代码文件" in agent._handle_list_code_files(
        {"payload": {"project_id": 1}}, None,
    ).data
    assert "暂无审查报告" in agent._handle_list_reports(None).data

    orchestrator.responses["list_projects"] = _failed("list failed")
    orchestrator.responses["list_rules"] = _failed("rules failed")
    assert agent._handle_list_projects(None).error == "list failed"
    assert agent._handle_list_rules(None).error == "rules failed"


def test_handle_chat_success_limits_history_and_injects_personalization(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通聊天应只发送最后十条历史并把画像/RAG 上下文拼入系统提示词。"""
    from app.services import personalization_service

    orchestrator = FakeOrchestrator()
    orchestrator._db = object()
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    def persona(db: Any, user_id: int, query: str) -> str:
        """返回稳定的个性化上下文并校验入参。"""
        assert db is orchestrator._db
        assert user_id == 7
        assert query == "message-11"
        return "\n[PERSONA]偏好简洁回答"

    monkeypatch.setattr(personalization_service, "chat_context_for_agent", persona)
    body = {
        "choices": [{"finish_reason": "stop", "message": {"content": "你好，已收到"}}],
        "model": "chat-model",
    }
    requests = _install_http(monkeypatch, [FakeHttpResponse(200, body)])
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}"}
        for index in range(12)
    ]

    result = agent._handle_chat(messages, AgentContext(user_id=7))

    assert result.success is True
    assert result.data == "你好，已收到"
    payload = requests[0]["json"]
    assert len(payload["messages"]) == 11
    assert payload["messages"][1]["content"] == "message-2"
    assert payload["messages"][-1]["content"] == "message-11"
    assert "[PERSONA]偏好简洁回答" in payload["messages"][0]["content"]
    assert requests[0]["url"] == "https://93.184.216.34/chat/completions"
    assert requests[0]["headers"]["Host"] == "api.deepseek.com"
    assert requests[0]["extensions"] == {"sni_hostname": "api.deepseek.com"}
    assert requests[0]["client"]["trust_env"] is False


def test_handle_chat_retries_status_errors_and_network_failure(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429、5xx、4xx 应指数退避重试，网络异常应转为明确失败结果。"""
    import time

    agent.set_orchestrator(FakeOrchestrator())  # type: ignore[arg-type]
    sleeps: list[int] = []

    def fake_sleep(seconds: int) -> None:
        """记录指数退避秒数而不真实等待。"""
        sleeps.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    agent._max_retries = 2
    requests = _install_http(
        monkeypatch,
        [FakeHttpResponse(429), FakeHttpResponse(503), FakeHttpResponse(400)],
    )
    failed = agent._handle_chat([{"role": "user", "content": "hello"}], None)

    assert failed.success is False
    assert failed.error == "聊天失败: 调用失败(400)"
    assert sleeps == [2, 4]
    assert len(requests) == 3

    agent._max_retries = 0
    _install_http(monkeypatch, [RuntimeError("network down")])
    network = agent._handle_chat([{"role": "user", "content": "hello"}], None)
    assert network.error == "聊天失败: network down"


def test_handle_chat_personalization_error_degrades_to_plain_prompt(
    agent: ChatAssistantAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """个性化服务异常不得阻断聊天，应继续使用基础系统提示词调用模型。"""
    from app.services import personalization_service

    orchestrator = FakeOrchestrator()
    orchestrator._db = object()
    agent.set_orchestrator(orchestrator)  # type: ignore[arg-type]

    def fail_persona(db: Any, user_id: int, query: str) -> str:
        """模拟画像或知识库查询失败。"""
        raise RuntimeError("persona unavailable")

    monkeypatch.setattr(personalization_service, "chat_context_for_agent", fail_persona)
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "fallback answer"}}]}
    requests = _install_http(monkeypatch, [FakeHttpResponse(200, body)])

    result = agent._handle_chat(
        [{"role": "user", "content": "hello"}],
        AgentContext(user_id=8),
    )

    assert result.success is True
    assert result.data == "fallback answer"
    assert "PERSONA" not in requests[0]["json"]["messages"][0]["content"]
