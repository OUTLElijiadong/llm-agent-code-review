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
import contextlib
import inspect
import json as json_lib
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

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
    status: str = "active"  # active | paused | concluded | partial | failed | cancelled
    terminal_status: str = ""  # success | partial | failed | cancelled
    terminal_error: str = ""
    current_round: int = 0
    total_rounds: int = 0
    speaker_code: str = ""
    speaker_name: str = ""
    speaker_index: int = 0
    total_speakers: int = 0
    stage: str = "preparing"
    stage_message: str = "圆桌正在准备"
    max_rounds: int = 3
    report_task_id: int = 0  # 讨论沉淀的审查报告 task_id(收尾时回填)
    closed_at: float = 0.0  # concluded 时间戳,供过期清理判断
    # 续会必须从真实原始审查上下文重新创建任务，不能复用旧 report_task。
    project_id: int = 0
    file_id: int = 0
    review_type: str = "full"
    # 小菱发起的圆桌结束后，要回投到同一 surface/session。
    origin_surface: str = ""
    origin_session_key: str = ""
    continued_from_session_id: str = ""


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
        self._discussion_tasks: dict[str, asyncio.Task] = {}

    @classmethod
    def instance(cls) -> "DiscussionBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 会话管理 ──

    def create_session(self, session_id: str, task_id: int, file_name: str,
                       owner_user_id: int = 0,
                       max_rounds: int = 3,
                       project_id: int = 0,
                       file_id: int = 0,
                       review_type: str = "full",
                       origin_surface: str = "",
                       origin_session_key: str = "",
                       continued_from_session_id: str = "") -> DiscussionSession:
        session = DiscussionSession(
            session_id=session_id, task_id=task_id,
            file_name=file_name, owner_user_id=owner_user_id,
            max_rounds=max_rounds,
            project_id=int(project_id or 0),
            file_id=int(file_id or 0),
            review_type=str(review_type or "full")[:50],
            origin_surface=str(origin_surface or "")[:24],
            origin_session_key=str(origin_session_key or "")[:128],
            continued_from_session_id=str(continued_from_session_id or "")[:64],
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
            if not session.terminal_status:
                session.terminal_status = "success"
            session.status = self._session_status_for_terminal(session.terminal_status)
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
            if s.status in {"concluded", "partial", "failed", "cancelled"}
            and s.closed_at
            and now - s.closed_at > _CONCLUDED_SESSION_TTL
            and not self._queues.get(sid)
            and (self._discussion_tasks.get(sid) is None or self._discussion_tasks[sid].done())
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            self._queues.pop(sid, None)
            self._control_callbacks.pop(sid, None)
            self._discussion_tasks.pop(sid, None)
            logger.debug(f"[DiscussBus] 过期会话已清理 session={sid}")

    def request_stop(self, session_id: str, reason: str = ""):
        """标记会话为终止 — 编排循环在下一次检查时退出。"""
        session = self._sessions.get(session_id)
        if session:
            session.status = "cancelled"
            session.terminal_status = "cancelled"
            session.terminal_error = (
                session.terminal_error
                or str(reason or "圆桌讨论已取消")[:1000]
            )
            session.stage = "cancelled"
            session.stage_message = session.terminal_error

    @staticmethod
    def _session_status_for_terminal(terminal_status: str) -> str:
        return {
            "success": "concluded",
            "partial": "partial",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(str(terminal_status or "").lower(), "concluded")

    def start_discussion_task(
        self,
        session_id: str,
        awaitable: Awaitable,
    ) -> asyncio.Task:
        """登记一个会话唯一的后台编排任务，供登录失效时精确取消。"""

        current = self._discussion_tasks.get(session_id)
        if current is not None and not current.done():
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            return current

        task = asyncio.create_task(awaitable, name=f"discussion:{session_id}")
        self._discussion_tasks[session_id] = task

        def discard(done: asyncio.Task) -> None:
            if self._discussion_tasks.get(session_id) is done:
                self._discussion_tasks.pop(session_id, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.exception()

        task.add_done_callback(discard)
        return task

    def get_discussion_task(self, session_id: str) -> Optional[asyncio.Task]:
        """返回当前会话仍在运行的编排任务。"""

        task = self._discussion_tasks.get(session_id)
        return task if task is not None and not task.done() else None

    def cancel_discussion_task(self, session_id: str) -> bool:
        """终止会话并取消其后台编排任务。"""

        self.request_stop(session_id)
        task = self.get_discussion_task(session_id)
        if task is None:
            return False
        task.cancel()
        return True

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
        session = self._sessions.get(session_id)
        data = payload or {}
        if session:
            if action == "round_start":
                session.current_round = int(data.get("round") or 0)
                session.total_rounds = int(data.get("total_rounds") or session.max_rounds or 0)
                session.speaker_code = ""
                session.speaker_name = ""
                session.speaker_index = 0
                session.total_speakers = 0
                session.stage = "round"
                session.stage_message = f"第 {session.current_round} / {session.total_rounds} 轮"
            elif action == "speaker":
                session.speaker_code = str(data.get("speaker_code") or "")[:80]
                session.speaker_name = str(data.get("speaker_name") or "")[:120]
                session.speaker_index = int(data.get("speaker_index") or 0)
                session.total_speakers = int(data.get("total_speakers") or 0)
                session.stage = "agent_thinking"
                session.stage_message = f"{session.speaker_name or 'Agent'} 正在组织发言"
            elif action == "phase":
                session.stage = str(data.get("stage") or session.stage)[:80]
                session.stage_message = str(data.get("message") or session.stage_message)[:240]
                if "round" in data:
                    session.current_round = int(data.get("round") or 0)
                if "total_rounds" in data:
                    session.total_rounds = int(data.get("total_rounds") or 0)
            elif action == "paused":
                session.status = "paused"
                session.stage = "paused"
                session.stage_message = "讨论已暂停"
            elif action == "resumed":
                if session.status == "paused":
                    session.status = "active"
                session.stage = "resumed"
                session.stage_message = "讨论已继续"
            elif action in {"done", "cancelled"}:
                terminal_status = str(data.get("status") or ("cancelled" if action == "cancelled" else "success"))
                session.terminal_status = terminal_status
                session.terminal_error = str(data.get("error") or "")[:1000]
                session.status = self._session_status_for_terminal(terminal_status)
                session.report_task_id = int(data.get("task_id") or session.report_task_id or 0)
                session.stage = terminal_status
                session.stage_message = session.terminal_error or "圆桌讨论已结束"
        msg = json_lib.dumps({
            "type": "control",
            "session_id": session_id,
            "action": action,
            "payload": data,
        }, ensure_ascii=False)

        for q in list(self._queues.get(session_id, [])):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # ── WebSocket 订阅 ──

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        """返回队列供 WebSocket 端点消费。先回放已完成的发言。"""
        session = self._sessions.get(session_id)
        q: asyncio.Queue = asyncio.Queue(maxsize=max(128, len(session.turns) + 3 if session else 128))
        self._queues.setdefault(session_id, []).append(q)

        if session:
            q.put_nowait(json_lib.dumps({
                "type": "control",
                "session_id": session_id,
                "action": "snapshot",
                "payload": {
                    "status": session.status,
                    "terminal_status": session.terminal_status,
                    "round": session.current_round,
                    "total_rounds": session.total_rounds or session.max_rounds,
                    "speaker_code": session.speaker_code,
                    "speaker_name": session.speaker_name,
                    "speaker_index": session.speaker_index,
                    "total_speakers": session.total_speakers,
                    "stage": session.stage,
                    "message": session.stage_message,
                    "task_id": session.task_id,
                    "report_task_id": session.report_task_id,
                    "error": session.terminal_error,
                },
            }, ensure_ascii=False))

        if session and session.turns:
            for t in session.turns:
                q.put_nowait(json_lib.dumps({
                    "type": "discuss",
                    "session_id": session_id,
                    "turn": t.to_dict(),
                }, ensure_ascii=False))

        # 重连到已结束会话: 补发终态控制帧,使前端显示「已结束」而非停留「进行中」。
        # (正常首连时 status 为 active,不会触发。)
        if session and session.status in {"concluded", "partial", "failed", "cancelled"}:
            try:
                q.put_nowait(json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": "done",
                    "payload": {
                        "task_id": session.report_task_id,
                        "status": session.terminal_status or "success",
                        "error": session.terminal_error,
                    },
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

    def send_user_input(self, session_id: str, content: str) -> bool:
        cb = self._control_callbacks.get(session_id)
        if cb:
            cb("user_input", {"content": content})
            return True
        else:
            logger.warning(f"[DiscussBus] session={session_id} 无回调注册")
            return False

    def control_session(self, session_id: str, action: str) -> bool:
        """向已启动且仍在运行的讨论发送暂停、恢复或停止指令。"""
        if action not in {"pause", "resume", "stop"}:
            return False
        cb = self._control_callbacks.get(session_id)
        if cb is None:
            return False
        cb(action, {"session_id": session_id})
        return True
