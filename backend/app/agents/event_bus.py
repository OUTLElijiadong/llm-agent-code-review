"""Agent 事件总线 (v2.0 M2 + v3.0 AgentSkill 事件触发)

单例,内存广播。所有 Agent 通过 emit() 发布事件,所有订阅者(主要是 SSE 端点)
通过 subscribe() 拿到事件流。同时缓存最近 200 条,新订阅者首屏即可看到近况。

线程模型: FastAPI 默认单 worker 多协程,asyncio.Queue 足够。
若未来上多 worker,可在此切换到 Redis pub/sub。

v3.0 新增:
- subscribe_skill_triggers(): 订阅事件触发 Skill 的后台 task
- _SKILL_TRIGGER_MAP: 事件类型 → (agent_name, skill_name, action) 映射
- 去抖(5min) + 全局并发限制(N=3) 防雪崩
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import AsyncIterator, Deque, Dict, List, Optional, Tuple

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
        """获取最近 N 条历史事件

        Args:
            limit: 返回上限

        Returns:
            List[AgentEvent]: 最近的 N 条事件(按时间正序)
        """
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
    user_id: Optional[int] = None,
) -> None:
    """Agent 内统一 emit 辅助

    Args:
        type_: 事件类型
        agent: Agent 标识
        trace_id: 调用链 ID
        parent: 父事件 ID(可选)
        message: 事件描述
        payload: 事件负轂数据
        user_id: 归属用户 ID(用于 SSE 按用户隔离,None 表示系统级事件)
    """
    AgentEventBus.instance().publish(
        AgentEvent(
            type=type_,
            agent=agent,
            trace_id=trace_id,
            parent=parent,
            message=message,
            payload=payload or {},
            user_id=user_id,
        ),
    )


# =================== v3.0 AgentSkill: 事件触发 Skill ===================

# 事件订阅清单(详见 CONSENSUS §6):
# - REVIEW_ISSUE_STATUS_CHANGED → code_reviewer.self_improve
# - SECURITY_SCAN_COMPLETED → security_sentinel.self_improve
# - AI_CALL_THRESHOLD_REACHED → orchestrator.self_improve
# - EVOLUTION_PROPOSAL_PROMOTED → evolution.self_improve
_SKILL_TRIGGER_MAP: Dict[AgentEventType, Tuple[str, str, str]] = {
    AgentEventType.REVIEW_ISSUE_STATUS_CHANGED: (
        "code_reviewer", "code_reviewer.self_improve", "reflect_from_logs",
    ),
    AgentEventType.SECURITY_SCAN_COMPLETED: (
        "security_sentinel", "security_sentinel.self_improve", "reflect_from_logs",
    ),
    AgentEventType.AI_CALL_THRESHOLD_REACHED: (
        "orchestrator", "orchestrator.self_improve", "evolve",
    ),
    AgentEventType.EVOLUTION_PROPOSAL_PROMOTED: (
        "evolution", "evolution.self_improve", "evolve",
    ),
}

# 去抖字典: key = f"{agent_name}:{skill_name}:{event_type}", value = 上次触发时间戳
_skill_debounce: Dict[str, float] = {}

# 后台 task 句柄(供 start/stop 控制)
_skill_subscriber_task: Optional[asyncio.Task] = None

# 全局并发 Semaphore(延迟初始化,避免事件循环未启动时报错)
_skill_semaphore: Optional[asyncio.Semaphore] = None


def _get_skill_semaphore() -> asyncio.Semaphore:
    """获取(必要时创建)全局 Skill 触发并发 Semaphore

    Returns:
        asyncio.Semaphore: 并发限制信号量(默认 N=3, 由 settings.skill_trigger_max_concurrency 决定)
    """
    global _skill_semaphore
    if _skill_semaphore is None:
        try:
            from app.core.config import settings
            max_concurrency = max(1, int(getattr(settings, "skill_trigger_max_concurrency", 3)))
        except Exception:
            max_concurrency = 3
        _skill_semaphore = asyncio.Semaphore(max_concurrency)
        logger.info(f"[EventBus] Skill 触发并发限制 N={max_concurrency}")
    return _skill_semaphore


def _get_debounce_seconds() -> float:
    """获取去抖窗口秒数(默认 300s,由 settings.skill_event_debounce_seconds 决定)

    Returns:
        float: 去抖窗口秒数
    """
    try:
        from app.core.config import settings
        return float(getattr(settings, "skill_event_debounce_seconds", 300))
    except Exception:
        return 300.0


def _should_trigger(agent_name: str, skill_name: str, event_type: AgentEventType) -> bool:
    """去抖判断: 同 key 在去抖窗口内不重复触发

    Args:
        agent_name: 目标 Agent name
        skill_name: 目标 Skill name
        event_type: 触发事件类型

    Returns:
        bool: True=允许触发, False=已被去抖过滤
    """
    key = f"{agent_name}:{skill_name}:{event_type.value}"
    now = time.time()
    last = _skill_debounce.get(key, 0.0)
    debounce = _get_debounce_seconds()
    if now - last < debounce:
        logger.debug(
            f"[EventBus] Skill 触发被去抖过滤: key={key} "
            f"剩余 {int(debounce - (now - last))}s"
        )
        return False
    _skill_debounce[key] = now
    return True


async def _invoke_skill_async(
    agent_name: str,
    skill_name: str,
    action: str,
    event: AgentEvent,
) -> None:
    """异步触发 Skill(在并发 Semaphore 控制下执行)

    通过 asyncio.to_thread 包装同步的 Orchestrator.invoke_skill 调用,
    避免阻塞事件循环。失败时仅记录日志,不向上传播(事件订阅者异常不能杀死 task)。

    Args:
        agent_name: 目标 Agent name
        skill_name: 目标 Skill name
        action: Skill 子动作(如 evolve/reflect_from_logs)
        event: 触发事件(供 trigger_source 与 ctx 使用)
    """
    async with _get_skill_semaphore():
        try:
            from app.agents.base import AgentContext
            from app.agents.orchestrator import Orchestrator
            from app.core.database import SessionLocal

            # 触发来源标记(写入 agent_skill_record.trigger_source)
            trigger_source = f"event:{event.type.value}"

            # 用独立 DB 会话,不依赖请求级 orchestrator
            db = SessionLocal()
            try:
                # 构造请求级 Orchestrator(register=False 避免污染全局注册中心)
                orch = Orchestrator(register=False)
                orch._db = db  # 直接注入 db,跳过 inject_db 的 user 依赖
                ctx = AgentContext(
                    trace_id=event.trace_id,
                    user_id=event.user_id,
                    extra={"event_type": event.type.value, "event_payload": event.payload},
                )
                # asyncio.to_thread 避免阻塞事件循环
                # trigger_type="event" 确保 agent_skill_record 正确归类
                result = await asyncio.to_thread(
                    orch.invoke_skill,
                    agent_name=agent_name,
                    skill_name=skill_name,
                    params={"action": action, "event_payload": event.payload},
                    ctx=ctx,
                    trigger_type="event",
                    trigger_source=trigger_source,
                )
                # result.data 是 skill_service 返回的 dict,含 effect 字段
                effect = "?"
                if isinstance(result.data, dict):
                    effect = result.data.get("effect", "?")
                logger.info(
                    f"[EventBus] 事件触发 Skill 完成: event={event.type.value} "
                    f"skill={skill_name} success={result.success} effect={effect}"
                )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 - 订阅者异常不能杀死 task
            logger.warning(
                f"[EventBus] 事件触发 Skill 异常: event={event.type.value} "
                f"skill={skill_name} error={exc}"
            )


async def _skill_trigger_loop() -> None:
    """Skill 事件触发后台 task 主循环

    订阅 EventBus, 收到匹配事件后调用 _invoke_skill_async 触发对应 Skill。
    异常时仅记录日志, 不向上传播(避免 task 退出)。
    """
    logger.info("[EventBus] Skill 事件触发订阅器已启动")
    bus = AgentEventBus.instance()
    try:
        async for event in bus.subscribe(replay=0):
            try:
                # 只处理 _SKILL_TRIGGER_MAP 中定义的事件类型
                event_type = event.type if isinstance(event.type, AgentEventType) else None
                if event_type is None:
                    # 兼容字符串类型(从 to_dict 反序列化时)
                    try:
                        event_type = AgentEventType(event.type)
                    except (ValueError, TypeError):
                        continue
                if event_type not in _SKILL_TRIGGER_MAP:
                    continue

                agent_name, skill_name, action = _SKILL_TRIGGER_MAP[event_type]
                # 去抖判断
                if not _should_trigger(agent_name, skill_name, event_type):
                    continue

                logger.info(
                    f"[EventBus] 事件触发 Skill: event={event_type.value} "
                    f"→ {agent_name}.{skill_name} action={action}"
                )
                # 后台 fire-and-forget(不等结果,继续处理下一条事件)
                asyncio.create_task(
                    _invoke_skill_async(agent_name, skill_name, action, event)
                )
            except Exception as exc:  # noqa: BLE001 - 单条事件处理异常不影响后续
                logger.warning(f"[EventBus] Skill 触发循环处理事件异常: {exc}")
    except asyncio.CancelledError:
        logger.info("[EventBus] Skill 事件触发订阅器已停止")
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底
        logger.exception(f"[EventBus] Skill 事件触发订阅器异常退出: {exc}")


def start_skill_event_subscriber() -> None:
    """启动 Skill 事件触发后台 task(供 FastAPI lifespan 调用)

    幂等: 已启动则直接返回; settings.skill_event_trigger_enabled=False 时跳过。

    Returns:
        None
    """
    global _skill_subscriber_task
    if _skill_subscriber_task is not None and not _skill_subscriber_task.done():
        logger.debug("[EventBus] Skill 事件触发订阅器已在运行,跳过")
        return

    try:
        from app.core.config import settings
        if not bool(getattr(settings, "skill_event_trigger_enabled", True)):
            logger.info("[EventBus] Skill 事件触发已通过配置禁用,跳过启动")
            return
    except Exception:
        pass  # 配置读取失败时默认启动

    _skill_subscriber_task = asyncio.create_task(_skill_trigger_loop())
    logger.info("[EventBus] Skill 事件触发订阅器 task 已创建")


async def stop_skill_event_subscriber() -> None:
    """停止 Skill 事件触发后台 task(供 FastAPI lifespan 调用)

    Returns:
        None
    """
    global _skill_subscriber_task
    if _skill_subscriber_task is None:
        return
    if not _skill_subscriber_task.done():
        _skill_subscriber_task.cancel()
        try:
            await _skill_subscriber_task
        except asyncio.CancelledError:
            pass
    _skill_subscriber_task = None
    logger.info("[EventBus] Skill 事件触发订阅器 task 已停止")
