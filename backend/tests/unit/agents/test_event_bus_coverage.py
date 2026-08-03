"""AgentEventBus 事件广播、Skill 触发与生命周期补充测试。"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest

from app.agents import event_bus as module
from app.agents.base import AgentContext, BaseAgent
from app.agents.events import AgentEvent, AgentEventType


@pytest.fixture(autouse=True)
def _restore_event_bus_state() -> AsyncIterator[None]:
    """隔离并恢复 EventBus 单例、去抖表、信号量和后台任务。"""
    old_instance = module.AgentEventBus._instance
    old_debounce = dict(module._skill_debounce)
    old_semaphore = module._skill_semaphore
    old_task = module._skill_subscriber_task
    module.AgentEventBus._instance = None
    module._skill_debounce.clear()
    module._skill_semaphore = None
    module._skill_subscriber_task = None
    try:
        yield
    finally:
        task = module._skill_subscriber_task
        if task is not None and not task.done():
            task.cancel()
        module.AgentEventBus._instance = old_instance
        module._skill_debounce.clear()
        module._skill_debounce.update(old_debounce)
        module._skill_semaphore = old_semaphore
        module._skill_subscriber_task = old_task


def _event(
    trace_id: str,
    event_type: AgentEventType | str = AgentEventType.REVIEW_ISSUE_STATUS_CHANGED,
) -> AgentEvent:
    """构造带稳定字段的 Agent 事件供各测试复用。"""
    return AgentEvent(
        type=event_type,  # type: ignore[arg-type]
        agent="tester",
        trace_id=trace_id,
        payload={"source": trace_id},
        user_id=7,
    )


@pytest.mark.asyncio
async def test_publish_isolates_full_subscriber_and_retains_history() -> None:
    """单个订阅队列满时应只丢弃该队列消息并继续广播给其他订阅者。"""
    bus = module.AgentEventBus(history_size=2, queue_size=1)
    full_queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=1)
    healthy_queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait(_event("old"))
    loop = asyncio.get_running_loop()
    bus._subscribers.extend([(loop, full_queue), (loop, healthy_queue)])

    current = _event("new")
    bus.publish(current)

    assert full_queue.get_nowait().trace_id == "old"
    assert healthy_queue.get_nowait() is current
    assert [item.trace_id for item in bus.recent(1)] == ["new"]


@pytest.mark.asyncio
async def test_publish_from_listener_thread_wakes_async_subscriber() -> None:
    """Redis 监听线程发布事件时必须通过线程安全回调唤醒 SSE 协程。"""
    bus = module.AgentEventBus(queue_size=1)
    stream = bus.subscribe()
    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)

    thread = threading.Thread(target=bus.publish, args=(_event("thread-live"),))
    thread.start()
    thread.join(timeout=1)

    assert (await asyncio.wait_for(pending, timeout=1)).trace_id == "thread-live"
    await stream.aclose()


def test_recent_merges_redis_stream_and_local_history_without_duplicates() -> None:
    """重启前 Stream 历史与当前进程历史应按时间合并并去重。"""
    redis_only = _event("redis-only")
    redis_only.timestamp = "2026-01-01T00:00:00+00:00"
    duplicated = _event("duplicated")
    duplicated.timestamp = "2026-01-01T00:00:01+00:00"
    local_only = _event("local-only")
    local_only.timestamp = "2026-01-01T00:00:02+00:00"

    class FakeRelay:
        def recent(self, limit: int) -> list[AgentEvent]:
            assert limit == 10
            return [redis_only, duplicated]

        def publish(self, event: AgentEvent) -> None:
            return None

    bus = module.AgentEventBus()
    bus._relay = FakeRelay()  # type: ignore[assignment]
    bus._history.extend([duplicated, local_only])

    assert [event.trace_id for event in bus.recent(10)] == [
        "redis-only", "duplicated", "local-only",
    ]


@pytest.mark.asyncio
async def test_subscribe_deduplicates_event_arriving_during_replay() -> None:
    """回放查询期间到达的实时事件不得在 SSE 中连续出现两次。"""
    replay_event = _event("during-replay")
    replay_started = threading.Event()
    release_replay = threading.Event()

    class BlockingRelay:
        def recent(self, limit: int) -> list[AgentEvent]:
            replay_started.set()
            release_replay.wait(timeout=1)
            return [replay_event]

        def publish(self, event: AgentEvent) -> None:
            return None

    bus = module.AgentEventBus()
    bus._relay = BlockingRelay()  # type: ignore[assignment]
    stream = bus.subscribe(replay=5)
    first = asyncio.create_task(stream.__anext__())
    assert await asyncio.to_thread(replay_started.wait, 1)
    bus.publish(replay_event)
    release_replay.set()

    assert (await asyncio.wait_for(first, timeout=1)).trace_id == "during-replay"
    second = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    bus.publish(_event("next-live"))
    assert (await asyncio.wait_for(second, timeout=1)).trace_id == "next-live"
    await stream.aclose()


def test_redis_envelope_decode_is_backward_compatible() -> None:
    """Redis 解码同时支持旧裸事件与带来源标识的新信封。"""
    relay = module._RedisEventRelay(module.AgentEventBus())
    event = _event("decode")
    bare = module.json.dumps(event.to_dict())
    envelope = module.json.dumps({"source": "worker-b", "event": event.to_dict()})

    source, decoded = relay._decode(bare)
    assert source is None
    assert decoded is not None and decoded.trace_id == "decode"
    source, decoded = relay._decode(envelope)
    assert source == "worker-b"
    assert decoded is not None and decoded.trace_id == "decode"


@pytest.mark.asyncio
async def test_subscribe_removes_queue_after_generator_close() -> None:
    """订阅生成器关闭后应移除内部队列，避免长期连接泄漏。"""
    bus = module.AgentEventBus(queue_size=1)
    stream = bus.subscribe()
    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)

    assert len(bus._subscribers) == 1
    bus.publish(_event("live"))
    assert (await pending).trace_id == "live"

    await stream.aclose()
    assert bus._subscribers == []


def test_emit_event_populates_all_public_fields() -> None:
    """统一 emit helper 应完整传递父链、负载和用户归属字段。"""
    bus = module.AgentEventBus()
    module.AgentEventBus._instance = bus

    module.emit_event(
        AgentEventType.PROGRESS,
        "reviewer",
        "trace-1",
        parent="root",
        message="working",
        payload={"percent": 50},
        user_id=9,
    )

    event = bus.recent(1)[0]
    assert event.type is AgentEventType.PROGRESS
    assert event.agent == "reviewer"
    assert event.trace_id == "trace-1"
    assert event.parent == "root"
    assert event.message == "working"
    assert event.payload == {"percent": 50}
    assert event.user_id == 9


def test_base_agent_events_keep_current_user_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通 Agent 事件必须携带当前用户 ID，避免作为全局系统事件广播。"""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "emit_event", lambda **kwargs: captured.append(kwargs))
    agent = BaseAgent()

    agent._emit(
        AgentEventType.PROGRESS,
        AgentContext(user_id=42, extra={"trace_id": "owned-trace"}),
        message="working",
    )

    assert captured[0]["user_id"] == 42
    assert captured[0]["trace_id"] == "owned-trace"


def test_base_agent_never_exposes_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """最终 content 为空时必须失败，不能把模型内部推理回显给用户。"""
    post_calls = 0

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "private chain of thought"},
                }],
                "usage": {},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            nonlocal post_calls
            post_calls += 1
            return FakeResponse()

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__("app.utils.public_http", fromlist=["PinnedPublicUrl"]).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()
    agent._max_retries = 2

    result = agent.call("test")

    assert result.success is False
    assert result.failure_kind == "output_truncated"
    assert result.finish_reason == "length"
    assert post_calls == 1
    assert "finish_reason=length" in str(result.error)
    assert "private chain of thought" not in str(result.error)


def test_base_agent_rejects_parseable_nonempty_length_response_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恰好闭合的截断 JSON 仍是不完整输出，不得当成零漏洞结论。"""
    post_calls = 0
    events: list[AgentEventType] = []

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": '{"findings": []}'},
                }],
                "usage": {"completion_tokens": 4096},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            nonlocal post_calls
            post_calls += 1
            return FakeResponse()

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__("app.utils.public_http", fromlist=["PinnedPublicUrl"]).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()
    agent._max_retries = 2
    monkeypatch.setattr(
        agent,
        "_emit",
        lambda event_type, *_args, **_kwargs: events.append(event_type),
    )

    result = agent.call_json("test")

    assert result.success is False
    assert result.failure_kind == "output_truncated"
    assert post_calls == 1
    assert AgentEventType.FAILED in events
    assert AgentEventType.COMPLETE not in events


def test_base_agent_still_retries_rate_limit_then_accepts_stop_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 仍属于可恢复错误，但只接纳 stop 终止的完整响应。"""
    responses = [
        SimpleNamespace(status_code=429, text="rate limited"),
        SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {},
            },
        ),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr("app.agents.base.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__("app.utils.public_http", fromlist=["PinnedPublicUrl"]).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()
    agent._max_retries = 2

    result = agent.call_json("test")

    assert result.success is True
    assert result.data == {}
    assert result.finish_reason == "stop"
    assert responses == []


@pytest.mark.parametrize(
    ("thinking", "expected"),
    [
        (None, None),
        (False, {"type": "disabled"}),
        (True, {"type": "enabled"}),
    ],
)
def test_base_agent_thinking_request_body_is_explicit_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    thinking: bool | None,
    expected: dict[str, str] | None,
) -> None:
    payloads: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            payloads.append(kwargs["json"])
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {
                    "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                    "usage": {},
                },
            )

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__(
            "app.utils.public_http",
            fromlist=["PinnedPublicUrl"],
        ).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()

    result = agent.call_json("test", thinking=thinking)

    assert result.success is True
    assert len(payloads) == 1
    if expected is None:
        assert "thinking" not in payloads[0]
    else:
        assert payloads[0]["thinking"] == expected


def test_base_agent_does_not_sleep_past_shared_audit_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试退避不得突破整次语义审计的共享墙钟时限。"""
    post_calls = 0
    sleeps: list[float] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            nonlocal post_calls
            post_calls += 1
            return SimpleNamespace(status_code=429, text="rate limited")

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr("app.agents.base.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("app.agents.base.time.sleep", sleeps.append)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__("app.utils.public_http", fromlist=["PinnedPublicUrl"]).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()
    agent._max_retries = 2

    result = agent.call_json("test", deadline_monotonic=100.05)

    assert result.success is False
    assert result.failure_kind == "semantic_budget_exhausted"
    assert post_calls == 1
    assert sleeps == []


def test_base_agent_invalid_json_emits_failed_without_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[AgentEventType] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {
                    "choices": [{"finish_reason": "stop", "message": {"content": "{bad"}}],
                    "usage": {},
                },
            )

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__(
            "app.utils.public_http",
            fromlist=["PinnedPublicUrl"],
        ).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()
    monkeypatch.setattr(
        agent,
        "_emit",
        lambda event_type, *_args, **_kwargs: events.append(event_type),
    )

    result = agent.call_json("test")

    assert result.success is False
    assert result.failure_kind == "invalid_json"
    assert events.count(AgentEventType.FAILED) == 1
    assert AgentEventType.COMPLETE not in events


def test_base_agent_rejects_non_string_content_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_calls = 0

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            nonlocal post_calls
            post_calls += 1
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {
                    "choices": [{"finish_reason": "stop", "message": {"content": {}}}],
                    "usage": {},
                },
            )

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__(
            "app.utils.public_http",
            fromlist=["PinnedPublicUrl"],
        ).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()
    agent._max_retries = 2

    result = agent.call_json("test")

    assert result.success is False
    assert result.failure_kind == "invalid_response"
    assert post_calls == 1


def test_base_agent_rejects_non_stop_terminal_reason_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_calls = 0

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            nonlocal post_calls
            post_calls += 1
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {
                    "choices": [{
                        "finish_reason": "content_filter",
                        "message": {"content": "{}"},
                    }],
                    "usage": {},
                },
            )

    monkeypatch.setattr("app.agents.base.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: __import__(
            "app.utils.public_http",
            fromlist=["PinnedPublicUrl"],
        ).PinnedPublicUrl(
            url,
            "https://93.184.216.34/chat/completions",
            "api.deepseek.com",
            "api.deepseek.com",
            "93.184.216.34",
        ),
    )
    agent = BaseAgent()
    agent._max_retries = 3

    result = agent.call_json("test")

    assert result.success is False
    assert result.failure_kind == "incomplete_response"
    assert result.finish_reason == "content_filter"
    assert post_calls == 1


def test_skill_semaphore_and_debounce_follow_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发下限应至少为一，去抖应在窗口内拒绝并在过期后恢复。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "skill_trigger_max_concurrency", 0)
    monkeypatch.setattr(settings, "skill_event_debounce_seconds", 10)
    try:
        previous_loop = asyncio.get_event_loop()
    except RuntimeError:
        previous_loop = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    module._skill_semaphore = None
    module._skill_debounce.clear()
    try:
        semaphore = module._get_skill_semaphore()
        assert semaphore._value == 1
        assert module._get_skill_semaphore() is semaphore

        timestamps = iter([100.0, 105.0, 111.0])
        monkeypatch.setattr(module.time, "time", lambda: next(timestamps))
        event_type = AgentEventType.SECURITY_SCAN_COMPLETED

        assert module._should_trigger("security", "self-improve", event_type) is True
        assert module._should_trigger("security", "self-improve", event_type) is False
        assert module._should_trigger("security", "self-improve", event_type) is True
    finally:
        module._skill_semaphore = None
        loop.close()
        asyncio.set_event_loop(previous_loop)


@pytest.mark.asyncio
async def test_invoke_skill_uses_isolated_session_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事件触发 Skill 时应注入独立会话、构造上下文并在完成后关闭会话。"""
    import app.agents.orchestrator as orchestrator_module
    import app.core.database as database_module

    calls: list[dict[str, Any]] = []

    class FakeDb:
        """记录独立数据库会话是否被关闭。"""

        def __init__(self) -> None:
            """初始化关闭状态。"""
            self.closed = False

        def close(self) -> None:
            """标记会话关闭。"""
            self.closed = True

    class FakeOrchestrator:
        """记录事件触发的 Skill 调用参数。"""

        def __init__(self, register: bool) -> None:
            """记录注册开关并预留数据库注入位。"""
            self.register = register
            self._db: Any = None

        def invoke_skill(self, **kwargs: Any) -> SimpleNamespace:
            """记录调用并返回带 effect 的成功结果。"""
            calls.append({"orchestrator": self, **kwargs})
            return SimpleNamespace(success=True, data={"effect": "improved"})

    db = FakeDb()
    monkeypatch.setattr(database_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(orchestrator_module, "Orchestrator", FakeOrchestrator)
    event = _event("skill-trace", AgentEventType.AI_CALL_THRESHOLD_REACHED)

    await module._invoke_skill_async(
        "orchestrator",
        "orchestrator.self_improve",
        "evolve",
        event,
    )

    assert db.closed is True
    assert len(calls) == 1
    call = calls[0]
    assert call["orchestrator"].register is False
    assert call["orchestrator"]._db is db
    assert call["params"] == {"action": "evolve", "event_payload": event.payload}
    assert call["trigger_type"] == "event"
    assert call["trigger_source"] == "event:ai_call_threshold_reached"
    assert call["ctx"].extra["trace_id"] == "skill-trace"
    assert call["ctx"].extra["event_type"] == "ai_call_threshold_reached"
    assert call["ctx"].user_id == 7


@pytest.mark.asyncio
async def test_invoke_skill_swallows_failure_but_still_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill 调用异常不得杀死订阅器，且 finally 必须关闭独立会话。"""
    import app.agents.orchestrator as orchestrator_module
    import app.core.database as database_module

    class FakeDb:
        """提供可观察的关闭状态。"""

        def __init__(self) -> None:
            """初始化关闭状态。"""
            self.closed = False

        def close(self) -> None:
            """标记数据库会话已关闭。"""
            self.closed = True

    class FailingOrchestrator:
        """模拟同步 Skill 调用失败。"""

        def __init__(self, register: bool) -> None:
            """接收生产构造参数。"""
            self._db: Any = None

        def invoke_skill(self, **kwargs: Any) -> None:
            """抛出受控错误以验证降级。"""
            raise RuntimeError("skill failed")

    db = FakeDb()
    monkeypatch.setattr(database_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(orchestrator_module, "Orchestrator", FailingOrchestrator)

    await module._invoke_skill_async("agent", "skill", "run", _event("failed"))

    assert db.closed is True


class _FiniteBus:
    """提供有限事件流以确定性运行 Skill 订阅循环。"""

    def __init__(self, events: list[AgentEvent], error: Exception | None = None) -> None:
        """保存待发送事件与可选订阅异常。"""
        self.events = events
        self.error = error

    async def subscribe(self, replay: int = 0) -> AsyncIterator[AgentEvent]:
        """按顺序产生事件，并可在结尾抛出受控异常。"""
        assert replay == 0
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_skill_trigger_loop_filters_and_dispatches_supported_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """订阅循环应兼容字符串枚举、过滤未知类型并隔离单条处理异常。"""
    events = [
        _event("enum", AgentEventType.REVIEW_ISSUE_STATUS_CHANGED),
        _event("plain", AgentEventType.DISPATCH),
        _event("string", AgentEventType.SECURITY_SCAN_COMPLETED.value),
        _event("invalid", "not-an-event"),
        _event("boom", AgentEventType.AI_CALL_THRESHOLD_REACHED),
        _event("after", AgentEventType.EVOLUTION_PROPOSAL_PROMOTED),
    ]
    fake_bus = _FiniteBus(events)
    invoked: list[tuple[str, str, str, str]] = []

    def fake_should_trigger(
        agent_name: str,
        skill_name: str,
        event_type: AgentEventType,
    ) -> bool:
        """让指定事件抛错，其余事件允许触发。"""
        if event_type is AgentEventType.AI_CALL_THRESHOLD_REACHED:
            raise RuntimeError("bad debounce")
        return True

    async def fake_invoke(
        agent_name: str,
        skill_name: str,
        action: str,
        event: AgentEvent,
    ) -> None:
        """记录 fire-and-forget Skill 调用。"""
        invoked.append((agent_name, skill_name, action, event.trace_id))

    monkeypatch.setattr(module.AgentEventBus, "instance", classmethod(lambda cls: fake_bus))
    monkeypatch.setattr(module, "_should_trigger", fake_should_trigger)
    monkeypatch.setattr(module, "_invoke_skill_async", fake_invoke)

    await module._skill_trigger_loop()
    await asyncio.sleep(0)

    assert [item[3] for item in invoked] == ["enum", "string", "after"]
    assert invoked[0][:3] == (
        "code_reviewer",
        "code_reviewer.self_improve",
        "reflect_from_logs",
    )


@pytest.mark.asyncio
async def test_skill_trigger_loop_contains_subscription_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """订阅源异常应由循环兜底记录并正常返回。"""
    fake_bus = _FiniteBus([], error=RuntimeError("stream failed"))
    monkeypatch.setattr(module.AgentEventBus, "instance", classmethod(lambda cls: fake_bus))

    await module._skill_trigger_loop()


class _TaskStub:
    """模拟 start 生命周期中的 asyncio task。"""

    def __init__(self, done: bool = False) -> None:
        """保存完成状态和取消标记。"""
        self._done = done
        self.cancelled = False

    def done(self) -> bool:
        """返回任务完成状态。"""
        return self._done

    def cancel(self) -> None:
        """记录取消动作。"""
        self.cancelled = True


@pytest.mark.asyncio
async def test_start_and_stop_skill_subscriber_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动应尊重禁用与幂等设置，停止应取消运行任务并清空句柄。"""
    from app.core.config import settings

    created: list[_TaskStub] = []
    real_create_task = asyncio.create_task

    def fake_create_task(coro: Any) -> _TaskStub:
        """关闭未调度协程并返回可观察的 task stub。"""
        coro.close()
        task = _TaskStub()
        created.append(task)
        return task

    running = _TaskStub()
    module._skill_subscriber_task = running  # type: ignore[assignment]
    monkeypatch.setattr(module.asyncio, "create_task", fake_create_task)
    module.start_skill_event_subscriber()
    assert created == []

    module._skill_subscriber_task = None
    monkeypatch.setattr(settings, "skill_event_trigger_enabled", False)
    module.start_skill_event_subscriber()
    assert created == []

    monkeypatch.setattr(settings, "skill_event_trigger_enabled", True)
    module.start_skill_event_subscriber()
    assert module._skill_subscriber_task is created[0]

    monkeypatch.setattr(module.asyncio, "create_task", real_create_task)

    async def wait_forever() -> None:
        """保持真实 task 运行直到停止函数取消它。"""
        await asyncio.Event().wait()

    real_task = asyncio.create_task(wait_forever())
    await asyncio.sleep(0)
    module._skill_subscriber_task = real_task
    await module.stop_skill_event_subscriber()
    assert real_task.cancelled() is True
    assert module._skill_subscriber_task is None

    await module.stop_skill_event_subscriber()
