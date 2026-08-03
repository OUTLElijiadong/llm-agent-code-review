"""圆桌讨论编排器隔离覆盖率测试。

用例通过内存数据库、记录型 fake 与 monkeypatch 隔离 LLM、事件总线、
MetaGPT Environment、WebSocket 讨论总线和真实等待，不访问任何外部服务。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional

import pytest

from app.agents.discussion_bus import DiscussionBus
from app.agents.events import AgentEvent, AgentEventType, DiscussionTurn
from app.ai import discussion_orchestrator as module
from app.ai.discussion_orchestrator import DiscussionOrchestrator
from app.ai.multi_agent import GENERAL_AGENT, SECURITY_AGENT
from app.ai.result_parser import Issue
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile


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
    """Agent 发言应包含历史与最近三条用户指示并去除首尾空白。

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
    assert "忽略旧指示" not in call["user_prompt"]
    assert all(text in call["user_prompt"] for text in ("检查输入", "关注权限", "给出行号"))
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
    assert summary.endswith("结" * 2000)
    assert returned_meta == meta
    assert success_agent.calls[0]["agent_label"] == "general"

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
    assert "A" * 100 + "..." in fallback
    assert "  · 短结论" in fallback
    assert fallback_meta is None


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

    task_id = module._create_review_task(
        user_id=3,
        project_id=4,
        file_id=5,
        file_name="demo.py",
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


def test_extract_issues_returns_empty_when_llm_fails() -> None:
    """结构化问题 LLM 调用失败时应返回空列表。

    Returns:
        None: 断言主调用异常不会传播到报告收尾流程。
    """
    issues = module._extract_issues(
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

    assert issues == []


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
    assert rows[1].end_line == 6
    assert rows[1].fixed_code == "check_permission()"
    assert saved_task is not None
    assert saved_task.processed_files == 1
    assert saved_task.total_issues == 2
    assert saved_task.high_issues == 1
    assert saved_task.medium_issues == 1
    assert saved_task.score == 89
    assert saved_task.summary == consensus
    assert saved_task.status == "success"
    assert saved_task.end_time is not None
    assert saved_task.duration_ms >= 0


def test_finalize_review_recovers_task_when_report_processing_fails(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告整理异常时应回滚并把已有讨论任务标记为可展示成功。

    Args:
        db: 内存 SQLite 会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言异常恢复状态、摘要与关闭路径。
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
    assert saved_task.status == "success"
    assert saved_task.summary == "圆桌讨论已完成(报告整理部分失败)。"
    assert saved_task.end_time is not None


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

    monkeypatch.setattr(module, "DeepSeekAgent", create_agent)
    monkeypatch.setattr(module, "build_discussion_environment", build_environment)
    monkeypatch.setattr(module, "_create_review_task", create_task)
    monkeypatch.setattr(module, "_finalize_review", finalize_review)
    monkeypatch.setattr(module, "new_trace_id", fixed_trace_id)
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
    assert session.turns[1].content == "通用代理发现第 2 行问题"
    assert session.turns[-1].content.startswith("📋 **讨论共识小结**")
    assert built["trace_id"] == "trace-roundtable"
    assert built["user_id"] == 42
    assert built["agent_codes"] == ["code_reviewer", "security_sentinel"]
    assert built["max_depth"] == 6
    assert len(environment.messages) == 4
    assert [message.cause_by for message in environment.messages] == [
        "StartDiscussion",
        "DiscussTurn",
        "DiscussTurn",
        "DiscussionSummary",
    ]
    assert len(event_bus.events) == 8
    assert all(event.user_id == 42 for event in event_bus.events)
    assert finalized["create"]["review_type"] == "full"
    review_args = finalized["review"]
    assert review_args["task_id"] == 77
    assert len(review_args["all_turns"]) == 2
    assert len(review_args["deferred_logs"]) == 3
    assert review_args["consensus"].startswith("📋 **讨论共识小结**")


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

    monkeypatch.setattr(module, "DeepSeekAgent", lambda: RecordingAgent())
    monkeypatch.setattr(module, "build_discussion_environment", fail_environment)
    monkeypatch.setattr(module, "_create_review_task", create_task)
    monkeypatch.setattr(module, "_cancel_review_task", cancel_review)
    monkeypatch.setattr(module, "_finalize_review", finalize_review)

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
    """缺失会话应直接返回，环境与任务创建失败时仍应完成降级关闭。

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

    monkeypatch.setattr(module, "DeepSeekAgent", create_agent)
    monkeypatch.setattr(module, "build_discussion_environment", fail_environment)
    monkeypatch.setattr(module, "_create_review_task", fail_task)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(module, "AgentEventBus", EventBusProvider)
    EventBusProvider.current = RecordingEventBus(fail_publish=True)

    await orchestrator.start_discussion(
        "degraded",
        (),
        "",
        "python",
        "degraded.py",
        7,
        8,
        9,
        max_rounds=0,
    )

    assert orchestrator._env is None
    assert orchestrator._task_id == 0
    assert session.status == "concluded"
    assert session.report_task_id == 0
    assert len(session.turns) == 2
