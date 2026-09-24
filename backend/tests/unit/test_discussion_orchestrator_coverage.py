"""圆桌讨论编排器隔离覆盖率测试。

用例通过内存数据库、记录型 fake 与 monkeypatch 隔离 LLM、事件总线、
MetaGPT Environment、WebSocket 讨论总线和真实等待，不访问任何外部服务。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from app.agents.discussion_bus import DiscussionBus
from app.agents.events import AgentEvent, AgentEventType, DiscussionTurn
from app.ai import discussion_orchestrator as module
from app.ai.deepseek_agent import DeepSeekOutputTruncatedError
from app.ai.discussion_orchestrator import DiscussionOrchestrator
from app.ai.multi_agent import GENERAL_AGENT, SECURITY_AGENT
from app.ai.result_parser import Issue
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile

_REAL_CALL_FOR_TASK = module._call_raw_for_task


class RecordingAgent:
    """按顺序返回预设结果并记录 LLM 调用参数的 fake Agent。"""

    def __init__(
        self,
        responses: Optional[list[tuple[str, Optional[dict[str, Any]]]]] = None,
        error: Optional[Exception] = None,
        model: str = "fake-model",
    ) -> None:
        """初始化记录型 Agent。

        Args:
            responses: 每次调用依次返回的文本与元数据。
            error: 非空时每次调用抛出的异常。
            model: 供报告日志标记使用的模型名。

        Returns:
            None: 初始化响应队列和调用记录。
        """
        self.responses = list(responses or [])
        self.error = error
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def call_raw(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        agent_label: str,
        json_mode: bool,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """记录调用并返回下一项预设结果。

        Args:
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。
            agent_label: 调用归属 Agent 标签。
            json_mode: 是否要求 JSON 输出。

        Returns:
            tuple[str, Optional[dict[str, Any]]]: 预设的文本与日志元数据。

        Raises:
            Exception: 构造 fake 时指定的异常。
            AssertionError: 响应队列已耗尽。
        """
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "agent_label": agent_label,
            "json_mode": json_mode,
            "max_tokens": max_tokens,
        })
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("RecordingAgent 响应队列已耗尽")
        return self.responses.pop(0)


class RecordingEnvironment:
    """记录 publish 消息并提供角色列表的 fake Environment。"""

    def __init__(self, roles: Optional[list[str]] = None, fail_publish: bool = False) -> None:
        """初始化 fake Environment。

        Args:
            roles: list_roles 返回的角色编码。
            fail_publish: 是否在 publish 时模拟异常。

        Returns:
            None: 初始化消息记录与异常开关。
        """
        self.roles = roles or ["code_reviewer"]
        self.fail_publish = fail_publish
        self.messages: list[Any] = []

    def list_roles(self) -> list[str]:
        """返回环境中的角色编码。

        Returns:
            list[str]: 预设角色列表的副本。
        """
        return list(self.roles)

    def publish(self, message: Any) -> None:
        """记录消息或模拟发布失败。

        Args:
            message: 待发布的 MetaGPT 消息。

        Returns:
            None: 消息仅保存在内存中。

        Raises:
            RuntimeError: fail_publish 为 True 时模拟总线异常。
        """
        self.messages.append(message)
        if self.fail_publish:
            raise RuntimeError("environment unavailable")


class RecordingEventBus:
    """记录 AgentEvent 的 fake 事件总线。"""

    def __init__(self, fail_publish: bool = False) -> None:
        """初始化事件记录器。

        Args:
            fail_publish: 是否在 publish 时模拟异常。

        Returns:
            None: 初始化事件列表。
        """
        self.fail_publish = fail_publish
        self.events: list[AgentEvent] = []

    def publish(self, event: AgentEvent) -> None:
        """记录事件或模拟事件总线异常。

        Args:
            event: 待广播的 Agent 事件。

        Returns:
            None: 事件仅保存在内存中。

        Raises:
            RuntimeError: fail_publish 为 True 时模拟广播失败。
        """
        self.events.append(event)
        if self.fail_publish:
            raise RuntimeError("event bus unavailable")


class EventBusProvider:
    """向被测模块提供可替换 RecordingEventBus 的类级入口。"""

    current: Optional[RecordingEventBus] = None

    @classmethod
    def instance(cls) -> RecordingEventBus:
        """返回当前测试绑定的事件总线。

        Returns:
            RecordingEventBus: 当前测试记录器。

        Raises:
            AssertionError: 测试未预先绑定记录器。
        """
        if cls.current is None:
            raise AssertionError("EventBusProvider.current 未设置")
        return cls.current


class RecordingControlBus:
    """记录暂停、恢复与终止控制消息的 fake 讨论总线。"""

    def __init__(self) -> None:
        """初始化控制消息与终止请求记录。

        Returns:
            None: 创建空记录列表。
        """
        self.controls: list[tuple[str, str, dict[str, Any]]] = []
        self.stop_requests: list[str] = []

    def publish_control(self, session_id: str, action: str, payload: dict[str, Any]) -> None:
        """记录一条讨论控制消息。

        Args:
            session_id: 讨论会话 ID。
            action: 控制动作。
            payload: 控制载荷。

        Returns:
            None: 控制消息仅保存在内存中。
        """
        self.controls.append((session_id, action, payload))

    def request_stop(self, session_id: str) -> None:
        """记录终止会话请求。

        Args:
            session_id: 讨论会话 ID。

        Returns:
            None: 会话 ID 仅保存在内存中。
        """
        self.stop_requests.append(session_id)


def _make_orchestrator(bus: Optional[Any] = None) -> DiscussionOrchestrator:
    """构造不共享单例状态的编排器实例。

    Args:
        bus: 可选的讨论总线 fake。

    Returns:
        DiscussionOrchestrator: 已初始化全部运行态字段的实例。
    """
    orchestrator = DiscussionOrchestrator.__new__(DiscussionOrchestrator)
    orchestrator._bus = bus or RecordingControlBus()
    orchestrator._env = None
    orchestrator._session_id = "session-1"
    orchestrator._paused = False
    orchestrator._paused_event = None
    orchestrator._user_inputs = []
    orchestrator._all_turns = []
    orchestrator._task_id = 0
    orchestrator._user_id = 0
    orchestrator._file_id = 0
    orchestrator._trace_id = "trace-test"
    return orchestrator


def _turn(
    turn_id: int,
    *,
    role: str = "agent",
    agent_code: str = "security",
    agent_name: str = "安全代理",
    content: str = "发现问题",
) -> DiscussionTurn:
    """创建简洁的讨论发言测试数据。

    Args:
        turn_id: 发言序号。
        role: 发言角色，agent 或 user。
        agent_code: Agent 机器编码。
        agent_name: Agent 展示名称。
        content: 发言正文。

    Returns:
        DiscussionTurn: 可直接传给编排器的发言对象。
    """
    return DiscussionTurn(
        turn_id=turn_id,
        agent_code=agent_code,
        agent_name=agent_name,
        role=role,
        content=content,
    )


def test_init_uses_discussion_bus_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """构造编排器时应绑定 DiscussionBus 单例。

    Args:
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言构造器保存了单例结果。
    """
    expected_bus = RecordingControlBus()

    def get_instance(_cls: type[DiscussionBus]) -> RecordingControlBus:
        """返回当前测试专用讨论总线。

        Args:
            _cls: 被替换的 DiscussionBus 类。

        Returns:
            RecordingControlBus: 测试专用总线。
        """
        return expected_bus

    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(get_instance))

    orchestrator = DiscussionOrchestrator()

    assert orchestrator._bus is expected_bus


def test_build_history_formats_empty_user_and_agent_turns() -> None:
    """历史构造应区分首轮、用户发言与 Agent 发言标签。

    Returns:
        None: 断言讨论历史文本格式与顺序。
    """
    orchestrator = _make_orchestrator()

    assert "第一位发言者" in orchestrator._build_history([])

    history = orchestrator._build_history([
        _turn(1, role="user", agent_code="user", agent_name="用户", content="请看边界"),
        _turn(2, content="第 8 行存在越界"),
    ])

    assert history.startswith("## 讨论记录 (按发言时间顺序)")
    assert "### 用户\n请看边界" in history
    assert "### 安全代理(security) [动作:speak; 立场:neutral]" in history
    assert "第 8 行存在越界" in history
    assert history.index("请看边界") < history.index("第 8 行存在越界")


def test_parse_speaker_decision_supports_speech_silence_and_plain_text_fallback() -> None:
    """决策解析应保留结构化立场，并兼容旧模型的纯文本响应。

    Returns:
        None: 断言发言、静音和降级决策的规范化结果。
    """
    speaking = module._parse_speaker_decision(
        '{"action":"speak","stance":"oppose","reply_to":"security",'
        '"content":"我否认该严重度判断，因为输入已在第 8 行校验。"}',
    )
    assert speaking.action == "speak"
    assert speaking.stance == "oppose"
    assert speaking.reply_to == "security"
    assert "否认" in speaking.content

    silent = module._parse_speaker_decision(
        '{"action":"silent","stance":"agree","reply_to":"general",'
        '"content":"现有观点已覆盖本领域，没有新增证据。"}',
    )
    assert silent.action == "silent"
    assert silent.stance == "neutral"
    assert silent.reply_to is None
    assert "没有新增证据" in silent.content

    fallback = module._parse_speaker_decision("我补充第 12 行存在资源泄漏。")
    assert fallback.action == "speak"
    assert fallback.stance == "neutral"
    assert fallback.content == "我补充第 12 行存在资源泄漏。"


def test_discussion_turn_serializes_decision_metadata() -> None:
    """讨论帧应携带自主决策、立场、回应对象和轮次。

    Returns:
        None: 断言 WebSocket 序列化契约字段。
    """
    turn = DiscussionTurn(
        turn_id=3,
        agent_code="reliability",
        agent_name="可靠性代理",
        role="agent",
        content="同意边界条件问题。",
        action="speak",
        stance="agree",
        reply_to="security",
        round_index=2,
    )

    payload = turn.to_dict()
    assert payload["action"] == "speak"
    assert payload["stance"] == "agree"
    assert payload["reply_to"] == "security"
    assert payload["round_index"] == 2


@pytest.mark.asyncio
async def test_speaker_turn_builds_context_and_returns_trimmed_content() -> None:
    """Agent 发言应包含完整历史与全部用户指示并去除首尾空白。

    Returns:
        None: 断言提示词、Agent 标签、文本与元数据。
    """
    orchestrator = _make_orchestrator()
    meta = {"model_name": "fake-model", "tokens": 12}
    agent = RecordingAgent(responses=[("  我同意并补充第 9 行问题。  ", meta)])

    decision, returned_meta, ok = await orchestrator._speaker_turn(
        agent=agent,
        profile=SECURITY_AGENT,
        code="print(user_input)",
        language="python",
        file_name="demo.py",
        all_turns=[_turn(1, content="第 3 行需要校验")],
        user_inputs=["忽略旧指示", "检查输入", "关注权限", "给出行号"],
        round_idx=1,
        speaker_idx=0,
    )

    assert decision.content == "我同意并补充第 9 行问题。"
    assert decision.action == "speak"
    assert decision.stance == "neutral"
    assert returned_meta == meta
    assert ok is True
    call = agent.calls[0]
    assert call["agent_label"] == "security"
    assert call["json_mode"] is True
    assert "安全审查代理" in call["system_prompt"]
    assert "第 3 行需要校验" in call["user_prompt"]
    assert all(text in call["user_prompt"] for text in ("忽略旧指示", "检查输入", "关注权限", "给出行号"))
    assert "选择发言或静音" in call["user_prompt"]


@pytest.mark.asyncio
async def test_speaker_turn_returns_structured_silence_decision() -> None:
    """Agent 应能自主选择静音，并通过 JSON 模式返回结构化决策。

    Returns:
        None: 断言静音动作、调用模式和共享历史上下文。
    """
    orchestrator = _make_orchestrator()
    agent = RecordingAgent(responses=[(
        '{"action":"silent","stance":"neutral","reply_to":null,'
        '"content":"已有发言完整覆盖我的检查范围。"}',
        {"model_name": "fake-model"},
    )])

    decision, _, ok = await orchestrator._speaker_turn(
        agent=agent,
        profile=SECURITY_AGENT,
        code="safe_call()",
        language="python",
        file_name="safe.py",
        all_turns=[_turn(1, content="已检查输入校验")],
        user_inputs=[],
        round_idx=1,
        speaker_idx=1,
    )

    assert ok is True
    assert decision.action == "silent"
    assert decision.content == "已有发言完整覆盖我的检查范围。"
    assert agent.calls[0]["json_mode"] is True
    assert "发言或静音" in agent.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_speaker_turn_handles_first_speaker_and_llm_failure() -> None:
    """首位发言者应获得首轮提示，LLM 异常应转换为可展示失败文本。

    Returns:
        None: 断言首轮识别与异常降级三元组。
    """
    orchestrator = _make_orchestrator()
    first_agent = RecordingAgent(responses=[("首轮发现", None)])

    first_result = await orchestrator._speaker_turn(
        agent=first_agent,
        profile=GENERAL_AGENT,
        code="value = 1",
        language="python",
        file_name="first.py",
        all_turns=[_turn(1, role="user", agent_code="user", agent_name="用户")],
        user_inputs=[],
        round_idx=0,
        speaker_idx=0,
    )

    first_decision, first_meta, first_ok = first_result
    assert first_decision.content == "首轮发现"
    assert first_decision.action == "speak"
    assert first_meta is None
    assert first_ok is True
    assert "第一位有效发言者" in first_agent.calls[0]["user_prompt"]

    failing_agent = RecordingAgent(error=RuntimeError("upstream timeout"))
    failed_decision, failed_meta, failed_ok = await orchestrator._speaker_turn(
        agent=failing_agent,
        profile=GENERAL_AGENT,
        code="value = 1",
        language="python",
        file_name="first.py",
        all_turns=[],
        user_inputs=[],
        round_idx=0,
        speaker_idx=0,
    )

    assert "upstream timeout" in failed_decision.content
    assert failed_decision.action == "speak"
    assert failed_meta is None
    assert failed_ok is False


@pytest.mark.asyncio
async def test_speaker_retries_length_with_complete_context_and_counts_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产中 4096 截断的专家发言应逐级扩容，且每次沿用完整源码与历史。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr(module, "_call_raw_for_task", _REAL_CALL_FOR_TASK)

    class RetryingAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if len(self.calls) < 3:
                raise DeepSeekOutputTruncatedError(
                    "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
                )
            return (
                '{"action":"speak","stance":"supplement","content":"第 7 行缺少校验"}',
                {"model_name": "speaker-model"},
            )

    budget = module._RoundtableCallBudget()
    token = module._roundtable_call_budget.set(budget)
    agent = RetryingAgent()
    try:
        decision, meta, ok = await _make_orchestrator()._speaker_turn(
            agent=agent, profile=SECURITY_AGENT,
            code="source_marker = untrusted_input", language="python", file_name="source.py",
            all_turns=[_turn(1, content="历史证据标记")],
            user_inputs=["请检查账号隔离"], round_idx=1, speaker_idx=1,
        )
    finally:
        module._roundtable_call_budget.reset(token)

    assert ok and decision.content == "第 7 行缺少校验"
    assert meta == {"model_name": "speaker-model"}
    assert budget.used == 3
    assert [call["max_tokens"] for call in agent.calls] == [8192, 16_384, 32_768]
    assert len({call["user_prompt"] for call in agent.calls}) == 1
    assert all(
        "source_marker = untrusted_input" in call["user_prompt"]
        and "历史证据标记" in call["user_prompt"]
        and "请检查账号隔离" in call["user_prompt"]
        for call in agent.calls
    )


@pytest.mark.asyncio
async def test_speaker_exhausted_length_retries_remains_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三个输出预算都截断时不能把半截 JSON 算成有效专家发言。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr(module, "_call_raw_for_task", _REAL_CALL_FOR_TASK)

    class TruncatedAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            raise DeepSeekOutputTruncatedError(
                "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
            )

    budget = module._RoundtableCallBudget()
    token = module._roundtable_call_budget.set(budget)
    agent = TruncatedAgent()
    try:
        decision, meta, ok = await _make_orchestrator()._speaker_turn(
            agent=agent, profile=SECURITY_AGENT, code="x = 1", language="python",
            file_name="partial.py", all_turns=[], user_inputs=[], round_idx=0, speaker_idx=0,
        )
    finally:
        module._roundtable_call_budget.reset(token)

    assert not ok and meta is None
    assert "finish_reason=length" in decision.content
    assert budget.used == 3
    assert [call["max_tokens"] for call in agent.calls] == [8192, 16_384, 32_768]


def test_summarize_handles_empty_success_and_fallback() -> None:
    """主持人汇总应覆盖无发言、成功输出和空输出统计回退。

    Returns:
        None: 断言终止前缀、正文截断、统计分组与长文本省略号。
    """
    orchestrator = _make_orchestrator()
    user_only = [_turn(1, role="user", agent_code="user", agent_name="用户")]

    empty_summary, empty_meta = orchestrator._summarize(
        user_only,
        "x = 1",
        "python",
        "empty.py",
        RecordingAgent(),
        stopped=True,
    )
    assert empty_summary.startswith("🛑 讨论已被用户终止。")
    assert "没有产生有效发言" in empty_summary
    assert empty_meta is None

    meta = {"model_name": "summary-model"}
    success_agent = RecordingAgent(responses=[("  " + "结" * 2100 + "  ", meta)])
    summary, returned_meta = orchestrator._summarize(
        [_turn(1, content="问题一")],
        "x = 1",
        "python",
        "success.py",
        success_agent,
    )
    assert summary.startswith("📋 **讨论共识小结**")
    assert summary.endswith("结" * 2100)
    assert returned_meta == meta
    assert success_agent.calls[0]["agent_label"] == "general"
    assert success_agent.calls[0]["max_tokens"] > 4096

    fallback_agent = RecordingAgent(responses=[("   ", {"ignored": True})])
    fallback, fallback_meta = orchestrator._summarize(
        [
            _turn(1, agent_name="质量代理", content="A" * 120),
            _turn(2, agent_name="质量代理", content="短结论"),
        ],
        "x = 1",
        "python",
        "fallback.py",
        fallback_agent,
    )
    assert "共 2 条发言" in fallback
    assert fallback.count("**质量代理**:") == 1
    assert "A" * 120 in fallback
    assert "  · 短结论" in fallback
    assert fallback_meta is None


def test_summarize_retries_length_without_losing_full_result() -> None:
    """主持总结不能把 length 终态或固定 2000 字切片当成完整结果。"""
    class BudgetAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if (kwargs.get("max_tokens") or 4096) < 32_768:
                raise DeepSeekOutputTruncatedError(
                    "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
                )
            return "结" * 2200, {"model_name": "summary-model"}

    orchestrator = _make_orchestrator()
    agent = BudgetAgent()
    summary, meta = orchestrator._summarize(
        [_turn(1, content="第 7 行有证据")], "x = 1", "python", "summary.py", agent,
    )
    assert summary.endswith("结" * 2200)
    assert meta is not None
    assert [call["max_tokens"] for call in agent.calls] == [16_384, 32_768]


def test_host_projects_complete_oversized_source_before_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """专家分窗已完成时，主持不应因再次塞入完整大源码而只能回退。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 12_000)

    class HostAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if "历史压缩器" in kwargs["system_prompt"]:
                entries = []
                for source_id, body in re.findall(
                    r"【来源 ([^】]+)】\n(.*?)(?=\n\n【来源 |\Z)",
                    kwargs["user_prompt"], re.S,
                ):
                    prior_quote = re.search(r"「([^」]+)」", body)
                    entries.append({
                        "source_id": source_id,
                        "summary": "此处源码已覆盖",
                        "quotes": [prior_quote.group(1) if prior_quote else body[:8]],
                    })
                return json.dumps({"entries": entries}, ensure_ascii=False), {"model_name": "compress"}
            assert "完整源码窗口的证据投影" in kwargs["user_prompt"]
            return "第 3000 行的发现需要优先修复。", {"model_name": "host"}

    code = "\n".join(f"line_{index:04d}()" for index in range(1, 3001))
    agent = HostAgent()
    summary, meta = _make_orchestrator()._summarize(
        [_turn(1, content="第 3000 行有边界问题")], code, "python", "large.py", agent,
    )
    assert meta is not None and "第 3000 行" in summary
    source_prompts = [call["user_prompt"] for call in agent.calls
                      if "历史压缩器" in call["system_prompt"]]
    assert source_prompts
    assert "line_0001()" in "\n".join(source_prompts)
    assert "line_3000()" in "\n".join(source_prompts)
    host_prompt = agent.calls[-1]["user_prompt"]
    source_ids = set(re.findall(r"【来源 (C\d{4})", host_prompt))
    assert source_ids
    assert source_ids == {f"C{index:04d}" for index in range(1, len(source_ids) + 1)}


def test_summarize_falls_back_when_llm_raises() -> None:
    """主持人 LLM 调用异常时应返回本地统计而不传播异常。

    Returns:
        None: 断言异常降级文本与空元数据。
    """
    orchestrator = _make_orchestrator()
    summary, meta = orchestrator._summarize(
        [_turn(1, agent_name="可靠性代理", content="需要补充异常处理")],
        "raise ValueError",
        "python",
        "error.py",
        RecordingAgent(error=RuntimeError("summary failed")),
        stopped=True,
    )

    assert summary.startswith("🛑 讨论已被用户终止。")
    assert "可靠性代理" in summary
    assert meta is None


def test_summarize_receives_user_input_and_structured_stances() -> None:
    """主持 Agent 的总结上下文应包含用户插话和 Agent 结构化立场。

    Returns:
        None: 断言共享群聊记录完整传给总结模型。
    """
    orchestrator = _make_orchestrator()
    agent = RecordingAgent(responses=[("已汇总", None)])
    turns = [
        _turn(1, role="user", agent_code="user", agent_name="你", content="请优先确认权限风险"),
        DiscussionTurn(
            turn_id=2,
            agent_code="security",
            agent_name="安全代理",
            role="agent",
            content="我否认当前权限校验充分。",
            action="speak",
            stance="oppose",
            reply_to="general",
            round_index=1,
        ),
    ]

    summary, _ = orchestrator._summarize(
        turns, "check(user)", "python", "auth.py", agent,
    )

    assert summary.endswith("已汇总")
    prompt = agent.calls[0]["user_prompt"]
    assert "请优先确认权限风险" in prompt
    assert "动作:speak; 立场:oppose; 回应:general" in prompt
    assert "尚未解决的分歧" in agent.calls[0]["system_prompt"]


def test_extract_issues_skips_sessions_with_only_silent_agents() -> None:
    """只有静音决策时不应调用问题抽取模型或生成虚假问题。

    Returns:
        None: 断言静音记录被问题抽取入口过滤。
    """
    silent_turn = DiscussionTurn(
        turn_id=1,
        agent_code="performance",
        agent_name="性能代理",
        role="agent",
        content="没有新增性能证据。",
        action="silent",
        stance="neutral",
    )
    agent = RecordingAgent()

    issues = module._extract_issues(
        [silent_turn], "pass", "python", "quiet.py", agent, None, 1, 2, 3,
    )

    assert issues == []
    assert agent.calls == []


def test_publish_to_environment_sets_metadata_and_swallows_failures() -> None:
    """Environment 发布应携带上下文、覆盖 cause_by 并静默处理失败。

    Returns:
        None: 断言空内容跳过、正常消息字段与异常隔离。
    """
    orchestrator = _make_orchestrator()
    orchestrator._user_id = 42
    orchestrator._file_id = 9
    orchestrator._trace_id = "trace-env"
    environment = RecordingEnvironment()
    orchestrator._env = environment

    orchestrator._publish_to_env("security", "", turn_id=1)
    assert environment.messages == []

    orchestrator._publish_to_env(
        speaker="security",
        content="发现越权",
        turn_id=3,
        cause_by="DiscussionSummary",
    )
    message = environment.messages[0]
    assert message.role == "security"
    assert message.content == "发现越权"
    assert message.cause_by == "DiscussionSummary"
    assert message.metadata == {
        "user_id": 42,
        "project_id": None,
        "file_id": 9,
        "trace_id": "trace-env",
        "turn_id": 3,
    }

    orchestrator._env = RecordingEnvironment(fail_publish=True)
    orchestrator._publish_to_env("security", "仍需继续", turn_id=4)


def test_emit_builds_user_scoped_event_and_swallows_bus_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """讨论状态事件应按用户隔离并在事件总线失败时静默降级。

    Args:
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言事件主体、载荷、用户归属和异常隔离。
    """
    monkeypatch.setattr(module, "AgentEventBus", EventBusProvider)
    event_bus = RecordingEventBus()
    EventBusProvider.current = event_bus
    orchestrator = _make_orchestrator()
    orchestrator._trace_id = "trace-event"
    orchestrator._task_id = 88
    orchestrator._user_id = 42

    orchestrator._emit(AgentEventType.THINKING, "security_sentinel", "正在分析")

    event = event_bus.events[0]
    assert event.type == AgentEventType.THINKING
    assert event.agent == "security_sentinel"
    assert event.trace_id == "trace-event"
    assert event.message == "正在分析"
    assert event.payload == {"task_id": 88, "user_id": 42, "source": "discussion"}
    assert event.user_id == 42

    EventBusProvider.current = RecordingEventBus(fail_publish=True)
    orchestrator._emit(AgentEventType.FAILED, "security_sentinel", "失败")


@pytest.mark.asyncio
async def test_handle_control_routes_user_pause_resume_and_stop() -> None:
    """控制回调应更新状态、唤醒等待者并向讨论总线发送反馈。

    Returns:
        None: 断言用户输入、暂停、恢复和终止四类状态转换。
    """
    control_bus = RecordingControlBus()
    orchestrator = _make_orchestrator(control_bus)
    orchestrator._env = RecordingEnvironment()
    orchestrator._user_id = 5
    orchestrator._file_id = 8
    initial_event = asyncio.Event()
    orchestrator._paused_event = initial_event

    orchestrator._handle_control("user_input", {"content": "优先检查权限", "turn_id": 7})
    assert orchestrator._user_inputs == ["优先检查权限"]
    assert len(orchestrator._all_turns) == 1
    assert orchestrator._all_turns[0].role == "user"
    assert orchestrator._all_turns[0].content == "优先检查权限"
    assert initial_event.is_set()
    assert orchestrator._env.messages[0].role == "user"
    assert orchestrator._env.messages[0].metadata["turn_id"] == 7

    orchestrator._handle_control("pause", {})
    paused_event = orchestrator._paused_event
    assert orchestrator._paused is True
    assert paused_event is not None and not paused_event.is_set()

    orchestrator._handle_control("resume", {})
    assert orchestrator._paused is False
    assert paused_event.is_set()

    stop_event = asyncio.Event()
    orchestrator._paused = True
    orchestrator._paused_event = stop_event
    orchestrator._handle_control("stop", {})
    assert orchestrator._paused is False
    assert stop_event.is_set()
    assert control_bus.stop_requests == ["session-1"]
    assert [action for _, action, _ in control_bus.controls] == [
        "paused",
        "resumed",
        "stopping",
    ]


@pytest.mark.asyncio
async def test_wait_if_paused_rearms_event_until_resumed() -> None:
    """暂停等待被误唤醒后应重建事件，并在恢复时正常退出。

    Returns:
        None: 断言 while 分支、事件重建和恢复唤醒行为。
    """
    control_bus = RecordingControlBus()
    orchestrator = _make_orchestrator(control_bus)
    orchestrator._paused = True
    first_event = asyncio.Event()
    orchestrator._paused_event = first_event

    task = asyncio.create_task(orchestrator._wait_if_paused("session-1"))
    await asyncio.sleep(0)
    first_event.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if orchestrator._paused_event is not first_event:
            break

    assert orchestrator._paused_event is not first_event
    orchestrator._handle_control("resume", {})
    await asyncio.wait_for(task, timeout=1)
    await orchestrator._wait_if_paused("session-1")


@pytest.fixture(autouse=True)
def isolate_model_accounting_boundary(monkeypatch):
    # 本文件用 FakeAgent 隔离模型/数据库；真实独立日志事务在来源集成测试覆盖。
    def call(agent, _task, _user, *, usage_file_id=None, usage_chunk_index=None, **kwargs):
        return agent.call_raw(**kwargs)
    monkeypatch.setattr(module, "_call_raw_for_task", call)


def test_create_review_task_persists_task_and_file_link(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """讨论开始应持久化 running 任务、画像快照和文件关联。

    Args:
        db: 内存 SQLite 会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言 ReviewTask 与 ReviewTaskFile 字段。
    """
    def get_session() -> Any:
        """返回当前测试的内存数据库会话。

        Returns:
            Any: Pytest 提供的 SQLAlchemy Session。
        """
        return db

    monkeypatch.setattr(module, "SessionLocal", get_session)
    db.add(CodeFile(
        id=5, project_id=4, file_name="demo.py", file_path="demo.py",
        language="python", content="value = 1\n", version_no=1, is_binary=0, status="active",
    ))
    db.add(CodeVersion(
        file_id=5, version_no=1, content="value = 1\n", create_time=datetime.now(timezone.utc),
    ))
    db.commit()

    task_id = module._create_review_task(
        user_id=3,
        project_id=4,
        file_id=5,
        file_name="demo.py",
        code="value = 1\n",
        language="python",
        review_type="full",
        model_name="fake-model",
        profiles=(GENERAL_AGENT, SECURITY_AGENT),
    )

    task = db.get(ReviewTask, task_id)
    link = db.query(ReviewTaskFile).filter_by(task_id=task_id).one()
    assert task is not None
    assert task.task_name == "demo.py · 圆桌讨论审"
    assert task.review_type == "discuss"
    assert task.status == "running"
    assert task.model_name == "fake-model/discuss"
    assert task.rules_snapshot == [
        {"code": "general", "name": "通用质量代理"},
        {"code": "security", "name": "安全审查代理"},
    ]
    assert link.file_id == 5
    assert link.version_no == 1
    assert len(link.content_sha256) == 64
    assert link.file_snapshot["file_name"] == "demo.py"


def test_extract_issues_parses_json_and_isolates_logging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构化问题抽取应解析有效 JSON，且日志补写失败不影响结果。

    Args:
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言无专家短路、解析字段、JSON 模式和日志异常隔离。
    """
    logged: list[dict[str, Any]] = []

    def fail_log(_db: Any, **kwargs: Any) -> None:
        """记录日志参数后模拟 AiCallLog 写入失败。

        Args:
            _db: 传入的数据库会话占位对象。
            **kwargs: 日志字段。

        Returns:
            None: 记录后抛出异常。

        Raises:
            RuntimeError: 模拟日志持久化失败。
        """
        logged.append(kwargs)
        raise RuntimeError("log unavailable")

    monkeypatch.setattr(module.DeepSeekAgent, "log_deferred", staticmethod(fail_log))
    no_agent_turns = [_turn(0, agent_code="orchestrator", agent_name="主持人")]
    assert module._extract_issues(
        no_agent_turns,
        "x = 1",
        "python",
        "none.py",
        RecordingAgent(),
        object(),
        1,
        2,
        3,
    ) == []

    raw = (
        '{"issues":[{"issue_type":"潜在Bug","severity":"高",'
        '"title":"空值未校验","line_number":7,"description":"可能抛异常",'
        '"suggestion":"增加空值判断"}]}'
    )
    agent = RecordingAgent(responses=[(raw, {"model_name": "extract-model"})])
    turns = [
        _turn(1, content="第 7 行缺少空值校验"),
        _turn(2, role="user", agent_code="user", agent_name="用户", content="请确认严重度"),
    ]

    issues = module._extract_issues(
        turns,
        "value.strip()",
        "python",
        "extract.py",
        agent,
        object(),
        11,
        12,
        13,
    )

    assert len(issues) == 1
    assert issues[0].title == "空值未校验"
    assert issues[0].line_number == 7
    assert issues[0].severity == "高"
    assert agent.calls[0]["json_mode"] is True
    assert "第 7 行缺少空值校验" in agent.calls[0]["user_prompt"]
    assert "请确认严重度" in agent.calls[0]["user_prompt"]
    assert logged[0]["chunk_index"] == 9100
    assert logged[0]["status"] == "success"


def test_extract_issues_propagates_llm_failure() -> None:
    """结构化问题 LLM 调用失败时必须让报告收尾进入失败状态。

    Returns:
        None: 断言异常携带可读阶段和根因。
    """
    with pytest.raises(RuntimeError, match="结构化问题抽取失败.*extract failed"):
        module._extract_issues(
            [_turn(1)],
            "x = 1",
            "python",
            "failure.py",
            RecordingAgent(error=RuntimeError("extract failed")),
            object(),
            1,
            2,
            3,
        )


def test_extract_issues_recovers_from_default_4096_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实模型默认 4096 截断时，抽取阶段须显式提高输出预算。"""
    monkeypatch.setattr(module.DeepSeekAgent, "log_deferred", staticmethod(lambda *_args, **_kwargs: None))

    class BudgetAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if (kwargs.get("max_tokens") or 4096) <= 4096:
                raise DeepSeekOutputTruncatedError(
                    "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
                )
            return '{"issues":[]}', {"model_name": "extract-model"}

    agent = BudgetAgent()
    assert module._extract_issues(
        [_turn(1, content="第 7 行应校验输入")],
        "value.strip()", "python", "extract.py", agent, object(), 11, 12, 13,
    ) == []
    assert agent.calls[0]["max_tokens"] > 4096


def test_extract_issues_splits_many_findings_without_dropping_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使整段输出反复 length，每条不同专家发现也必须被覆盖。"""
    monkeypatch.setattr(module.DeepSeekAgent, "log_deferred", staticmethod(lambda *_args, **_kwargs: None))

    class BoundedAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            prompt = kwargs["user_prompt"]
            present = [
                int(value) for value in re.findall(r"【本批目标[^】]*】发现(\d+)号", prompt)
            ]
            if len(present) > 2:
                raise DeepSeekOutputTruncatedError(
                    "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
                )
            issues = [
                {"issue_type": "潜在Bug", "severity": "中", "title": f"发现{index}号",
                 "line_number": index, "description": f"第{index}行证据", "suggestion": "修复"}
                for index in present
            ]
            return json.dumps({"issues": issues}, ensure_ascii=False), {"model_name": "extract-model"}

    agent = BoundedAgent()
    turns = [_turn(index, content=f"发现{index}号") for index in range(1, 7)]
    issues = module._extract_issues(
        turns, "\n".join(f"line {index}" for index in range(1, 7)),
        "python", "many.py", agent, object(), 11, 12, 13,
    )
    assert {issue.title for issue in issues} == {f"发现{index}号" for index in range(1, 7)}
    assert len(agent.calls) > 1


def test_extract_issues_semantically_compresses_all_long_history_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入超预算时每段都有来源和原文引文，不能直接砍掉旧轮次。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 6000)
    monkeypatch.setattr(module.DeepSeekAgent, "log_deferred", staticmethod(lambda *_args, **_kwargs: None))

    class CompressingAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if "历史压缩器" in kwargs["system_prompt"]:
                entries = []
                for source_id, body in re.findall(
                    r"【来源 ([^】]+)】\n(.*?)(?=\n\n【来源 |\Z)",
                    kwargs["user_prompt"], re.S,
                ):
                    prior_quote = re.search(r"「([^」]+)」", body)
                    quote = prior_quote.group(1) if prior_quote else body[:8]
                    entries.append({
                        "source_id": source_id,
                        "summary": "保留代码证据及异议",
                        "quotes": [quote],
                    })
                return json.dumps({"entries": entries}, ensure_ascii=False), {"model_name": "compress"}
            return '{"issues":[]}', {"model_name": "extract"}

    turns = [
        _turn(index, content=f"发现{index}号：" + chr(64 + index) * 2400)
        for index in range(1, 9)
    ]
    agent = CompressingAgent()
    result = module._extract_issues(
        turns, "value = 1", "python", "long.py", agent, object(), 11, 12, 13,
    )
    assert result == []
    assert any("历史压缩器" in call["system_prompt"] for call in agent.calls)
    final_prompt = [
        call["user_prompt"] for call in agent.calls
        if "代码审查记录员" in call["system_prompt"]
    ][-1]
    for index in range(1, 9):
        assert f"S{index:04d}-T{index}" in final_prompt
    assert all(turn.content.endswith(chr(64 + index) * 2400) for index, turn in enumerate(turns, 1))


def test_history_compression_rejects_unverifiable_quote() -> None:
    """摘要编造引文时必须显式失败，原始记录保持不变。"""
    class InventingAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            return json.dumps({"entries": [{
                "source_id": "S0001-T1", "summary": "虚构事实", "quotes": ["不在原文中的证据"],
            }]}), {"model_name": "compress"}

    records = [("S0001-T1", "【本批目标·审查员#1】真实证据是第 7 行")]
    with pytest.raises(RuntimeError, match="引文无法从原发言核验"):
        module._compress_roundtable_history(
            records, agent=InventingAgent(), task_id=11, user_id=12,
            file_id=13, target_tokens=2000,
        )
    assert records[0][1].endswith("第 7 行")


def test_history_compression_uses_second_level_without_losing_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首层摘要仍超预算时，再压缩并逐项校验原文引文。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 6000)

    class LayeredAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            second_level = "上一层摘要" in kwargs["system_prompt"]
            entries = []
            for source_id, body in re.findall(
                r"【来源 ([^】]+)】\n(.*?)(?=\n\n【来源 |\Z)",
                kwargs["user_prompt"], re.S,
            ):
                prior_quote = re.search(r"「([^」]+)」", body)
                entries.append({
                    "source_id": source_id,
                    "summary": "证" if second_level else "长" * 50,
                    "quotes": [prior_quote.group(1) if prior_quote else body[:8]],
                })
            return json.dumps({"entries": entries}, ensure_ascii=False), {"model_name": "compress"}

    records = [(f"S{index:04d}-T{index}", f"【x】证据{index} " + "A" * 1200)
               for index in range(1, 9)]
    agent = LayeredAgent()
    projected = module._compress_roundtable_history(
        records, agent=agent, task_id=11, user_id=12, file_id=13, target_tokens=400,
    )
    assert any("上一层摘要" in call["system_prompt"] for call in agent.calls)
    for source_id, _content in records:
        assert source_id in projected
    assert "长" * 50 not in projected


def test_code_windows_cover_single_oversized_line_without_dropping_text() -> None:
    """单行超窗口时按同一绝对行号分片，所有源码字符都进入证据窗口。"""
    long_line = "A" * (module._EXTRACTION_CODE_WINDOW_CHARS * 2)
    windows = module._discussion_code_windows("first\n" + long_line + "\nlast")
    assert len(windows) >= 3
    fragments = re.findall(r"2: (A+) \[片段 \d+/\d+\]", "\n".join(windows))
    assert "".join(fragments) == long_line
    assert "1: first" in windows[0]
    assert "3: last" in windows[-1]


@pytest.mark.asyncio
async def test_speaker_and_host_compress_long_history_without_losing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发言和主持两个阶段都能投影长历史，且保留每个来源 ID。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 12000)

    class CompressingAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if "历史压缩器" in kwargs["system_prompt"]:
                entries = []
                for source_id, body in re.findall(
                    r"【来源 ([^】]+)】\n(.*?)(?=\n\n【来源 |\Z)",
                    kwargs["user_prompt"], re.S,
                ):
                    prior_quote = re.search(r"「([^」]+)」", body)
                    entries.append({
                        "source_id": source_id,
                        "summary": "核对该来源",
                        "quotes": [prior_quote.group(1) if prior_quote else body[:8]],
                    })
                return json.dumps({"entries": entries}, ensure_ascii=False), {"model_name": "compress"}
            if "主持人" in kwargs["system_prompt"]:
                return "共识包含全部来源", {"model_name": "host"}
            return '{"action":"speak","stance":"neutral","content":"已核对"}', {"model_name": "speaker"}

    turns = [_turn(index, content=f"发现{index}号：" + chr(64 + index) * 5000)
             for index in range(1, 9)]
    orchestrator = _make_orchestrator()
    agent = CompressingAgent()
    decision, _meta, ok = await orchestrator._speaker_turn(
        agent=agent, profile=SECURITY_AGENT, code="value = 1", language="python",
        file_name="long.py", all_turns=turns, user_inputs=[], round_idx=1, speaker_idx=0,
    )
    assert ok and decision.content == "已核对"
    summary, meta = orchestrator._summarize(turns, "value = 1", "python", "long.py", agent)
    assert "共识包含全部来源" in summary and meta is not None
    final_calls = [call for call in agent.calls if "历史压缩器" not in call["system_prompt"]]
    assert len(final_calls) == 2
    for call in final_calls:
        assert "语义投影" in call["user_prompt"]
        for index in range(1, 9):
            assert f"S{index:04d}-T{index}" in call["user_prompt"]


@pytest.mark.asyncio
async def test_speaker_reviews_full_oversized_source_in_numbered_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码超过单次输入预算时逐窗审查，原始每行都可核对。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 6000)
    class WindowAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if "历史压缩器" in kwargs["system_prompt"]:
                entries = []
                for source_id, body in re.findall(
                    r"【来源 ([^】]+)】\n(.*?)(?=\n\n【来源 |\Z)",
                    kwargs["user_prompt"], re.S,
                ):
                    entries.append({
                        "source_id": source_id,
                        "summary": "已核对当前源码窗口",
                        "quotes": [body[:8]],
                    })
                return json.dumps({"entries": entries}, ensure_ascii=False), {"model_name": "compress"}
            numbers = re.findall(r"(?m)^(\d+): ", kwargs["user_prompt"])
            assert numbers and "源码窗口" in kwargs["user_prompt"]
            return json.dumps({
                "action": "speak", "stance": "propose",
                "content": f"检查第 {numbers[0]}–{numbers[-1]} 行，有具体证据",
            }, ensure_ascii=False), {"model_name": "speaker"}

    source = "\n".join(f"line_{index:03d}()" for index in range(1, 301))
    agent = WindowAgent()
    decision, meta, ok = await _make_orchestrator()._speaker_turn(
        agent=agent, profile=SECURITY_AGENT, code=source,
        language="python", file_name="oversized.py", all_turns=[],
        user_inputs=[], round_idx=0, speaker_idx=0,
    )
    assert ok and decision.action == "speak"
    assert isinstance(meta, list) and len(meta) > 1
    window_calls = [call for call in agent.calls if "历史压缩器" not in call["system_prompt"]]
    covered = [int(value) for call in window_calls
               for value in re.findall(r"(?m)^(\d+): ", call["user_prompt"])]
    assert covered == list(range(1, 301))
    assert "W0001" in decision.content
    assert f"W{len(window_calls):04d}" in decision.content


@pytest.mark.asyncio
async def test_speaker_rejects_more_than_bounded_windows_without_silent_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超出可审查的窗口数量时明确失败，不能把未读源码计为完成。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 6000)
    agent = RecordingAgent(responses=[('{"action":"silent"}', None)])
    decision, meta, ok = await _make_orchestrator()._speaker_turn(
        agent=agent, profile=SECURITY_AGENT, code="A" * 50_000,
        language="python", file_name="too-large.py", all_turns=[],
        user_inputs=[], round_idx=0, speaker_idx=0,
    )
    assert not ok and meta is None
    assert "超过 64 个上限" in decision.content
    assert agent.calls == []


@pytest.mark.asyncio
async def test_speaker_does_not_claim_complete_when_middle_window_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一源码窗口未完成时，该 Agent 整轮不得被标记为成功。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 6000)

    class FailingWindowAgent(RecordingAgent):
        def call_raw(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.calls.append(kwargs)
            if "源码窗口 2/" in kwargs["user_prompt"]:
                raise RuntimeError("第二窗口模型故障")
            return '{"action":"speak","content":"已核对当前窗口"}', {"model_name": "speaker"}

    agent = FailingWindowAgent()
    decision, meta, ok = await _make_orchestrator()._speaker_turn(
        agent=agent, profile=SECURITY_AGENT,
        code="\n".join(f"line_{index:03d}()" for index in range(1, 301)),
        language="python", file_name="incomplete.py", all_turns=[],
        user_inputs=[], round_idx=0, speaker_idx=0,
    )
    assert not ok and meta is None
    assert "第二窗口模型故障" in decision.content
    assert len(agent.calls) == 2


def test_roundtable_model_call_budget_is_shared_at_all_call_sites() -> None:
    """统一模型入口的会话预算耗尽后，第三次请求不得发给提供商。"""
    budget = module._RoundtableCallBudget(limit=2)
    token = module._roundtable_call_budget.set(budget)
    agent = RecordingAgent(responses=[("第一答", None), ("第二答", None)])
    try:
        for _index in range(2):
            _REAL_CALL_FOR_TASK(
                agent, 0, 0, system_prompt="规则", user_prompt="问题",
                agent_label="general", json_mode=False,
            )
        with pytest.raises(RuntimeError, match="模型调用预算最多 2 次"):
            _REAL_CALL_FOR_TASK(
                agent, 0, 0, system_prompt="规则", user_prompt="问题",
                agent_label="general", json_mode=False,
            )
    finally:
        module._roundtable_call_budget.reset(token)
    assert budget.used == 2
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_speaker_checks_total_session_budget_before_window_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多 Agent 尚未发言时，单个大文件不得占满全部会话调用预算。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 6000)
    budget = module._RoundtableCallBudget(limit=12)
    token = module._roundtable_call_budget.set(budget)
    agent = RecordingAgent(responses=[('{"action":"speak","content":"伪成功"}', None)])
    try:
        decision, meta, ok = await _make_orchestrator()._speaker_turn(
            agent=agent, profile=SECURITY_AGENT,
            code="\n".join(f"line_{index:03d}()" for index in range(1, 301)),
            language="python", file_name="budget.py", all_turns=[],
            user_inputs=[], round_idx=0, speaker_idx=0, remaining_turns=4,
        )
    finally:
        module._roundtable_call_budget.reset(token)
    assert not ok and meta is None
    assert "模型调用预算最多 12 次" in decision.content
    assert budget.used == 0
    assert agent.calls == []


@pytest.mark.asyncio
async def test_default_sized_context_reviews_300_lines_in_one_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使按配置允许的最小 100K 上下文，常见 300 行源码仍只需一次发言调用。"""
    monkeypatch.setattr(module.settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr(module, "_call_raw_for_task", _REAL_CALL_FOR_TASK)
    budget = module._RoundtableCallBudget(limit=module._ROUNDTABLE_MAX_MODEL_CALLS)
    token = module._roundtable_call_budget.set(budget)
    agent = RecordingAgent(responses=[(
        '{"action":"speak","content":"第 7 行有一处证据"}', {"model_name": "speaker"},
    )])
    try:
        decision, _meta, ok = await _make_orchestrator()._speaker_turn(
            agent=agent, profile=SECURITY_AGENT,
            code="\n".join(f"line_{index:03d}()" for index in range(1, 301)),
            language="python", file_name="typical.py", all_turns=[],
            user_inputs=[], round_idx=0, speaker_idx=0, remaining_turns=9,
        )
    finally:
        module._roundtable_call_budget.reset(token)
    assert ok and decision.action == "speak"
    assert budget.used == 1
    assert len(agent.calls) == 1


def test_extract_issues_propagates_invalid_json() -> None:
    """结构化输出无法解析时必须暴露可读根因。"""
    with pytest.raises(RuntimeError, match="结构化问题解析失败.*非合法 JSON"):
        module._extract_issues(
            [_turn(1)],
            "x = 1",
            "python",
            "failure.py",
            RecordingAgent(responses=[("not-json", {"model_name": "fake"})]),
            object(),
            1,
            2,
            3,
        )


def test_finalize_review_persists_issues_statistics_and_log_labels(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告收尾应写入问题、严重度统计、评分、共识和 Agent 日志标签。

    Args:
        db: 内存 SQLite 会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言成功收尾的全部关键持久化字段。
    """
    task = ReviewTask(
        user_id=2,
        project_id=3,
        task_name="待收尾",
        review_type="discuss",
        status="running",
        total_files=1,
        processed_files=0,
    )
    db.add(task)
    db.commit()
    logs: list[dict[str, Any]] = []

    def get_session() -> Any:
        """返回当前测试的内存数据库会话。

        Returns:
            Any: Pytest 提供的 SQLAlchemy Session。
        """
        return db

    def record_log(_db: Any, **kwargs: Any) -> None:
        """记录延迟日志，并让首条失败以覆盖降级分支。

        Args:
            _db: 传入的数据库会话。
            **kwargs: 延迟日志字段。

        Returns:
            None: 日志仅保存在内存中。

        Raises:
            RuntimeError: 首次调用模拟日志写入失败。
        """
        logs.append(kwargs)
        if len(logs) == 1:
            raise RuntimeError("first log failed")

    def extract_issues(
        _turns: list[DiscussionTurn],
        _code: str,
        _language: str,
        _file_name: str,
        _agent: RecordingAgent,
        _db: Any,
        _task_id: int,
        _user_id: int,
        _file_id: int,
        progress_callback: Any = None,
    ) -> list[Issue]:
        """返回两条不同严重度的结构化问题。

        Args:
            _turns: 讨论发言。
            _code: 被审查代码。
            _language: 编程语言。
            _file_name: 文件名。
            _agent: 记录型 Agent。
            _db: 数据库会话。
            _task_id: 审查任务 ID。
            _user_id: 用户 ID。
            _file_id: 文件 ID。

        Returns:
            list[Issue]: 高、中严重度问题各一条。
        """
        return [
            Issue(
                line_number=4,
                end_line=6,
                issue_type="安全漏洞",
                severity="高",
                title="越权风险",
                description="缺少权限检查",
                suggestion="增加鉴权",
                fixed_code="check_permission()",
                owasp="A01:2021-Broken Access Control",
                cwe="CWE-639",
                evidence="return data",
                exploit_scenario="普通用户可读取其他账号数据",
                references=["https://cwe.mitre.org/data/definitions/639.html"],
                confidence=0.92,
                cvss_vector="AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N",
                compliance_mapping={"iso27001": ["A.8.3"]},
                remediation="服务端按资源所有者校验权限",
            ),
            Issue(
                line_number=0,
                issue_type="可维护性",
                severity="中",
                title=None,
                description="函数过长",
                suggestion=None,
                fixed_code=None,
            ),
        ]

    monkeypatch.setattr(module, "SessionLocal", get_session)
    monkeypatch.setattr(module.DeepSeekAgent, "log_deferred", staticmethod(record_log))
    monkeypatch.setattr(module, "_extract_issues", extract_issues)
    consensus = "讨论共识：优先修复越权风险。"

    result = module._finalize_review(
        task_id=task.id,
        user_id=2,
        file_id=10,
        file_name="final.py",
        all_turns=[_turn(1)],
        code="return data",
        language="python",
        deferred_logs=[
            {
                "meta": {"model_name": "model-a"},
                "agent_label": "security",
                "chunk_index": 3,
                "status": "success",
            },
            {
                "meta": {"model_name": "model-b"},
                "agent_label": "general",
            },
        ],
        agent=RecordingAgent(model="fallback-model"),
        stopped=False,
        consensus=consensus,
    )

    saved_task = db.get(ReviewTask, task.id)
    rows = db.query(ReviewIssue).filter_by(task_id=task.id).order_by(ReviewIssue.line_number).all()
    assert result == task.id
    assert len(logs) == 2
    assert logs[0]["meta"]["model_tag"] == "model-a/security-agent"
    assert "model_tag" not in logs[1]["meta"]
    assert len(rows) == 2
    assert rows[0].line_number == 0
    assert rows[0].title == ""
    assert rows[1].line_number == 1
    assert rows[1].end_line == 1
    assert rows[1].fixed_code == "check_permission()"
    assert rows[1].source == "llm"
    assert rows[1].source_details[0]["source"] == "llm:roundtable"
    assert rows[1].confirmation_count == 1
    assert len(rows[1].finding_fingerprint) == 64
    assert rows[1].cwe == "CWE-639"
    assert rows[1].evidence == "return data"
    assert rows[1].cvss_score == pytest.approx(8.1)
    assert rows[1].cvss_version == "3.1"
    assert rows[1].cvss_source == "vector"
    assert rows[1].compliance_mapping == {"iso27001": ["A.8.3"]}
    assert rows[1].remediation == "服务端按资源所有者校验权限"
    assert saved_task is not None
    assert saved_task.processed_files == 1
    assert saved_task.total_issues == 2
    assert saved_task.high_issues == 1
    assert saved_task.medium_issues == 1
    assert saved_task.score == 89
    assert saved_task.score_version == "severity-deduction-v1"
    assert saved_task.score_breakdown["score"] == 89
    assert saved_task.score_breakdown["counts"] == {"严重": 0, "高": 1, "中": 1, "低": 0}
    assert saved_task.summary == consensus
    assert saved_task.status == "success"
    assert saved_task.end_time is not None
    assert saved_task.duration_ms >= 0


def test_finalize_review_keeps_summary_but_marks_truncated_speaker_partial(
    db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一轮专家截断时，即使主持与问题整理成功也不能产出完整报告。"""
    task = ReviewTask(
        user_id=4, project_id=5, task_name="部分圆桌",
        review_type="discuss", status="running", total_files=1, processed_files=0,
    )
    db.add(task)
    db.commit()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(module, "_extract_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "_normalize_discussion_issues", lambda *_args, **_kwargs: [])

    module._finalize_review(
        task_id=task.id, user_id=4, file_id=6, file_name="partial.py",
        all_turns=[_turn(1, content="第一位专家的有效发现")],
        code="x = 1", language="python", deferred_logs=[],
        agent=RecordingAgent(), stopped=False, consensus="主持已归纳有效部分",
        coverage={
            "expected_turns": 2, "attempted_turns": 2, "successful_turns": 1,
            "failed_turns": 1, "valid_speeches": 1, "summary_status": "success",
            "errors": ["可靠性代理第 2 轮输出被截断"],
        },
    )

    saved = db.get(ReviewTask, task.id)
    assert saved.status == "failed"
    assert saved.coverage["stage"] == "partial"
    assert saved.coverage["extraction_status"] == "success"
    assert saved.summary == "主持已归纳有效部分"
    assert saved.processed_files == 0
    assert saved.score == 0


def test_finalize_review_marks_task_failed_when_extraction_fails(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构化抽取失败时必须回滚并把任务标记为失败。

    Args:
        db: 内存 SQLite 会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言失败状态、可读错误与关闭路径。
    """
    task = ReviewTask(
        user_id=4,
        project_id=5,
        task_name="异常收尾",
        review_type="discuss",
        status="running",
        total_files=1,
        processed_files=0,
    )
    db.add(task)
    db.commit()

    def get_session() -> Any:
        """返回当前测试的内存数据库会话。

        Returns:
            Any: Pytest 提供的 SQLAlchemy Session。
        """
        return db

    def raise_extract(*_args: Any, **_kwargs: Any) -> list[Issue]:
        """模拟结构化问题整理发生异常。

        Args:
            *_args: 被测函数传入的位置参数。
            **_kwargs: 被测函数传入的关键字参数。

        Returns:
            list[Issue]: 此 fake 不会正常返回。

        Raises:
            RuntimeError: 始终模拟报告整理失败。
        """
        raise RuntimeError("parser unavailable")

    monkeypatch.setattr(module, "SessionLocal", get_session)
    monkeypatch.setattr(module, "_extract_issues", raise_extract)

    result = module._finalize_review(
        task_id=task.id,
        user_id=4,
        file_id=6,
        file_name="broken.py",
        all_turns=[_turn(1)],
        code="broken()",
        language="python",
        deferred_logs=[],
        agent=RecordingAgent(),
        stopped=False,
    )

    saved_task = db.get(ReviewTask, task.id)
    assert result == task.id
    assert saved_task is not None
    assert saved_task.status == "failed"
    assert saved_task.summary == "圆桌讨论已完成，但报告整理失败。"
    assert "parser unavailable" in saved_task.error_message
    assert saved_task.end_time is not None


def test_finalize_review_marks_task_failed_when_normalization_fails(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归一化失败不能产出成功报告。"""
    task = ReviewTask(
        user_id=4,
        project_id=5,
        task_name="归一化失败",
        review_type="discuss",
        status="running",
        total_files=1,
        processed_files=0,
    )
    db.add(task)
    db.commit()

    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(module, "_extract_issues", lambda *_args, **_kwargs: [Issue(description="风险")])
    monkeypatch.setattr(
        module,
        "_normalize_discussion_issues",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("normalization unavailable")),
    )

    module._finalize_review(
        task_id=task.id,
        user_id=4,
        file_id=6,
        file_name="broken.py",
        all_turns=[_turn(1)],
        code="broken()",
        language="python",
        deferred_logs=[],
        agent=RecordingAgent(),
        stopped=False,
    )

    saved_task = db.get(ReviewTask, task.id)
    assert saved_task is not None
    assert saved_task.status == "failed"
    assert "normalization unavailable" in saved_task.error_message
    assert db.query(ReviewIssue).filter_by(task_id=task.id).count() == 0


def test_finalize_review_marks_task_failed_when_issue_persistence_fails(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """问题入库失败必须回滚问题并保留任务失败原因。"""
    task = ReviewTask(
        user_id=4,
        project_id=5,
        task_name="入库失败",
        review_type="discuss",
        status="running",
        total_files=1,
        processed_files=0,
    )
    db.add(task)
    db.commit()

    issue = Issue(
        line_number=1,
        issue_type="安全漏洞",
        severity="高",
        title="命令注入",
        description="外部输入进入 shell",
    )
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(module, "_extract_issues", lambda *_args, **_kwargs: [issue])
    monkeypatch.setattr(module, "_normalize_discussion_issues", lambda *_args, **_kwargs: [issue])

    real_commit = db.commit
    failed_once = False

    def fail_issue_commit_once() -> None:
        nonlocal failed_once
        if not failed_once and any(isinstance(row, ReviewIssue) for row in db.new):
            failed_once = True
            raise RuntimeError("review issue insert failed")
        real_commit()

    monkeypatch.setattr(db, "commit", fail_issue_commit_once)

    module._finalize_review(
        task_id=task.id,
        user_id=4,
        file_id=6,
        file_name="broken.py",
        all_turns=[_turn(1)],
        code="broken()",
        language="python",
        deferred_logs=[],
        agent=RecordingAgent(),
        stopped=False,
    )

    saved_task = db.get(ReviewTask, task.id)
    assert failed_once is True
    assert saved_task is not None
    assert saved_task.status == "failed"
    assert "review issue insert failed" in saved_task.error_message
    assert saved_task.coverage["extraction_status"] == "success"
    assert db.query(ReviewIssue).filter_by(task_id=task.id).count() == 0


@pytest.mark.asyncio
async def test_start_discussion_runs_full_isolated_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整讨论生命周期应发布发言、事件、环境消息并沉淀报告任务号。

    Args:
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言一轮双 Agent 讨论从开场到关闭的集成行为。
    """
    bus = DiscussionBus()
    session = bus.create_session(
        "roundtable-1",
        task_id=0,
        file_name="round.py",
        owner_user_id=42,
        max_rounds=1,
    )
    orchestrator = _make_orchestrator(bus)
    agent = RecordingAgent(responses=[
        ("  通用代理发现第 2 行问题  ", {"model_name": "fake-model"}),
        ("安全代理同意并补充第 4 行", {"model_name": "fake-model"}),
        ("  总体可修复，优先处理权限问题。  ", {"model_name": "fake-model"}),
    ])
    environment = RecordingEnvironment(roles=["code_reviewer", "security_sentinel"])
    event_bus = RecordingEventBus()
    finalized: dict[str, Any] = {}
    built: dict[str, Any] = {}

    def create_agent() -> RecordingAgent:
        """返回共享的记录型 Agent。

        Returns:
            RecordingAgent: 为发言与汇总准备的 fake Agent。
        """
        return agent

    def build_environment(**kwargs: Any) -> RecordingEnvironment:
        """记录环境构建参数并返回 fake Environment。

        Args:
            **kwargs: start_discussion 传入的环境上下文。

        Returns:
            RecordingEnvironment: 记录消息的环境。
        """
        built.update(kwargs)
        return environment

    def create_task(**kwargs: Any) -> int:
        """模拟创建审查任务。

        Args:
            **kwargs: 任务创建字段。

        Returns:
            int: 固定测试任务 ID 77。
        """
        finalized["create"] = kwargs
        return 77

    def finalize_review(**kwargs: Any) -> int:
        """记录收尾参数并返回报告任务 ID。

        Args:
            **kwargs: 报告收尾所需上下文。

        Returns:
            int: 固定报告任务 ID 88。
        """
        finalized["review"] = kwargs
        return 88

    async def no_sleep(_seconds: float) -> None:
        """替代真实延时以保持测试快速稳定。

        Args:
            _seconds: 原调用计划等待的秒数。

        Returns:
            None: 立即让出事件循环后返回。
        """
        return None

    def fixed_trace_id() -> str:
        """返回确定性的讨论调用链 ID。

        Returns:
            str: 固定测试 trace ID。
        """
        return "trace-roundtable"

    monkeypatch.setattr(
        module, "_build_discussion_agents",
        lambda _user_id, _profiles: (
            create_agent(), {"code_reviewer": create_agent(), "security_sentinel": create_agent()},
        ),
    )
    monkeypatch.setattr(module, "build_discussion_environment", build_environment)
    monkeypatch.setattr(module, "_create_review_task", create_task)
    monkeypatch.setattr(module, "_finalize_review", finalize_review)
    monkeypatch.setattr(module, "new_trace_id", fixed_trace_id)
    monkeypatch.setattr(module, "_review_task_state", lambda _task_id: {
        "status": "success" if "review" in finalized else "running", "error": "",
    })
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(module, "AgentEventBus", EventBusProvider)
    EventBusProvider.current = event_bus

    await orchestrator.start_discussion(
        session_id="roundtable-1",
        profiles=(GENERAL_AGENT, SECURITY_AGENT),
        code="def load(user):\n    return data[user]\n",
        language="python",
        file_name="round.py",
        user_id=42,
        project_id=12,
        file_id=34,
        review_type="full",
        max_rounds=1,
        continuation_context=(
            "【上一轮结论】输入校验问题已经处理。\n"
            "【本次纠正要求】重新核对权限边界，不要沿用上一轮判断。"
            + "长" * 6100 + "【尾部追问】必须核对未授权路径。"
        ),
    )

    assert session.status == "concluded"
    assert session.report_task_id == 88
    assert len(session.turns) == 4
    assert [turn.agent_code for turn in session.turns] == [
        "orchestrator",
        "general",
        "security",
        "orchestrator",
    ]
    assert all(turn.role != "user" for turn in session.turns)
    assert session.turns[1].content == "通用代理发现第 2 行问题"
    assert session.turns[-1].content.startswith("📋 **讨论共识小结**")
    assert "输入校验问题已经处理" in agent.calls[0]["user_prompt"]
    assert "重新核对权限边界" in agent.calls[0]["user_prompt"]
    assert "【尾部追问】必须核对未授权路径。" in agent.calls[0]["user_prompt"]
    assert built["trace_id"] == "trace-roundtable"
    assert built["user_id"] == 42
    assert built["agent_codes"] == ["code_reviewer", "security_sentinel"]
    assert built["max_depth"] == 6
    assert len(environment.messages) == 5
    assert [message.cause_by for message in environment.messages] == [
        "StartDiscussion",
        "DiscussionContinuation",
        "DiscussTurn",
        "DiscussTurn",
        "DiscussionSummary",
    ]
    assert len(event_bus.events) == 8
    assert all(event.user_id == 42 for event in event_bus.events)
    assert finalized["create"]["review_type"] == "full"
    review_args = finalized["review"]
    assert review_args["task_id"] == 77
    assert len(review_args["all_turns"]) == 3
    assert review_args["all_turns"][0].role == "user"
    assert "【尾部追问】必须核对未授权路径。" in review_args["all_turns"][0].content
    assert len(review_args["deferred_logs"]) == 3
    assert review_args["consensus"].startswith("📋 **讨论共识小结**")


@pytest.mark.asyncio
async def test_late_user_input_after_last_speaker_gets_visible_agent_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """末位 Agent 生成中到达的消息不能在轮末清空；应触发可见补答。"""
    bus = DiscussionBus()
    session = bus.create_session(
        "roundtable-late-input", task_id=0, file_name="late.py",
        owner_user_id=42, max_rounds=1,
    )
    orchestrator = _make_orchestrator(bus)
    agent = RecordingAgent()
    seen_inputs: list[list[str]] = []
    finalized: dict[str, Any] = {}

    async def speaker(**kwargs: Any) -> tuple[module.SpeakerDecision, dict[str, Any], bool]:
        seen_inputs.append(list(kwargs["user_inputs"]))
        if len(seen_inputs) == 1:
            assert bus.accept_user_input("roundtable-late-input", "最后一位专家请回答权限边界")
            return module.SpeakerDecision("speak", "propose", None, "第一轮发现"), {}, True
        return module.SpeakerDecision("speak", "supplement", None, "已回答权限边界"), {}, True

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module, "_build_discussion_agents", lambda *_args: (
        agent, {"code_reviewer": agent},
    ))
    monkeypatch.setattr(module, "build_discussion_environment", lambda **_kwargs: RecordingEnvironment())
    monkeypatch.setattr(module, "_create_review_task", lambda **_kwargs: 77)
    monkeypatch.setattr(module, "_finalize_review", lambda **kwargs: finalized.update(kwargs) or 77)
    monkeypatch.setattr(module, "_review_task_state", lambda _task_id: {
        "status": "success" if finalized else "running", "error": "",
    })
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(orchestrator, "_speaker_turn", speaker)
    monkeypatch.setattr(orchestrator, "_summarize", lambda *_args: (
        "主持已汇总最新消息", {"model_name": "host"},
    ))
    monkeypatch.setattr(module, "AgentEventBus", EventBusProvider)
    EventBusProvider.current = RecordingEventBus()

    await orchestrator.start_discussion(
        session_id="roundtable-late-input", profiles=(GENERAL_AGENT,),
        code="x = 1", language="python", file_name="late.py",
        user_id=42, project_id=12, file_id=34, max_rounds=1,
    )

    assert session.status == "concluded"
    assert seen_inputs == [[], ["最后一位专家请回答权限边界"]]
    assert any(turn.role == "user" and "权限边界" in turn.content for turn in session.turns)
    assert any(turn.agent_code == "general" and turn.content == "已回答权限边界"
               for turn in session.turns)
    assert finalized["coverage"]["expected_turns"] == 2
    assert finalized["coverage"]["attempted_turns"] == 2
    assert any(turn.role == "user" and "权限边界" in turn.content
               for turn in finalized["all_turns"])


@pytest.mark.asyncio
async def test_continuous_late_inputs_get_explicit_partial_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两次补答中仍不断发言时，最后一条必须有明确可见状态且报告不算完整。"""
    bus = DiscussionBus()
    session = bus.create_session(
        "roundtable-late-burst", task_id=0, file_name="burst.py",
        owner_user_id=42, max_rounds=1,
    )
    orchestrator = _make_orchestrator(bus)
    agent = RecordingAgent()
    seen_inputs: list[list[str]] = []
    finalized: dict[str, Any] = {}

    async def speaker(**kwargs: Any) -> tuple[module.SpeakerDecision, dict[str, Any], bool]:
        seen_inputs.append(list(kwargs["user_inputs"]))
        assert bus.accept_user_input(
            "roundtable-late-burst", f"连续补充 {len(seen_inputs)}",
        )
        return module.SpeakerDecision("speak", "supplement", None, "当前补答"), {}, True

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module, "_build_discussion_agents", lambda *_args: (
        agent, {"code_reviewer": agent},
    ))
    monkeypatch.setattr(module, "build_discussion_environment", lambda **_kwargs: RecordingEnvironment())
    monkeypatch.setattr(module, "_create_review_task", lambda **_kwargs: 77)
    monkeypatch.setattr(module, "_finalize_review", lambda **kwargs: finalized.update(kwargs) or 77)
    monkeypatch.setattr(module, "_review_task_state", lambda _task_id: {
        "status": "failed" if finalized else "running", "error": "部分结果",
        "coverage": {"stage": "partial"} if finalized else {},
    })
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(orchestrator, "_speaker_turn", speaker)
    monkeypatch.setattr(orchestrator, "_summarize", lambda *_args: (
        "主持已汇总", {"model_name": "host"},
    ))
    monkeypatch.setattr(module, "AgentEventBus", EventBusProvider)
    EventBusProvider.current = RecordingEventBus()

    await orchestrator.start_discussion(
        session_id="roundtable-late-burst", profiles=(GENERAL_AGENT,),
        code="x = 1", language="python", file_name="burst.py",
        user_id=42, project_id=12, file_id=34, max_rounds=1,
    )

    assert len(seen_inputs) == 3
    assert seen_inputs[-1] == ["连续补充 2"]
    assert any(turn.agent_code == "orchestrator" and "1 条专家尚未处理" in turn.content
               for turn in session.turns)
    assert finalized["coverage"]["pending_user_inputs"] == 1
    assert finalized["coverage"]["errors"]
    assert session.progress["phase"] == "partial"


@pytest.mark.asyncio
async def test_cancelled_discussion_marks_task_cancelled_without_finalizing_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登录会话失效取消编排后，不得再抽取问题或生成成功报告。"""
    bus = DiscussionBus()
    session = bus.create_session(
        "roundtable-cancelled",
        task_id=0,
        file_name="cancelled.py",
        owner_user_id=42,
        max_rounds=1,
    )
    orchestrator = _make_orchestrator(bus)
    created = threading.Event()
    cancelled_task_ids: list[int] = []
    finalized = False

    def create_task(**_kwargs: Any) -> int:
        created.set()
        return 77

    def cancel_review(task_id: int) -> int:
        cancelled_task_ids.append(task_id)
        return task_id

    def finalize_review(**_kwargs: Any) -> int:
        nonlocal finalized
        finalized = True
        return 88

    def fail_environment(**_kwargs: Any) -> Any:
        raise RuntimeError("environment disabled")

    monkeypatch.setattr(
        module, "_build_discussion_agents",
        lambda _user_id, _profiles: (RecordingAgent(), {"code_reviewer": RecordingAgent()}),
    )
    monkeypatch.setattr(module, "build_discussion_environment", fail_environment)
    monkeypatch.setattr(module, "_create_review_task", create_task)
    monkeypatch.setattr(module, "_cancel_review_task", cancel_review)
    monkeypatch.setattr(module, "_finalize_review", finalize_review)
    monkeypatch.setattr(module, "_review_task_state", lambda _task_id: {"status": "cancelled", "error": ""})

    run = asyncio.create_task(
        orchestrator.start_discussion(
            session_id="roundtable-cancelled",
            profiles=(GENERAL_AGENT,),
            code="print('cancel')",
            language="python",
            file_name="cancelled.py",
            user_id=42,
            project_id=12,
            file_id=34,
            max_rounds=1,
        )
    )
    assert await asyncio.wait_for(asyncio.to_thread(created.wait, 1), timeout=2)
    await asyncio.sleep(0)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert cancelled_task_ids == [77]
    assert finalized is False
    assert session.status == "concluded"
    assert session.report_task_id == 77


@pytest.mark.asyncio
async def test_start_discussion_handles_missing_session_and_setup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失会话应直接返回，任务创建失败必须失败关闭，不再执行讨论。

    Args:
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言入口短路和两类初始化失败的容错路径。
    """
    empty_bus = DiscussionBus()
    missing_orchestrator = _make_orchestrator(empty_bus)
    assert await missing_orchestrator.start_discussion(
        "missing",
        (),
        "",
        "python",
        "missing.py",
        1,
        2,
        3,
        max_rounds=0,
    ) is None

    bus = DiscussionBus()
    session = bus.create_session("degraded", task_id=0, file_name="degraded.py")
    orchestrator = _make_orchestrator(bus)

    def create_agent() -> RecordingAgent:
        """返回无需响应的 fake Agent。

        Returns:
            RecordingAgent: 空响应记录器。
        """
        return RecordingAgent()

    def fail_environment(**_kwargs: Any) -> RecordingEnvironment:
        """模拟 MetaGPT Environment 构建失败。

        Args:
            **_kwargs: 环境构建参数。

        Returns:
            RecordingEnvironment: 此 fake 不会正常返回。

        Raises:
            RuntimeError: 始终模拟环境构建异常。
        """
        raise RuntimeError("environment build failed")

    def fail_task(**_kwargs: Any) -> int:
        """模拟 ReviewTask 创建失败。

        Args:
            **_kwargs: 任务创建参数。

        Returns:
            int: 此 fake 不会正常返回。

        Raises:
            RuntimeError: 始终模拟数据库异常。
        """
        raise RuntimeError("database unavailable")

    async def no_sleep(_seconds: float) -> None:
        """替代讨论开场真实等待。

        Args:
            _seconds: 原调用等待秒数。

        Returns:
            None: 立即返回。
        """
        return None

    monkeypatch.setattr(
        module, "_build_discussion_agents",
        lambda _user_id, _profiles: (
            create_agent(), {"code_reviewer": create_agent(), "security_sentinel": create_agent()},
        ),
    )
    monkeypatch.setattr(module, "build_discussion_environment", fail_environment)
    monkeypatch.setattr(module, "_create_review_task", fail_task)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(module, "AgentEventBus", EventBusProvider)
    EventBusProvider.current = RecordingEventBus(fail_publish=True)

    await orchestrator.start_discussion(
        "degraded",
        (GENERAL_AGENT,),
        "value = 1\n",
        "python",
        "degraded.py",
        7,
        8,
        9,
        max_rounds=1,
    )

    assert orchestrator._env is None
    assert orchestrator._task_id == 0
    assert session.status == "concluded"
    assert session.report_task_id == 0
    assert len(session.turns) == 1
    assert "database unavailable" in session.turns[0].content
