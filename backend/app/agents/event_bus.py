"""Agent 事件总线 (v2.0 M2)

单例,内存广播。所有 Agent 通过 emit() 发布事件,所有订阅者(主要是 SSE 端点)
通过 subscribe() 拿到事件流。同时缓存最近 200 条,新订阅者首屏即可看到近况。

线程模型: FastAPI 默认单 worker 多协程,asyncio.Queue 足够。
若未来上多 worker,可在此切换到 Redis pub/sub。
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator, Deque, List, Optional

from loguru import logger

from app.agents.events import AgentEvent, AgentEventType


class AgentEventBus:
    _instance: Optional["AgentEventBus"] = None

    def __init__(self, history_size: int = 200, queue_size: int = 64):
        self._subscribers: List[asyncio.Queue] = []
        self._history: Deque[AgentEvent] = deque(maxlen=history_size)
        self._queue_size = queue_size

    @classmethod
    def instance(cls) -> "AgentEventBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def publish(self, event: AgentEvent) -> None:
        """同步发布事件,任何超出队列容量的订阅者会被静默丢弃这一条"""
        self._history.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    f"[EventBus] 队列已满,丢弃事件 trace={event.trace_id} type={event.type}",
                )

    def recent(self, limit: int = 50) -> List[AgentEvent]:
        return list(self._history)[-limit:]

    async def subscribe(self, replay: int = 0) -> AsyncIterator[AgentEvent]:
        """订阅事件流。replay>0 时先回放最近 N 条历史。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(q)
        logger.debug(
            f"[EventBus] 新订阅者加入,当前总数={len(self._subscribers)}",
        )
        try:
            if replay > 0:
                for ev in self.recent(replay):
                    yield ev
            while True:
                yield await q.get()
        finally:
            self._subscribers.remove(q)
            logger.debug(
                f"[EventBus] 订阅者退出,当前总数={len(self._subscribers)}",
            )


def emit_event(
    type_: AgentEventType,
    agent: str,
    trace_id: str,
    parent: str = "",
    message: str = "",
    payload: Optional[dict] = None,
) -> None:
    """Agent 内统一 emit 辅助"""
    AgentEventBus.instance().publish(
        AgentEvent(
            type=type_,
            agent=agent,
            trace_id=trace_id,
            parent=parent,
            message=message,
            payload=payload or {},
        ),
    )
