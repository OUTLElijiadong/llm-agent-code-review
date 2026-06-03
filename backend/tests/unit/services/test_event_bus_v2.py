"""单元测试 (v2.0 M2): EventBus 发布/订阅/回放"""
import asyncio

import pytest

from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEvent, AgentEventType


def _reset_bus():
    """每次测试用独立的 EventBus 实例"""
    AgentEventBus._instance = None
    return AgentEventBus.instance()


def test_publish_appends_to_history():
    bus = _reset_bus()
    bus.publish(AgentEvent(
        type=AgentEventType.DISPATCH,
        agent="orchestrator", trace_id="trc_1", message="hi",
    ))
    history = bus.recent(10)
    assert len(history) == 1
    assert history[0].agent == "orchestrator"


def test_history_capped_at_max_size():
    bus = AgentEventBus(history_size=5)
    for i in range(10):
        bus.publish(AgentEvent(
            type=AgentEventType.THINKING,
            agent="x", trace_id=f"trc_{i}",
        ))
    assert len(bus.recent(99)) == 5


def test_event_to_dict_serializable():
    ev = AgentEvent(type=AgentEventType.COMPLETE, agent="a", trace_id="t")
    d = ev.to_dict()
    assert d["type"] == "complete"
    assert d["agent"] == "a"
    assert d["trace_id"] == "t"


@pytest.mark.asyncio
async def test_subscribe_replays_history_and_streams_new_events():
    bus = _reset_bus()
    bus.publish(AgentEvent(
        type=AgentEventType.DISPATCH, agent="a", trace_id="t1",
    ))
    bus.publish(AgentEvent(
        type=AgentEventType.COMPLETE, agent="a", trace_id="t1",
    ))

    received = []

    async def consumer():
        async for ev in bus.subscribe(replay=10):
            received.append(ev)
            if len(received) >= 3:
                return

    task = asyncio.create_task(consumer())
    # 等一下确保订阅启动并回放历史
    await asyncio.sleep(0.05)
    bus.publish(AgentEvent(
        type=AgentEventType.THINKING, agent="b", trace_id="t2",
    ))
    await asyncio.wait_for(task, timeout=2)

    assert len(received) == 3
    assert [r.agent for r in received] == ["a", "a", "b"]
