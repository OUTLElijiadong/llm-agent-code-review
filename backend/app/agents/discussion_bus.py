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
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from loguru import logger

from app.agents.events import DiscussionTurn


def utc_timestamp(value: datetime | None) -> float:
    """数据库驱动可能返回无时区的 UTC 时间，不能按服务器本地时区解释。"""
    if value is None:
        return 0.0
    return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()


def interrupt_orphan_roundtable(db, row) -> None:
    """在同一事务中终结丢失执行器的圆桌及其报告任务。"""
    from app.models.review_task import ReviewTask

    if row.status not in {"active", "paused"}:
        return
    now = datetime.now(timezone.utc)
    progress = dict(row.progress or {})
    progress["phase"] = "interrupted"
    progress["seq"] = int(progress.get("seq") or 0) + 1
    row.progress = progress
    row.status = "concluded"
    row.closed_at = now
    row.updated_at = now
    task = db.get(ReviewTask, int(row.task_id or 0)) if row.task_id else None
    if task is not None and task.status == "running" and task.review_type == "discuss":
        task.status = "failed"
        task.error_message = "圆桌执行进程重启，任务已中断；请重新发起"
        task.end_time = now
        task.coverage = {**(task.coverage or {}), "stage": "interrupted"}


def visible_roundtable_progress(db, row) -> dict:
    """兼容旧圆桌终态：仅凭同账号报告的覆盖账本展示部分完成。"""
    progress = dict(row.progress or {})
    if row.status != "concluded" or progress.get("phase") != "failed" or not row.report_task_id:
        return progress
    from app.models.review_task import ReviewTask

    report = db.get(ReviewTask, int(row.report_task_id))
    if (report is not None and report.user_id == row.owner_user_id
            and report.review_type == "discuss" and report.status == "failed"
            and (report.coverage or {}).get("stage") == "partial"):
        progress["phase"] = "partial"
    return progress


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
    # 续会必须从真实原始审查上下文重新创建任务，不能复用旧 report_task。
    project_id: int = 0
    file_id: int = 0
    review_type: str = "full"
    # 小菱发起的圆桌结束后，要回投到同一 surface/session。
    origin_surface: str = ""
    origin_session_key: str = ""
    continued_from_session_id: str = ""
    agents: list[dict] = field(default_factory=list)
    progress: dict = field(default_factory=dict)
    last_turn_seq: int = 0


# 结束后的会话保留时长(秒): 期间刷新页面仍可回放全部发言,超时后随下次
# create/close 机会性清理,防止 _sessions(含全部 LLM 发言)随讨论次数无限增长
_CONCLUDED_SESSION_TTL = 3600.0
_FOLLOWUP_WINDOW_SECONDS = 300.0
_REPLAY_LIMIT = 100


class DiscussionSubscriptionQueue(asyncio.Queue[str]):
    """有界订阅队列；溢出时由 WS 从持久账本按 seq 修复。"""

    def __init__(self) -> None:
        super().__init__(maxsize=128)
        self.replay_after_seq = 0
        self.needs_resync = False


class DiscussionBus:
    """讨论消息总线 - 单例"""
    _instance: Optional["DiscussionBus"] = None

    def __init__(self, *, persist: bool = False, session_factory: Callable | None = None):
        self._sessions: dict[str, DiscussionSession] = {}
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._control_callbacks: dict[str, Callable] = {}
        self._discussion_tasks: dict[str, asyncio.Task] = {}
        # 直接实例化供纯单元测试使用；生产单例启用数据库账本。
        self._persist = persist
        self._session_factory = session_factory

    @classmethod
    def instance(cls) -> "DiscussionBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def enable_persistence(self, session_factory: Callable) -> None:
        """由已鉴权 REST 请求注入同一数据库绑定，避免全局隐式连库。"""
        self._session_factory = session_factory
        self._persist = True

    def _session_maker(self) -> Callable:
        if self._session_factory is not None:
            return self._session_factory
        from app.core.database import SessionLocal

        return SessionLocal

    # ── 会话管理 ──

    def create_session(self, session_id: str, task_id: int, file_name: str,
                       owner_user_id: int = 0,
                       max_rounds: int = 3,
                       project_id: int = 0,
                       file_id: int = 0,
                       review_type: str = "full",
                       origin_surface: str = "",
                       origin_session_key: str = "",
                       continued_from_session_id: str = "",
                       agents: list[dict] | None = None) -> DiscussionSession:
        participant_list = [dict(item) for item in (agents or [])]
        expected = max_rounds * len(participant_list) + 3
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
            agents=participant_list,
            progress={
                "phase": "pending", "completed": 0, "total": expected,
                "round": 0, "speaker_code": "", "seq": 0,
                "completed_units": 0, "total_units": expected, "current_round": 0,
            },
        )
        if self._persist:
            self._insert_session(session)
        self._sessions[session_id] = session
        self._queues[session_id] = []
        self._purge_expired()
        return session

    def get_session(
        self, session_id: str, *, owner_user_id: int | None = None,
    ) -> Optional[DiscussionSession]:
        session = self._sessions.get(session_id)
        if session is not None and owner_user_id is not None and session.owner_user_id != owner_user_id:
            return None
        # 未缓存的持久会话只有在明确归属后才能恢复；恢复运行中会话会写入
        # interrupted 状态，因此绝不能让未知账号的会话 ID 触发该副作用。
        if session is None and self._persist and owner_user_id is not None:
            session = self._restore_session(session_id, owner_user_id=owner_user_id)
            if session is not None:
                self._sessions[session_id] = session
                self._queues.setdefault(session_id, [])
        return session

    def get_turns_page(
        self,
        session_id: str,
        owner_user_id: int,
        *,
        limit: int = 100,
        before_seq: int | None = None,
    ) -> dict:
        """按持久顺序号分页，老页从数据库取而非从 100 条内存回放猜测。"""
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        if before_seq is not None and before_seq < 1:
            raise ValueError("before_seq 必须为正整数")
        session = self.get_session(session_id, owner_user_id=owner_user_id)
        if session is None:
            raise PermissionError("圆桌讨论不存在或无权访问")

        if self._persist:
            from app.models.roundtable import RoundtableSession, RoundtableTurn

            with self._session_maker()() as db:
                row = db.query(RoundtableSession).filter_by(
                    session_id=session_id, owner_user_id=owner_user_id,
                ).one_or_none()
                if row is not None:
                    query = db.query(RoundtableTurn).filter_by(
                        session_id=session_id, owner_user_id=owner_user_id,
                    )
                    if before_seq is not None:
                        query = query.filter(RoundtableTurn.seq < before_seq)
                    page = query.order_by(RoundtableTurn.seq.desc()).limit(limit + 1).all()
                    return {
                        "turns": [DiscussionTurn(**dict(item.turn)) for item in reversed(page[:limit])],
                        "total": int(row.last_turn_seq or 0),
                        "has_more": len(page) > limit,
                        "next_before_seq": int(page[limit - 1].seq) if len(page) > limit else None,
                    }

        ordered = [turn for turn in session.turns if before_seq is None or turn.seq < before_seq]
        page = ordered[-limit:]
        return {
            "turns": page,
            "total": session.last_turn_seq,
            "has_more": len(ordered) > limit,
            "next_before_seq": page[0].seq if len(ordered) > limit and page else None,
        }

    def get_turns_after(
        self, session_id: str, owner_user_id: int, *, after_seq: int, limit: int = 100,
    ) -> list[DiscussionTurn]:
        """供 WS 补发缺口，按 seq 正序读取持久账本；每次只取有界一页。"""
        if after_seq < 0 or not 1 <= limit <= 100:
            raise ValueError("无效的圆桌发言游标或页大小")
        session = self.get_session(session_id, owner_user_id=owner_user_id)
        if session is None:
            raise PermissionError("圆桌讨论不存在或无权访问")
        if self._persist:
            from app.models.roundtable import RoundtableTurn

            with self._session_maker()() as db:
                rows = db.query(RoundtableTurn).filter(
                    RoundtableTurn.session_id == session_id,
                    RoundtableTurn.owner_user_id == owner_user_id,
                    RoundtableTurn.seq > after_seq,
                ).order_by(RoundtableTurn.seq.asc()).limit(limit).all()
                return [DiscussionTurn(**dict(row.turn)) for row in rows]
        return [turn for turn in session.turns if turn.seq > after_seq][:limit]

    def recovery_frames(self, session_id: str, owner_user_id: int) -> list[str]:
        """缺口补齐后发送当前进度和终态，避免满队列丢失 done。"""
        session = self.get_session(session_id, owner_user_id=owner_user_id)
        if session is None:
            raise PermissionError("圆桌讨论不存在或无权访问")
        snapshot = dict(session.progress)
        snapshot["turn_count"] = session.last_turn_seq
        snapshot["has_earlier"] = session.last_turn_seq > len(session.turns[-_REPLAY_LIMIT:])
        frames = [json_lib.dumps({
            "type": "control", "session_id": session_id,
            "action": "progress", "payload": snapshot,
        }, ensure_ascii=False)]
        if session.status == "concluded":
            frames.append(json_lib.dumps({
                "type": "control", "session_id": session_id,
                "action": "done",
                "payload": {
                    "task_id": session.report_task_id,
                    "status": session.progress.get("phase", "completed"),
                    "followup_until": self.followup_until(session),
                },
            }, ensure_ascii=False))
            frames.append(json_lib.dumps({
                "type": "session_end", "followup_until": self.followup_until(session),
            }, ensure_ascii=False))
        return frames

    @staticmethod
    def followup_until(session: DiscussionSession) -> float:
        """正常生成报告后固定开放五分钟；取消、失败和中断不可追问。"""
        if (session.status == "concluded" and session.closed_at > 0
                and session.report_task_id > 0
                and "followup_start_seq" in session.progress
                and session.progress.get("phase") == "completed"):
            return session.closed_at + _FOLLOWUP_WINDOW_SECONDS
        return 0.0

    def can_accept_followup(self, session: DiscussionSession, *, now: float | None = None) -> bool:
        deadline = self.followup_until(session)
        return deadline > 0 and (time.time() if now is None else now) < deadline

    def close_session(self, session_id: str):
        session = self._sessions.get(session_id)
        if session:
            session.status = "concluded"
            if not session.closed_at:
                session.closed_at = time.time()
                session.progress = {
                    **session.progress,
                    # 报告整理期间接收的消息尚未进入不可变的报告输入；
                    # 正常完成后由主持人逐条回应，不能在关闭时跳过这些消息。
                    "followup_start_seq": int(
                        session.progress["finalizing_input_start_seq"]
                        if "finalizing_input_start_seq" in session.progress
                        else session.last_turn_seq
                    ),
                }
            if self._persist:
                self._update_session(session)
        # 编排循环已退出,pause/resume/user_input 回调随之失效,立即摘除
        self._control_callbacks.pop(session_id, None)
        # 通知所有订阅者结束 — 必须是 JSON 字符串, 与其它帧一致,
        # 否则 WebSocket.send_text() 会因收到 dict 抛错并中断推送任务。
        end_msg = json_lib.dumps({
            "type": "session_end",
            "followup_until": self.followup_until(session) if session else 0,
        }, ensure_ascii=False)
        for q in self._queues.get(session_id, []):
            try:
                q.put_nowait(end_msg)
            except asyncio.QueueFull:
                if isinstance(q, DiscussionSubscriptionQueue):
                    q.needs_resync = True
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
            and (self._discussion_tasks.get(sid) is None or self._discussion_tasks[sid].done())
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            self._queues.pop(sid, None)
            self._control_callbacks.pop(sid, None)
            self._discussion_tasks.pop(sid, None)
            logger.debug(f"[DiscussBus] 过期会话已清理 session={sid}")

    def _insert_session(self, session: DiscussionSession) -> None:
        """先持久化归属与可重开元数据，成功后才向调用方返回会话。"""
        from app.models.roundtable import RoundtableSession

        with self._session_maker().begin() as db:
            db.add(RoundtableSession(
                session_id=session.session_id,
                owner_user_id=session.owner_user_id,
                task_id=session.task_id,
                project_id=session.project_id,
                file_id=session.file_id,
                file_name=session.file_name,
                review_type=session.review_type,
                status=session.status,
                max_rounds=session.max_rounds,
                report_task_id=session.report_task_id,
                agents=session.agents,
                progress=dict(session.progress),
                last_turn_seq=0,
                origin_surface=session.origin_surface,
                origin_session_key=session.origin_session_key,
                continued_from_session_id=session.continued_from_session_id,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ))

    def _update_session(self, session: DiscussionSession) -> None:
        """持久化阶段、报告链接和终态；正文只放入 turn 表。"""
        from app.models.roundtable import RoundtableSession

        with self._session_maker().begin() as db:
            row = db.get(RoundtableSession, session.session_id)
            if row is None:
                raise RuntimeError("圆桌持久会话不存在")
            row.task_id = session.task_id
            row.status = session.status
            row.report_task_id = session.report_task_id
            row.progress = dict(session.progress)
            row.last_turn_seq = session.last_turn_seq
            row.updated_at = datetime.now(timezone.utc)
            row.closed_at = (
                datetime.fromtimestamp(session.closed_at, timezone.utc)
                if session.closed_at else None
            )

    def _persist_turn(self, session: DiscussionSession, turn: DiscussionTurn) -> int:
        """给发言分配会话内唯一顺序号并提交，供分页和刷新恢复。"""
        from app.models.roundtable import RoundtableSession, RoundtableTurn

        with self._session_maker().begin() as db:
            row = db.query(RoundtableSession).filter_by(
                session_id=session.session_id,
                owner_user_id=session.owner_user_id,
            ).with_for_update().one_or_none()
            if row is None:
                raise RuntimeError("圆桌持久会话不存在或归属不匹配")
            seq = int(row.last_turn_seq or 0) + 1
            turn.seq = seq
            db.add(RoundtableTurn(
                session_id=session.session_id,
                seq=seq,
                owner_user_id=session.owner_user_id,
                turn=turn.to_dict(),
                created_at=datetime.now(timezone.utc),
            ))
            row.last_turn_seq = seq
            row.updated_at = datetime.now(timezone.utc)
        return seq

    def _restore_session(
        self, session_id: str, *, owner_user_id: int,
    ) -> Optional[DiscussionSession]:
        """从账本恢复元数据和最近发言；失去执行任务的旧会话标为中断。"""
        from app.models.roundtable import RoundtableSession, RoundtableTurn

        with self._session_maker().begin() as db:
            row = db.query(RoundtableSession).filter_by(
                session_id=session_id, owner_user_id=owner_user_id,
            ).one_or_none()
            if row is None:
                return None
            if row.status in {"active", "paused"}:
                interrupt_orphan_roundtable(db, row)
            persisted_turns = db.query(RoundtableTurn).filter_by(
                session_id=session_id,
                owner_user_id=row.owner_user_id,
            ).order_by(RoundtableTurn.seq.desc()).limit(_REPLAY_LIMIT).all()
            return DiscussionSession(
                session_id=row.session_id,
                task_id=int(row.task_id or 0),
                file_name=row.file_name,
                owner_user_id=int(row.owner_user_id),
                turns=[DiscussionTurn(**dict(item.turn)) for item in reversed(persisted_turns)],
                status=row.status,
                max_rounds=int(row.max_rounds),
                report_task_id=int(row.report_task_id or 0),
                closed_at=utc_timestamp(row.closed_at),
                project_id=int(row.project_id or 0),
                file_id=int(row.file_id or 0),
                review_type=row.review_type,
                origin_surface=row.origin_surface,
                origin_session_key=row.origin_session_key,
                continued_from_session_id=row.continued_from_session_id,
                agents=list(row.agents or []),
                progress=visible_roundtable_progress(db, row),
                last_turn_seq=int(row.last_turn_seq or 0),
            )

    def request_stop(self, session_id: str):
        """标记会话为终止 — 编排循环在下一次检查时退出。"""
        session = self._sessions.get(session_id)
        if session:
            session.status = "concluded"
            if self._persist:
                self._update_session(session)

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
            if self._persist:
                self._persist_turn(session, turn)
            else:
                turn.seq = session.last_turn_seq + 1
            session.last_turn_seq = turn.seq
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
                if isinstance(q, DiscussionSubscriptionQueue):
                    q.needs_resync = True
                logger.debug(f"[DiscussBus] 队列满,等待账本补发 session={session_id}")

    def publish_control(self, session_id: str, action: str, payload: dict = None):
        """推送控制消息 (暂停/恢复/轮次信息)"""
        session = self._sessions.get(session_id)
        if session:
            progress = dict(session.progress)
            data = payload or {}
            if action == "round_start":
                progress["phase"] = "speaking"
                progress["round"] = int(data.get("round") or progress.get("round") or 0)
                progress["speaker_code"] = ""
            elif action == "speaker":
                speakers = int(data.get("total_speakers") or len(session.agents) or 0)
                speaker_index = int(data.get("speaker_index") or 1)
                progress["phase"] = "speaking"
                progress["speaker_code"] = str(data.get("speaker_code") or "")
                progress["completed"] = max(
                    int(progress.get("completed") or 0),
                    (max(int(progress.get("round") or 1), 1) - 1) * speakers + speaker_index - 1,
                )
                progress["total"] = max(int(progress.get("total") or 0), session.max_rounds * speakers + 3)
            elif action == "progress":
                for source, target in (
                    ("phase", "phase"), ("speaker_code", "speaker_code"),
                    ("completed", "completed"), ("completed_units", "completed"),
                    ("total", "total"), ("total_units", "total"),
                    ("round", "round"), ("current_round", "round"),
                ):
                    if source in data:
                        progress[target] = data[source]
            elif action == "paused":
                progress["paused"] = True
            elif action == "resumed":
                progress["paused"] = False
            elif action == "done":
                result = str(data.get("status") or "success")
                progress["phase"] = {
                    "success": "completed", "cancelled": "cancelled",
                    "failed": "partial" if data.get("partial") is True else "failed",
                    "interrupted": "interrupted",
                }.get(result, "failed")
                if result == "success":
                    progress["completed"] = int(progress.get("total") or 0)
                progress["speaker_code"] = ""
            if action in {"round_start", "speaker", "progress", "paused", "resumed", "done"}:
                progress["seq"] = int(progress.get("seq") or 0) + 1
                progress["completed_units"] = progress.get("completed", 0)
                progress["total_units"] = progress.get("total", 0)
                progress["current_round"] = progress.get("round", 0)
                session.progress = progress
                if self._persist:
                    self._update_session(session)
        msg = json_lib.dumps({
            "type": "control",
            "session_id": session_id,
            "action": action,
            "payload": dict(session.progress) if action == "progress" and session else (payload or {}),
        }, ensure_ascii=False)

        progress_msg = None
        if session and action in {"round_start", "speaker", "paused", "resumed", "done"}:
            progress_msg = json_lib.dumps({
                "type": "control", "session_id": session_id,
                "action": "progress", "payload": dict(session.progress),
            }, ensure_ascii=False)
        for q in list(self._queues.get(session_id, [])):
            try:
                q.put_nowait(msg)
                if progress_msg is not None:
                    q.put_nowait(progress_msg)
            except asyncio.QueueFull:
                if isinstance(q, DiscussionSubscriptionQueue):
                    q.needs_resync = True

    # ── WebSocket 订阅 ──

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        """返回队列供 WebSocket 端点消费。先回放已完成的发言。"""
        q = DiscussionSubscriptionQueue()
        self._queues.setdefault(session_id, []).append(q)

        session = self.get_session(session_id)
        if session and session.turns:
            recent_turns = session.turns[-_REPLAY_LIMIT:]
            q.replay_after_seq = max(0, int(recent_turns[0].seq) - 1)
            for t in recent_turns:
                q.put_nowait(json_lib.dumps({
                    "type": "discuss",
                    "session_id": session_id,
                    "turn": t.to_dict(),
                }, ensure_ascii=False))

        if session:
            snapshot = dict(session.progress)
            snapshot["turn_count"] = session.last_turn_seq
            snapshot["has_earlier"] = session.last_turn_seq > len(session.turns[-_REPLAY_LIMIT:])
            q.put_nowait(json_lib.dumps({
                "type": "control", "session_id": session_id,
                "action": "progress", "payload": snapshot,
            }, ensure_ascii=False))

        # 终态重连补进度、done 和截止时间；真实轮次已在进度快照里。
        if session and session.status == "concluded":
            try:
                deadline = self.followup_until(session)
                q.put_nowait(json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": "done",
                    "payload": {
                        "task_id": session.report_task_id,
                        "status": session.progress.get("phase", "completed"),
                        "followup_until": deadline,
                    },
                }, ensure_ascii=False))
                q.put_nowait(json_lib.dumps({
                    "type": "session_end", "followup_until": deadline,
                }, ensure_ascii=False))
            except asyncio.QueueFull:
                q.needs_resync = True

        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue):
        queues = self._queues.get(session_id, [])
        if q in queues:
            queues.remove(q)

    # ── 控制器回调 (用于暂停/恢复/用户发言) ──

    def set_controller(self, session_id: str, callback: Callable):
        self._control_callbacks[session_id] = callback

    def send_user_input(self, session_id: str, content: str) -> bool:
        """兼容旧控制器调用；新入口应使用 accept_user_input。"""
        session = self.get_session(session_id)
        if (
            not session or session.status != "active" or not content.strip()
            or session.progress.get("phase") not in {"pending", "speaking"}
        ):
            return False
        cb = self._control_callbacks.get(session_id)
        if cb:
            cb("user_input", {"content": content})
            return True
        else:
            logger.warning(f"[DiscussBus] session={session_id} 无回调注册")
            return False

    def accept_user_input(self, session_id: str, content: str) -> bool:
        """先验证并持久化；正式结束后的五分钟追问由独立主持人处理。"""
        cleaned = content.strip()
        session = self.get_session(session_id)
        if not cleaned or session is None:
            return False
        if self.can_accept_followup(session):
            self.publish_turn(session_id, DiscussionTurn(
                turn_id=-1, agent_code="user", agent_name="你",
                role="user", content=cleaned,
            ))
            return True
        if (
            session.status == "active"
            and session.progress.get("phase") in {"summarizing", "extracting", "reporting"}
        ):
            # 这一阶段的报告使用固定证据快照。迟到消息先入账，报告成功
            # 后再由主持人用完整账本回答；不假称已并入当前报告。
            if "finalizing_input_start_seq" not in session.progress:
                session.progress = {
                    **session.progress,
                    "finalizing_input_start_seq": session.last_turn_seq,
                }
                if self._persist:
                    self._update_session(session)
            self.publish_turn(session_id, DiscussionTurn(
                turn_id=-1, agent_code="user", agent_name="你",
                role="user", content=cleaned,
            ))
            return True
        callback = self._control_callbacks.get(session_id)
        if (
            session.status != "active"
            or session.progress.get("phase") not in {"pending", "speaking"}
            or callback is None
        ):
            return False
        self.publish_turn(session_id, DiscussionTurn(
            turn_id=-1,
            agent_code="user",
            agent_name="你",
            role="user",
            content=cleaned,
        ))
        callback("user_input", {"content": cleaned})
        return True

    def control_session(self, session_id: str, action: str) -> bool:
        """向已启动且仍在运行的讨论发送暂停、恢复或停止指令。"""
        if action not in {"pause", "resume", "stop"}:
            return False
        cb = self._control_callbacks.get(session_id)
        if cb is None:
            return False
        cb(action, {"session_id": session_id})
        return True
