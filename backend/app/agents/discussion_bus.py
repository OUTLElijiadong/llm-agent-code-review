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
import time
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
    owner_user_id: int = 0
    turns: list[DiscussionTurn] = field(default_factory=list)
    status: str = "active"  # active | paused | concluded
    max_rounds: int = 3
    report_task_id: int = 0  # 讨论沉淀的审查报告 task_id(收尾时回填)
    closed_at: float = 0.0  # concluded 时间戳,供过期清理判断


# 结束后的会话保留时长(秒): 期间刷新页面仍可回放全部发言,超时后随下次
# create/close 机会性清理,防止 _sessions(含全部 LLM 发言)随讨论次数无限增长
_CONCLUDED_SESSION_TTL = 3600.0


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
                       owner_user_id: int = 0,
                       max_rounds: int = 3) -> DiscussionSession:
        session = DiscussionSession(
            session_id=session_id, task_id=task_id,
            file_name=file_name, owner_user_id=owner_user_id,
            max_rounds=max_rounds,
        )
        self._sessions[session_id] = session
        self._queues[session_id] = []
        self._purge_expired()
        return session

    def get_session(self, session_id: str) -> Optional[DiscussionSession]:
        return self._sessions.get(session_id)

    def close_session(self, session_id: str):
        session = self._sessions.get(session_id)
        if session:
            session.status = "concluded"
            session.closed_at = time.time()
        # 编排循环已退出,pause/resume/user_input 回调随之失效,立即摘除
        self._control_callbacks.pop(session_id, None)
        # 通知所有订阅者结束 — 必须是 JSON 字符串, 与其它帧一致,
        # 否则 WebSocket.send_text() 会因收到 dict 抛错并中断推送任务。
        end_msg = json_lib.dumps({"type": "session_end"}, ensure_ascii=False)
        for q in self._queues.get(session_id, []):
            try:
                q.put_nowait(end_msg)
            except asyncio.QueueFull:
                pass
        self._purge_expired()

    def _purge_expired(self):
        """清理已结束且超过保留期、当前无订阅者的会话。"""
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if s.status == "concluded"
            and s.closed_at
            and now - s.closed_at > _CONCLUDED_SESSION_TTL
            and not self._queues.get(sid)
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            self._queues.pop(sid, None)
            self._control_callbacks.pop(sid, None)
            logger.debug(f"[DiscussBus] 过期会话已清理 session={sid}")

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
