"""多 Agent 讨论总线 (v2.3 M7)

WebSocket 实时推送的讨论消息总线。与 EventBus 分离设计:
- EventBus → SSE (单向) → Agent 中心状态卡片
- DiscussionBus → WebSocket (双向) → 讨论面板实时对话

支持:
- Agent 逐轮发言推送
- 用户插入发言
- 讨论暂停/恢复/终止
"""
from __future__ import annotations

import asyncio
import json as json_lib
from dataclasses import dataclass, field
from typing import Callable, Optional

from loguru import logger

from app.agents.events import DiscussionTurn


@dataclass
class DiscussionSession:
    """一次多 Agent 讨论会话"""
    session_id: str
    task_id: int
    file_name: str
    turns: list[DiscussionTurn] = field(default_factory=list)
    status: str = "active"  # active | paused | concluded
    max_rounds: int = 3
    report_task_id: int = 0  # 讨论沉淀的审查报告 task_id(收尾时回填)


class DiscussionBus:
    """讨论消息总线 - 单例"""
    _instance: Optional["DiscussionBus"] = None

    def __init__(self):
        self._sessions: dict[str, DiscussionSession] = {}
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._control_callbacks: dict[str, Callable] = {}

    @classmethod
    def instance(cls) -> "DiscussionBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 会话管理 ──

    def create_session(self, session_id: str, task_id: int, file_name: str,
                       max_rounds: int = 3) -> DiscussionSession:
        session = DiscussionSession(
            session_id=session_id, task_id=task_id,
            file_name=file_name, max_rounds=max_rounds,
        )
        self._sessions[session_id] = session
        self._queues[session_id] = []
        return session

    def get_session(self, session_id: str) -> Optional[DiscussionSession]:
        return self._sessions.get(session_id)

    def close_session(self, session_id: str):
        if session_id in self._sessions:
            self._sessions[session_id].status = "concluded"
        # 通知所有订阅者结束 — 必须是 JSON 字符串, 与其它帧一致,
        # 否则 WebSocket.send_text() 会因收到 dict 抛错并中断推送任务。
        end_msg = json_lib.dumps({"type": "session_end"}, ensure_ascii=False)
        for q in self._queues.get(session_id, []):
            try:
                q.put_nowait(end_msg)
            except asyncio.QueueFull:
                pass

    def request_stop(self, session_id: str):
        """标记会话为终止 — 编排循环在下一次检查时退出。"""
        session = self._sessions.get(session_id)
        if session:
            session.status = "concluded"

    # ── 发言推送 ──

    def publish_turn(self, session_id: str, turn: DiscussionTurn):
        """推送一条发言到所有 WebSocket 订阅者"""
        session = self._sessions.get(session_id)
        if session:
            session.turns.append(turn)

        msg = json_lib.dumps({
            "type": "discuss",
            "session_id": session_id,
            "turn": turn.to_dict(),
        }, ensure_ascii=False)

        for q in list(self._queues.get(session_id, [])):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                logger.debug(f"[DiscussBus] 队列满,丢弃消息 session={session_id}")

    def publish_control(self, session_id: str, action: str, payload: dict = None):
        """推送控制消息 (暂停/恢复/轮次信息)"""
        msg = json_lib.dumps({
            "type": "control",
            "session_id": session_id,
            "action": action,
            "payload": payload or {},
        }, ensure_ascii=False)

        for q in list(self._queues.get(session_id, [])):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # ── WebSocket 订阅 ──

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        """返回队列供 WebSocket 端点消费。先回放已完成的发言。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._queues.setdefault(session_id, []).append(q)

        session = self._sessions.get(session_id)
        if session and session.turns:
            for t in session.turns:
                q.put_nowait(json_lib.dumps({
                    "type": "discuss",
                    "session_id": session_id,
                    "turn": t.to_dict(),
                }, ensure_ascii=False))

        # 重连到已结束会话: 补发终态控制帧,使前端显示「已结束」而非停留「进行中」。
        # (正常首连时 status 为 active,不会触发。)
        if session and session.status == "concluded":
            try:
                q.put_nowait(json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": "round_start",
                    "payload": {"round": session.max_rounds,
                                "total_rounds": session.max_rounds},
                }, ensure_ascii=False))
                q.put_nowait(json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": "done",
                    "payload": {"task_id": session.report_task_id},
                }, ensure_ascii=False))
            except asyncio.QueueFull:
                pass

        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue):
        queues = self._queues.get(session_id, [])
        if q in queues:
            queues.remove(q)

    # ── 控制器回调 (用于暂停/恢复/用户发言) ──

    def set_controller(self, session_id: str, callback: Callable):
        self._control_callbacks[session_id] = callback

    def send_user_input(self, session_id: str, content: str):
        cb = self._control_callbacks.get(session_id)
        if cb:
            cb("user_input", {"content": content})
        else:
            logger.warning(f"[DiscussBus] session={session_id} 无回调注册")
