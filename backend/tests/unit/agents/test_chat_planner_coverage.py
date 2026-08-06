"""ChatPlanner 工具收集、LLM 调用、解析、校验和超时覆盖测试。"""
from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from types import SimpleNamespace
from typing import Any

import pytest

import app.agents.chat_planner as planner_module
from app.agents.base import AgentResult
from app.agents.chat_planner import ChatPlanner, ToolCall
from app.agents.skills.registry import SkillRegistry


class FakeSkill:
    """提供 ChatPlanner 工具元数据所需的最小 Skill。"""

    def __init__(self, name: str, *, invocable: bool = True):
        """保存技能名称与是否允许调用。"""
        self.name = name
        self.description = f"description:{name}"
        self.invocable = invocable

    def _params_schema(self) -> dict[str, Any]:
        """返回包含一个参数的固定 JSON Schema。"""
        return {"type": "object", "properties": {"value": {"type": "string"}}}


class FakeSkillRegistry:
    """返回预置 Skill 列表的注册中心。"""

    skills: list[FakeSkill] = []

    @classmethod
    def instance(cls) -> "FakeSkillRegistry":
        """返回注册中心实例。"""
        return cls()

    def list_all(self) -> list[FakeSkill]:
        """返回预置技能。"""
        return type(self).skills


class FailingSkillRegistry:
    """模拟注册中心初始化失败。"""

    @classmethod
    def instance(cls) -> "FailingSkillRegistry":
        """在工具收集时抛出异常。"""
        raise RuntimeError("registry unavailable")


class FakePlanningAgent:
    """记录规划时临时模型参数并返回预置 AgentResult。"""

    def __init__(self, result: AgentResult):
        """初始化原始模型参数、返回值与调用快照。"""
        self._system_prompt = "original prompt"
        self._temperature = 0.8
        self._max_tokens = 256
        self.result = result
        self.snapshots: list[tuple[str, float, int, str, Any]] = []

    def call_json(self, prompt: str, ctx: Any = None) -> AgentResult:
        """记录调用时参数并返回预置结果。"""
        self.snapshots.append(
            (self._system_prompt, self._temperature, self._max_tokens, prompt, ctx),
        )
        return self.result


class TimeoutFuture:
    """模拟 future.result 规划超时。"""

    def result(self, timeout: float) -> Any:
        """始终抛出 FutureTimeoutError。"""
        raise FutureTimeoutError(f"timeout={timeout}")


class TimeoutExecutor:
    """模拟只会超时的 ThreadPoolExecutor。"""

    def __init__(self, max_workers: int):
        """保存工作线程数量。"""
        self.max_workers = max_workers

    def __enter__(self) -> "TimeoutExecutor":
        """返回上下文管理器自身。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """不吞掉上下文异常。"""
        return False

    def submit(self, func: Any, *args: Any, **kwargs: Any) -> TimeoutFuture:
        """返回固定超时 future，不执行目标函数。"""
        return TimeoutFuture()


def test_collect_tools_filters_non_invocable_and_builds_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具收集应过滤不可调用 Skill，并把参数、意图和消息写入 prompt。"""
    FakeSkillRegistry.skills = [FakeSkill("demo.run"), FakeSkill("demo.hidden", invocable=False)]
    monkeypatch.setattr(SkillRegistry, "instance", FakeSkillRegistry.instance)
    planner = ChatPlanner(SimpleNamespace())

    tools = planner._collect_tools()
    prompt = planner._build_plan_prompt(
        {"intent": "start_review", "reason": "requested", "payload": {"project_id": 7}},
        tools,
        "请审查项目",
    )

    names = {item["name"] for item in tools}
    assert "demo.run" in names
    assert "demo.hidden" not in names
    assert "start_review" in names
    assert "demo.run(value)" in prompt
    assert '"project_id": 7' in prompt
    assert "请审查项目" in prompt
    assert "不得使用 $... 等动态引用语法" in prompt
    assert "不要先调用 list_code_files" in prompt


def test_collect_tools_degrades_to_fixed_tools_when_registry_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """SkillRegistry 故障时仍应返回 Orchestrator 固定工具。"""
    monkeypatch.setattr(SkillRegistry, "instance", FailingSkillRegistry.instance)

    tools = ChatPlanner(SimpleNamespace())._collect_tools()

    assert len(tools) == len(ChatPlanner._FIXED_TOOLS)
    assert all(item["type"] == "fixed" for item in tools)


def test_call_llm_for_plan_restores_agent_settings_on_success() -> None:
    """规划成功后应恢复 ChatAgent 原有 system prompt 和采样参数。"""
    agent = FakePlanningAgent(AgentResult(success=True, data=[{"tool_name": "list_projects"}]))
    planner = ChatPlanner(agent)
    ctx = SimpleNamespace(user_id=3)

    raw = planner._call_llm_for_plan("plan prompt", ctx)

    assert raw == [{"tool_name": "list_projects"}]
    assert agent.snapshots[0][:3] == (
        "你是 PRISM 平台的调用链规划器,只输出 JSON 数组,不要额外文本。",
        0.1,
        1024,
    )
    assert agent._system_prompt == "original prompt"
    assert agent._temperature == 0.8
    assert agent._max_tokens == 256


@pytest.mark.parametrize(
    "result",
    [
        AgentResult(success=False, error="model failed"),
        AgentResult(success=True, data="not-json"),
    ],
)
def test_call_llm_for_plan_returns_none_for_failure_or_non_json(result: AgentResult) -> None:
    """LLM 失败或返回非 dict/list 时应降级为空结果并恢复设置。"""
    agent = FakePlanningAgent(result)
    planner = ChatPlanner(agent)

    assert planner._call_llm_for_plan("prompt", None) is None
    assert agent._system_prompt == "original prompt"
    assert agent._temperature == 0.8
    assert agent._max_tokens == 256


@pytest.mark.parametrize(
    ("raw", "expected_name", "expected_args"),
    [
        ([{"tool_name": "list_projects", "arguments": {"page": 2}}], "list_projects", {"page": 2}),
        ({"plan": [{"name": "list_rules", "args": {"enabled": True}}]}, "list_rules", {"enabled": True}),
        ({"steps": [{"tool_name": "dashboard_summary"}]}, "dashboard_summary", {}),
        ({"tool_name": "list_reports", "reason": "single"}, "list_reports", {}),
    ],
)
def test_parse_plan_accepts_supported_shapes(raw: Any, expected_name: str, expected_args: dict[str, Any]) -> None:
    """解析器应兼容数组、plan/steps 包装和单步对象。"""
    plan = ChatPlanner(SimpleNamespace())._parse_plan(raw)

    assert plan[0].tool_name == expected_name
    assert plan[0].arguments == expected_args


@pytest.mark.parametrize("raw", ["invalid", {}, {"plan": []}, ["bad", {}]])
def test_parse_plan_rejects_invalid_or_empty_shapes(raw: Any) -> None:
    """非法类型、空链和缺少工具名的条目应抛 ValueError。"""
    with pytest.raises(ValueError):
        ChatPlanner(SimpleNamespace())._parse_plan(raw)


def test_validate_plan_rejects_unknown_tool_and_accepts_long_chain() -> None:
    """白名单校验应拒绝未知工具，但长链仅告警并由 plan 截断。"""
    planner = ChatPlanner(SimpleNamespace())
    tools = [{"name": "list_projects"}]

    with pytest.raises(ValueError, match="非法 tool_name"):
        planner._validate_plan([ToolCall("drop_database")], tools)

    planner._validate_plan([ToolCall("list_projects") for _ in range(planner.MAX_STEPS + 1)], tools)


def test_fixed_tool_schema_and_validation_share_contract_source() -> None:
    """Planner 应展示真实 required/extra Schema，并规范化固定工具兼容别名。"""
    planner = ChatPlanner(SimpleNamespace())
    tools = planner._collect_tools()
    start_review = next(item for item in tools if item["name"] == "start_review")
    plan = [ToolCall("list_projects", {"project_query": "legacy"})]

    planner._validate_plan(plan, tools)

    assert set(start_review["parameters"]["required"]) == {"project_id"}
    assert start_review["parameters"]["additionalProperties"] is False
    assert "ctx" not in start_review["parameters"]["properties"]
    assert "user" not in start_review["parameters"]["properties"]
    assert plan[0].arguments == {"keyword": "legacy"}

    with pytest.raises(ValueError, match="参数不合法"):
        planner._validate_plan([ToolCall("delete_project", {"project_id": "1"})], tools)


def test_start_review_replaces_planner_file_reference_with_server_resolution() -> None:
    """Planner 动态文件引用应交由 Orchestrator 解析项目 active 文件。"""
    planner = ChatPlanner(SimpleNamespace())
    tools = planner._collect_tools()
    plan = [
        ToolCall(
            "start_review",
            {
                "project_id": 27,
                "file_ids": "$[0].files[].id",
                "review_type": "quick",
                "task_name": "chat-auto-file-e2e",
            },
        ),
    ]

    planner._validate_plan(plan, tools)

    assert plan[0].arguments == {
        "project_id": 27,
        "review_type": "quick",
        "task_name": "chat-auto-file-e2e",
    }

    with pytest.raises(ValueError, match="参数不合法"):
        planner._validate_plan(
            [ToolCall("start_review", {"project_id": 27, "file_ids": "all"})],
            tools,
        )


def test_plan_runs_llm_validates_and_truncates_to_max_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整规划流程应校验工具并把超过上限的调用链截断。"""
    planner = ChatPlanner(SimpleNamespace())
    tools = [{"name": "list_projects", "description": "list", "parameters": {}}]
    raw = [{"tool_name": "list_projects", "arguments": {"page": index}} for index in range(7)]

    def _collect_tools() -> list[dict[str, Any]]:
        """返回单一白名单工具。"""
        return tools

    def _call_plan(prompt: str, ctx: Any) -> list[dict[str, Any]]:
        """返回七步有效调用链。"""
        assert "list_projects" in prompt
        return raw

    monkeypatch.setattr(planner, "_collect_tools", _collect_tools)
    monkeypatch.setattr(planner, "_call_llm_for_plan", _call_plan)

    plan = planner.plan({"intent": "list_projects", "payload": {}}, "列出项目")

    assert len(plan) == planner.MAX_STEPS
    assert [step.arguments["page"] for step in plan] == list(range(planner.MAX_STEPS))


def test_plan_rejects_empty_llm_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 返回 None 时完整规划流程应抛出明确空结果异常。"""
    planner = ChatPlanner(SimpleNamespace())

    def _collect_tools() -> list[dict[str, Any]]:
        """返回最小工具白名单。"""
        return [{"name": "list_projects", "description": "list", "parameters": {}}]

    def _call_plan(prompt: str, ctx: Any) -> None:
        """模拟 LLM 无结果。"""
        return None

    monkeypatch.setattr(planner, "_collect_tools", _collect_tools)
    monkeypatch.setattr(planner, "_call_llm_for_plan", _call_plan)

    with pytest.raises(ValueError, match="返回空结果"):
        planner.plan({"intent": "list_projects"})


def test_plan_translates_future_timeout_to_builtin_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程池 FutureTimeoutError 应转换为调用方可降级处理的 TimeoutError。"""
    planner = ChatPlanner(SimpleNamespace())

    def _collect_tools() -> list[dict[str, Any]]:
        """返回最小工具白名单。"""
        return [{"name": "list_projects", "description": "list", "parameters": {}}]

    monkeypatch.setattr(planner, "_collect_tools", _collect_tools)
    monkeypatch.setattr(planner_module, "ThreadPoolExecutor", TimeoutExecutor)

    with pytest.raises(TimeoutError, match="规划超时"):
        planner.plan({"intent": "list_projects"})
