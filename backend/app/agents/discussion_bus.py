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
import threading
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


def public_roundtable_progress(progress: dict) -> dict:
    """公开进度不携带恢复所需的源码、画像和执行检查点。"""
    return {key: value for key, value in progress.items() if not key.startswith("_")}


def visible_roundtable_progress(db, row) -> dict:
    """兼容旧圆桌终态：仅凭同账号报告的覆盖账本展示部分完成。"""
    progress = public_roundtable_progress(dict(row.progress or {}))
    if row.status not in {"concluded", "failed"} or progress.get("phase") != "failed" or not row.report_task_id:
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
    expected_turns: int = 0
    completed_turns: int = 0
    participants: list[dict] = field(default_factory=list)
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
    # 线程池中的结构化抽取可能跨越 asyncio 任务取消边界；事件用于让
    # 抽取器在发起下一次模型请求前观察停止状态。
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


# 结束后的会话保留时长(秒): 期间刷新页面仍可回放全部发言,超时后随下次
# create/close 机会性清理,防止 _sessions(含全部 LLM 发言)随讨论次数无限增长
_CONCLUDED_SESSION_TTL = 3600.0
_FOLLOWUP_WINDOW_SECONDS = 300.0
_REPLAY_LIMIT = 100
_TERMINAL_SESSION_STATUSES = frozenset({
    "concluded", "partial", "failed", "cancelled", "interrupted",
})


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
                       agents: list[dict] | None = None,
                       status: str = "active") -> DiscussionSession:
        participant_list = [dict(item) for item in (agents or [])]
        expected = max_rounds * len(participant_list) + 3
        session = DiscussionSession(
            session_id=session_id, task_id=task_id,
            file_name=file_name, owner_user_id=owner_user_id, status=status,
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

    def link_report_task(self, session_id: str, task_id: int) -> bool:
        """在报告任务创建后立即绑定圆桌，确保停止与重连能找到报告。"""
        session = self._sessions.get(session_id)
        if session is None or int(task_id or 0) <= 0:
            return False
        if self._persist:
            from app.models.roundtable import RoundtableSession

            with self._session_maker().begin() as db:
                row = db.query(RoundtableSession).filter_by(
                    session_id=session_id,
                    owner_user_id=session.owner_user_id,
                ).with_for_update().populate_existing().one_or_none()
                if row is None:
                    return False
                if row.status in _TERMINAL_SESSION_STATUSES:
                    existing_ids = {
                        int(value or 0)
                        for value in (row.task_id, row.report_task_id)
                        if int(value or 0) > 0
                    }
                    terminal_status = self._terminal_status_for_row(row)
                    if (
                        any(existing_id != int(task_id) for existing_id in existing_ids)
                        or (not existing_ids and terminal_status != "cancelled")
                    ):
                        self._apply_persisted_terminal(session, row)
                        return False
                    # 只允许为取消终态补齐尚未写入的报告关联，或补齐同一报告的
                    # 缺失字段；成功/失败等其他终态不能被迟到的新报告重新指向。
                    if not int(row.task_id or 0):
                        row.task_id = int(task_id)
                    if not int(row.report_task_id or 0):
                        row.report_task_id = int(task_id)
                    row.updated_at = datetime.now(timezone.utc)
                    self._apply_persisted_terminal(session, row)
                    return False
                row.task_id = int(task_id)
                row.report_task_id = int(task_id)
                row.updated_at = datetime.now(timezone.utc)
                session.task_id = int(task_id)
                session.report_task_id = int(task_id)
        else:
            if session.status in _TERMINAL_SESSION_STATUSES or session.terminal_status:
                existing_ids = {
                    int(value or 0)
                    for value in (session.task_id, session.report_task_id)
                    if int(value or 0) > 0
                }
                if (
                    any(existing_id != int(task_id) for existing_id in existing_ids)
                    or (not existing_ids and session.terminal_status != "cancelled")
                ):
                    return False
                if not session.task_id:
                    session.task_id = int(task_id)
                if not session.report_task_id:
                    session.report_task_id = int(task_id)
                return False
            session.task_id = int(task_id)
            session.report_task_id = int(task_id)
        return True

    @staticmethod
    def _terminal_status_for_row(row) -> str:
        progress = dict(row.progress or {})
        terminal_status = str(progress.get("terminal_status") or "").lower()
        if terminal_status:
            return terminal_status
        return {
            "concluded": "interrupted" if progress.get("phase") == "interrupted" else "success",
            "partial": "partial",
            "failed": "failed",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
        }.get(str(row.status or "").lower(), "")

    @staticmethod
    def _apply_persisted_terminal(session: DiscussionSession, row) -> None:
        """让进程内旧会话服从数据库已提交的终态，避免跨 worker 复活。"""
        progress = dict(row.progress or {})
        session.task_id = int(row.task_id or session.task_id or 0)
        session.report_task_id = int(row.report_task_id or session.report_task_id or 0)
        session.status = str(row.status or session.status)
        session.terminal_status = DiscussionBus._terminal_status_for_row(row)
        session.terminal_error = str(progress.get("terminal_error") or "")[:1000]
        session.progress = progress
        session.closed_at = utc_timestamp(row.closed_at)
        session.stage = str(progress.get("stage") or session.terminal_status or session.status)
        session.stage_message = str(
            progress.get("stage_message") or session.terminal_error or "圆桌讨论已结束"
        )
        session.cancel_event.set()

    def refresh_terminal_state(self, session_id: str, *, owner_user_id: int) -> bool:
        """从持久账本刷新终态；每次模型阶段前用于跨 worker 停止检查。"""
        session = self.get_session(session_id, owner_user_id=owner_user_id)
        if session is None:
            return False
        if not self._persist:
            return session.status == "active" and not session.terminal_status

        from app.models.roundtable import RoundtableSession

        with self._session_maker()() as db:
            row = db.query(RoundtableSession).filter_by(
                session_id=session_id,
                owner_user_id=owner_user_id,
            ).one_or_none()
            if row is None:
                return False
            if row.status in _TERMINAL_SESSION_STATUSES:
                self._apply_persisted_terminal(session, row)
                return False
            return session.status == "active" and not session.terminal_status

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
        snapshot = public_roundtable_progress(session.progress)
        snapshot["turn_count"] = session.last_turn_seq
        snapshot["has_earlier"] = session.last_turn_seq > len(session.turns[-_REPLAY_LIMIT:])
        frames = [json_lib.dumps({
            "type": "control", "session_id": session_id,
            "action": "progress", "payload": snapshot,
        }, ensure_ascii=False)]
        if session.status in {"concluded", "partial", "failed", "cancelled"}:
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
            if not session.terminal_status:
                session.terminal_status = "success"
            session.status = self._session_status_for_terminal(session.terminal_status)
            if not session.closed_at:
                session.closed_at = time.time()
                if session.terminal_status == "success":
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
            session.progress = {
                **session.progress,
                "terminal_status": session.terminal_status,
                "terminal_error": session.terminal_error,
                "stage": session.stage,
                "stage_message": session.stage_message,
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

    def _update_session(self, session: DiscussionSession) -> bool:
        """持久化阶段、报告链接和终态；正文只放入 turn 表。"""
        from app.models.roundtable import RoundtableSession

        with self._session_maker().begin() as db:
            row = db.query(RoundtableSession).filter_by(
                session_id=session.session_id,
                owner_user_id=session.owner_user_id,
            ).with_for_update().populate_existing().one_or_none()
            if row is None:
                raise RuntimeError("圆桌持久会话不存在")
            if row.status in _TERMINAL_SESSION_STATUSES:
                persisted_status = self._terminal_status_for_row(row)
                if (
                    session.status != row.status
                    or (session.terminal_status and session.terminal_status != persisted_status)
                ):
                    self._apply_persisted_terminal(session, row)
                    return False
                # 终态记录是跨 worker 的权威值。只允许补齐缺失的报告关联和
                # 首次关闭时间；仅成功终态可补写追问边界。不接受旧进程覆盖
                # 已提交的终态进度或清空其他 worker 新写入的 task ID。
                if not int(row.task_id or 0) and session.task_id:
                    row.task_id = session.task_id
                if not int(row.report_task_id or 0) and session.report_task_id:
                    row.report_task_id = session.report_task_id
                if row.closed_at is None and session.closed_at:
                    row.closed_at = datetime.fromtimestamp(session.closed_at, timezone.utc)
                if row.status == "concluded" and persisted_status == "success":
                    progress = dict(row.progress or {})
                    if "followup_start_seq" in session.progress:
                        progress["followup_start_seq"] = session.progress["followup_start_seq"]
                    row.progress = progress
                row.updated_at = datetime.now(timezone.utc)
                return True
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
        return True

    def save_checkpoint(self, session_id: str, checkpoint: dict) -> None:
        """检查点失败须终止编排，不能把未保存的进度伪称可恢复。"""
        session = self._sessions[session_id]
        session.progress = {**session.progress, "_checkpoint": checkpoint}
        if self._persist and not self._update_session(session):
            raise RuntimeError("圆桌已终止，拒绝保存迟到的检查点")

    def _persist_turn(
        self, session: DiscussionSession, turn: DiscussionTurn, *, allow_followup: bool = False,
        checkpoint: dict | None = None,
    ) -> int | None:
        """给发言分配会话内唯一顺序号并提交，供分页和刷新恢复。"""
        from app.models.roundtable import RoundtableSession, RoundtableTurn

        with self._session_maker().begin() as db:
            row = db.query(RoundtableSession).filter_by(
                session_id=session.session_id,
                owner_user_id=session.owner_user_id,
            ).with_for_update().one_or_none()
            if row is None:
                raise RuntimeError("圆桌持久会话不存在或归属不匹配")
            if row.status in _TERMINAL_SESSION_STATUSES and not (
                allow_followup and row.status == "concluded"
                and self._terminal_status_for_row(row) == "success"
            ):
                self._apply_persisted_terminal(session, row)
                return None
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
            if checkpoint is not None:
                row.progress = {**dict(row.progress or {}), "_checkpoint": checkpoint}
            row.updated_at = datetime.now(timezone.utc)
        return seq

    def _restore_session(
        self, session_id: str, *, owner_user_id: int,
    ) -> Optional[DiscussionSession]:
        """从账本恢复元数据和最近发言；失去执行任务的旧会话标为中断。

        活跃圆桌执行器依赖进程内任务，因此服务必须固定为单 Uvicorn worker；
        若未来需要多 worker/副本，应先引入持久租约与存活判断，再改变此恢复语义。
        """
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
            progress = {**dict(row.progress or {}), **visible_roundtable_progress(db, row)}
            cancel_event = threading.Event()
            if row.status == "cancelled":
                cancel_event.set()
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
                progress=progress,
                last_turn_seq=int(row.last_turn_seq or 0),
                terminal_status=str(
                    progress.get("terminal_status") or self._terminal_status_for_row(row)
                ),
                terminal_error=str(progress.get("terminal_error") or ""),
                cancel_event=cancel_event,
                current_round=int(progress.get("round") or 0),
                total_rounds=int(progress.get("total_rounds") or row.max_rounds or 0),
                speaker_code=str(progress.get("speaker_code") or ""),
                speaker_name=str(progress.get("speaker_name") or ""),
                speaker_index=int(progress.get("speaker_index") or 0),
                total_speakers=int(progress.get("total_speakers") or 0),
                stage=str(progress.get("stage") or "preparing"),
                stage_message=str(progress.get("stage_message") or "圆桌正在准备"),
            )

    def request_stop(self, session_id: str, reason: str = "") -> bool:
        """持久终态允许停止时返回 True；其他终态阻止迟到的取消请求。"""
        session = self._sessions.get(session_id)
        if not session:
            return False
        # 重复 stop 对已取消任务仍可幂等地确认停止；其他终态不能被改写。
        if session.status in _TERMINAL_SESSION_STATUSES or session.terminal_status:
            if session.status == "cancelled":
                session.cancel_event.set()
                session.terminal_status = "cancelled"
                session.terminal_error = session.terminal_error or str(
                    reason or "圆桌讨论已取消"
                )[:1000]
                return True
            return False
        session.cancel_event.set()
        session.status = "cancelled"
        session.terminal_status = "cancelled"
        session.terminal_error = session.terminal_error or str(reason or "圆桌讨论已取消")[:1000]
        session.stage = "cancelled"
        session.stage_message = session.terminal_error
        session.progress = {
            **session.progress,
            "phase": "cancelled",
            "terminal_status": "cancelled",
            "terminal_error": session.terminal_error,
            "stage": session.stage,
            "stage_message": session.stage_message,
            "seq": int(session.progress.get("seq") or 0) + 1,
        }
        if self._persist and not self._update_session(session):
            return False
        return session.status == "cancelled" and session.terminal_status == "cancelled"

    @staticmethod
    def _session_status_for_terminal(terminal_status: str) -> str:
        return {
            "success": "concluded",
            "partial": "partial",
            "failed": "failed",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
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

        session = self._sessions.get(session_id)
        if session is not None and session.status in {"concluded", "partial", "failed", "cancelled"}:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError("终态圆桌不能由后台事件重新启动；请创建新的用户续会")

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

        if not self.request_stop(session_id):
            return False
        task = self.get_discussion_task(session_id)
        if task is None:
            return False
        task.cancel()
        return True

    # ── 发言推送 ──

    def publish_turn(self, session_id: str, turn: DiscussionTurn, *, checkpoint: dict | None = None):
        """推送一条发言到所有 WebSocket 订阅者"""
        session = self._sessions.get(session_id)
        if session:
            followup_start_seq = int(session.progress.get("followup_start_seq") or 0)
            pending_followup_ids = {
                turn.seq for turn in session.turns
                if turn.seq > followup_start_seq and turn.role == "user"
            } - {
                turn.turn_id for turn in session.turns
                if turn.agent_code == "orchestrator" and turn.round_index == -1
            }
            followup_turn = (
                session.status == "concluded"
                and session.terminal_status == "success"
                and (
                    (turn.role == "user" and self.can_accept_followup(session))
                    or (
                        turn.role == "agent"
                        and turn.agent_code == "orchestrator"
                        and turn.reply_to == "user"
                        and turn.round_index == -1
                        and turn.turn_id in pending_followup_ids
                    )
                )
            )
            if (
                (session.status in _TERMINAL_SESSION_STATUSES or session.terminal_status)
                and not followup_turn
            ):
                return
            if self._persist:
                persisted_seq = (
                    self._persist_turn(session, turn, allow_followup=True)
                    if followup_turn else (
                        self._persist_turn(session, turn, checkpoint=checkpoint)
                        if checkpoint is not None else self._persist_turn(session, turn)
                    )
                )
                if persisted_seq is None:
                    return
            else:
                turn.seq = session.last_turn_seq + 1
            session.last_turn_seq = turn.seq
            if checkpoint is not None:
                session.progress = {**session.progress, "_checkpoint": checkpoint}
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
            data = payload or {}
            if session.status in _TERMINAL_SESSION_STATUSES or session.terminal_status:
                # stop_discussion 会先提交 request_stop，再发 cancelled/done 通知。
                # 允许同一个终态的幂等通知到达订阅者，但绝不再应用其状态，
                # 也拒绝迟到的 success/progress/started/resumed 等事件。
                if (
                    action not in {"done", "cancelled"}
                    or not session.terminal_status
                    or str(data.get("status") or ("cancelled" if action == "cancelled" else "success"))
                    != session.terminal_status
                ):
                    return
                terminal_payload = {
                    **data,
                    "status": session.terminal_status,
                    "task_id": int(
                        session.report_task_id or data.get("task_id") or session.task_id or 0
                    ),
                    "error": session.terminal_error,
                }
                msg = json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": action, "payload": terminal_payload,
                }, ensure_ascii=False)
                progress_msg = json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": "progress", "payload": public_roundtable_progress(session.progress),
                }, ensure_ascii=False) if action == "done" else None
                for q in list(self._queues.get(session_id, [])):
                    try:
                        q.put_nowait(msg)
                        if progress_msg is not None:
                            q.put_nowait(progress_msg)
                    except asyncio.QueueFull:
                        if isinstance(q, DiscussionSubscriptionQueue):
                            q.needs_resync = True
                return

            progress = dict(session.progress)
            if action == "started":
                if session.status == "pending":
                    session.status = "active"
                session.stage = "preparing"
                session.stage_message = "圆桌正在准备"
                progress["phase"] = "preparing"
            elif action == "round_start":
                session.current_round = int(data.get("round") or 0)
                session.total_rounds = int(data.get("total_rounds") or session.max_rounds or 0)
                session.stage = "round"
                session.stage_message = f"第 {session.current_round} / {session.total_rounds} 轮"
                progress["phase"] = "speaking"
                progress["round"] = int(data.get("round") or progress.get("round") or 0)
                progress["speaker_code"] = ""
            elif action == "speaker":
                speakers = int(data.get("total_speakers") or len(session.agents) or 0)
                speaker_index = int(data.get("speaker_index") or 1)
                session.speaker_code = str(data.get("speaker_code") or "")[:80]
                session.speaker_name = str(data.get("speaker_name") or "")[:120]
                session.speaker_index = int(data.get("speaker_index") or 0)
                session.total_speakers = speakers
                session.stage = "agent_thinking"
                session.stage_message = f"{session.speaker_name or 'Agent'} 正在组织发言"
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
                if "stage" in data:
                    session.stage = str(data.get("stage") or session.stage)[:80]
                if "message" in data:
                    session.stage_message = str(data.get("message") or session.stage_message)[:240]
                if "round" in data:
                    session.current_round = int(data.get("round") or 0)
                if "total_rounds" in data:
                    session.total_rounds = int(data.get("total_rounds") or 0)
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
                progress["paused"] = True
            elif action == "resumed":
                if session.status == "paused":
                    session.status = "active"
                session.stage = "resumed"
                session.stage_message = "讨论已继续"
                progress["paused"] = False
            elif action in {"done", "cancelled"}:
                result = str(data.get("status") or ("cancelled" if action == "cancelled" else "success"))
                session.terminal_status = result
                session.terminal_error = str(data.get("error") or "")[:1000]
                session.status = self._session_status_for_terminal(result)
                session.report_task_id = int(data.get("task_id") or session.report_task_id or 0)
                session.stage = result
                session.stage_message = session.terminal_error or "圆桌讨论已结束"
                progress["phase"] = {
                    "success": "completed", "cancelled": "cancelled",
                    "failed": "partial" if data.get("partial") is True else "failed",
                    "interrupted": "interrupted",
                }.get(result, "failed")
                if result == "success":
                    progress["completed"] = int(progress.get("total") or 0)
                progress["speaker_code"] = ""
            if action in {
                "started", "round_start", "speaker", "phase", "progress", "paused", "resumed", "done", "cancelled"
            }:
                progress["seq"] = int(progress.get("seq") or 0) + 1
                progress["completed_units"] = progress.get("completed", 0)
                progress["total_units"] = progress.get("total", 0)
                progress["current_round"] = progress.get("round", 0)
                progress.update({
                    "terminal_status": session.terminal_status,
                    "terminal_error": session.terminal_error,
                    "stage": session.stage,
                    "stage_message": session.stage_message,
                    "round": session.current_round or progress.get("round", 0),
                    "total_rounds": session.total_rounds or session.max_rounds,
                    "speaker_code": session.speaker_code or progress.get("speaker_code", ""),
                    "speaker_name": session.speaker_name,
                    "speaker_index": session.speaker_index,
                    "total_speakers": session.total_speakers,
                })
                session.progress = progress
                if self._persist:
                    if not self._update_session(session):
                        return
        msg = json_lib.dumps({
            "type": "control",
            "session_id": session_id,
            "action": action,
            "payload": (
                public_roundtable_progress(session.progress) if action == "progress" and session else (payload or {})
            ),
        }, ensure_ascii=False)

        progress_msg = None
        if session and action in {"started", "round_start", "speaker", "paused", "resumed", "done"}:
            progress_msg = json_lib.dumps({
                "type": "control", "session_id": session_id,
                "action": "progress", "payload": public_roundtable_progress(session.progress),
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
        if session:
            q.put_nowait(json_lib.dumps({
                "type": "control", "session_id": session_id, "action": "snapshot",
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
            recent_turns = session.turns[-_REPLAY_LIMIT:]
            q.replay_after_seq = max(0, int(recent_turns[0].seq) - 1)
            for t in recent_turns:
                q.put_nowait(json_lib.dumps({
                    "type": "discuss",
                    "session_id": session_id,
                    "turn": t.to_dict(),
                }, ensure_ascii=False))

        if session:
            snapshot = public_roundtable_progress(session.progress)
            snapshot["turn_count"] = session.last_turn_seq
            snapshot["has_earlier"] = session.last_turn_seq > len(session.turns[-_REPLAY_LIMIT:])
            q.put_nowait(json_lib.dumps({
                "type": "control", "session_id": session_id,
                "action": "progress", "payload": snapshot,
            }, ensure_ascii=False))

        # 终态重连补进度、done 和截止时间；真实轮次已在进度快照里。
        if session and session.status in {"concluded", "partial", "failed", "cancelled"}:
            try:
                deadline = self.followup_until(session) if session.terminal_status == "success" else 0
                q.put_nowait(json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": "done",
                    "payload": {
                        "task_id": session.report_task_id,
                        "status": session.terminal_status or session.progress.get("phase", "completed"),
                        "error": session.terminal_error,
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
            and session.progress.get("phase") == "summarizing"
        ):
            # 主持汇总尚未封存时，小菱可以接管并将原文标为未核验补充。
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
            callback = self._control_callbacks.get(session_id)
            if callback:
                callback("user_input", {"content": cleaned, "turn_id": -1})
            return True
        if (
            session.status == "active"
            and session.progress.get("phase") in {"extracting", "reporting"}
        ):
            # 报告输入边界已经封存；保存为报告完成后的追问，不回写当前报告。
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
