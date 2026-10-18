"""讨论会话过期清理测试 — 防止 DiscussionBus/_pending/_session_owners 无限增长"""
import asyncio
import json
import time

import pytest

import app.agents.discussion_bus as bus_mod
from app.agents.discussion_bus import DiscussionBus


def _fresh_bus() -> DiscussionBus:
    bus = DiscussionBus()
    DiscussionBus._instance = bus
    return bus


class TestBusPurge:
    @pytest.mark.asyncio
    async def test_reconnect_snapshot_restores_live_round_speaker_and_stage(self):
        bus = _fresh_bus()
        session = bus.create_session("disc_snapshot", task_id=0, file_name="main.py", max_rounds=3)
        bus.publish_control(session.session_id, "round_start", {"round": 2, "total_rounds": 3})
        bus.publish_control(session.session_id, "speaker", {
            "speaker_code": "security",
            "speaker_name": "安全专家",
            "speaker_index": 2,
            "total_speakers": 5,
        })
        bus.publish_control(session.session_id, "phase", {
            "stage": "agent_thinking",
            "message": "安全专家正在组织发言",
        })

        queue = await bus.subscribe(session.session_id)
        frames = [json.loads(await queue.get()) for _ in range(queue.qsize())]
        snapshot = next(frame for frame in frames if frame.get("action") == "snapshot")

        assert snapshot["payload"] == {
            "status": "active",
            "terminal_status": "",
            "round": 2,
            "total_rounds": 3,
            "speaker_code": "security",
            "speaker_name": "安全专家",
            "speaker_index": 2,
            "total_speakers": 5,
            "stage": "agent_thinking",
            "message": "安全专家正在组织发言",
            "task_id": 0,
            "report_task_id": 0,
            "error": "",
        }

    @pytest.mark.asyncio
    async def test_cancelled_terminal_replay_preserves_cancelled_status(self):
        bus = _fresh_bus()
        session = bus.create_session("disc_cancelled_snapshot", task_id=91, file_name="main.py")
        session.terminal_status = "cancelled"
        session.terminal_error = "父级小菱运行已停止"
        bus.request_stop(session.session_id)
        bus.close_session(session.session_id)

        queue = await bus.subscribe(session.session_id)
        frames = [json.loads(await queue.get()) for _ in range(queue.qsize())]
        done = next(frame for frame in frames if frame.get("action") == "done")

        assert session.status == "cancelled"
        assert done["payload"]["status"] == "cancelled"
        assert done["payload"]["error"] == "父级小菱运行已停止"

    def test_continuation_session_keeps_lineage_without_mutating_closed_session(self):
        bus = _fresh_bus()
        closed = bus.create_session(
            "disc_original",
            task_id=0,
            file_name="main.py",
            owner_user_id=7,
            max_rounds=2,
            project_id=11,
            file_id=13,
            review_type="security",
            origin_surface="user",
            origin_session_key="chat-original",
        )
        closed.report_task_id = 71
        bus.close_session(closed.session_id)

        continuation = bus.create_session(
            "disc_continuation",
            task_id=0,
            file_name="main.py",
            owner_user_id=7,
            max_rounds=2,
            project_id=11,
            file_id=13,
            review_type="security",
            origin_surface="user",
            origin_session_key="chat-original",
            continued_from_session_id=closed.session_id,
        )

        assert closed.status == "concluded"
        assert closed.report_task_id == 71
        assert continuation.status == "active"
        assert continuation.report_task_id == 0
        assert continuation.continued_from_session_id == closed.session_id
        assert continuation.project_id == closed.project_id == 11
        assert continuation.file_id == closed.file_id == 13
        assert continuation.review_type == closed.review_type == "security"
        assert continuation.origin_surface == closed.origin_surface == "user"
        assert continuation.origin_session_key == closed.origin_session_key == "chat-original"

        legacy = bus.create_session("disc_legacy", task_id=0, file_name="legacy.py")
        assert legacy.project_id == 0
        assert legacy.file_id == 0
        assert legacy.review_type == "full"
        assert legacy.continued_from_session_id == ""

    def test_concluded_session_purged_after_ttl(self):
        bus = _fresh_bus()
        bus.create_session("disc_gc1", task_id=1, file_name="a.py")
        bus.close_session("disc_gc1")
        # 未过期: 保留(刷新页面仍可回放)
        bus._purge_expired()
        assert bus.get_session("disc_gc1") is not None

        bus.get_session("disc_gc1").closed_at = time.time() - bus_mod._CONCLUDED_SESSION_TTL - 1
        bus._purge_expired()
        assert bus.get_session("disc_gc1") is None
        assert "disc_gc1" not in bus._queues

    def test_session_with_subscriber_not_purged(self):
        import asyncio

        bus = _fresh_bus()
        bus.create_session("disc_gc2", task_id=1, file_name="a.py")
        q = asyncio.new_event_loop().run_until_complete(bus.subscribe("disc_gc2"))
        bus.close_session("disc_gc2")
        bus.get_session("disc_gc2").closed_at = time.time() - bus_mod._CONCLUDED_SESSION_TTL - 1
        bus._purge_expired()
        # 还有订阅者挂着,不清
        assert bus.get_session("disc_gc2") is not None

        bus.unsubscribe("disc_gc2", q)
        bus._purge_expired()
        assert bus.get_session("disc_gc2") is None

    def test_active_session_never_purged(self):
        bus = _fresh_bus()
        bus.create_session("disc_gc3", task_id=1, file_name="a.py")
        bus._purge_expired()
        assert bus.get_session("disc_gc3") is not None

    def test_close_session_removes_control_callback(self):
        bus = _fresh_bus()
        bus.create_session("disc_gc4", task_id=1, file_name="a.py")
        bus.set_controller("disc_gc4", lambda action, payload: None)
        bus.close_session("disc_gc4")
        assert "disc_gc4" not in bus._control_callbacks

    @pytest.mark.asyncio
    async def test_discussion_task_is_unique_and_cancellable(self):
        bus = _fresh_bus()
        bus.create_session("disc_task", task_id=1, file_name="a.py")
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def run_forever():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        first = bus.start_discussion_task("disc_task", run_forever())
        await started.wait()
        second = bus.start_discussion_task("disc_task", run_forever())

        assert second is first
        assert bus.get_discussion_task("disc_task") is first
        assert bus.cancel_discussion_task("disc_task") is True
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)
        assert cancelled.is_set()
        assert bus.get_discussion_task("disc_task") is None
        assert bus.get_session("disc_task").status == "cancelled"


class TestWsRegistryPurge:
    def test_expired_preflight_closes_its_roundtable_instead_of_leaving_it_running(self):
        from app.api.v1 import ws_discussion

        bus = _fresh_bus()
        bus.create_session("disc_preflight_expired", task_id=0, file_name="main.py", owner_user_id=7)
        ws_discussion.register_pending("disc_preflight_expired", user_id=7)
        ws_discussion._pending["disc_preflight_expired"].created_at = (
            time.time() - ws_discussion._STALE_TTL - 1
        )
        try:
            assert ws_discussion.take_pending("disc_preflight_expired") is None
            session = bus.get_session("disc_preflight_expired", owner_user_id=7)
            assert session.status == "failed"
            assert session.terminal_status == "failed"
            assert session.progress["phase"] == "failed"
            assert "未消耗讨论模型调用" in session.terminal_error
            assert "disc_preflight_expired" not in ws_discussion._session_owners
        finally:
            with ws_discussion._pending_lock:
                ws_discussion._pending.pop("disc_preflight_expired", None)
                ws_discussion._session_owners.pop("disc_preflight_expired", None)
                ws_discussion._owner_registered_at.pop("disc_preflight_expired", None)
                ws_discussion._schedule_next_pending_expiry_locked()

    def test_stale_pending_purged_on_register(self):
        from app.api.v1 import ws_discussion

        _fresh_bus()
        ws_discussion.register_pending("disc_stale", user_id=7)
        ws_discussion._pending["disc_stale"].created_at = (
            time.time() - ws_discussion._STALE_TTL - 1
        )
        ws_discussion._owner_registered_at["disc_stale"] = (
            time.time() - ws_discussion._STALE_TTL - 1
        )
        try:
            ws_discussion.register_pending("disc_fresh", user_id=8)
            assert "disc_stale" not in ws_discussion._pending
            assert "disc_stale" not in ws_discussion._session_owners
            # 新注册的不受影响
            assert ws_discussion._session_owners.get("disc_fresh") == 8
        finally:
            for sid in ("disc_stale", "disc_fresh"):
                ws_discussion._pending.pop(sid, None)
                ws_discussion._session_owners.pop(sid, None)
                ws_discussion._owner_registered_at.pop(sid, None)
            with ws_discussion._pending_lock:
                ws_discussion._schedule_next_pending_expiry_locked()

    def test_owner_kept_while_bus_session_alive(self):
        from app.api.v1 import ws_discussion

        bus = _fresh_bus()
        ws_discussion.register_pending("disc_alive", user_id=9)
        # 模拟 WS 连接消费掉 pending 且编排器已建会话
        ws_discussion._pending.pop("disc_alive", None)
        bus.create_session("disc_alive", task_id=1, file_name="a.py", owner_user_id=9)
        ws_discussion._owner_registered_at["disc_alive"] = (
            time.time() - ws_discussion._STALE_TTL - 1
        )
        try:
            ws_discussion.register_pending("disc_other", user_id=10)
            # 总线里会话还在(讨论进行中),归属记录必须保留
            assert ws_discussion._session_owners.get("disc_alive") == 9
        finally:
            for sid in ("disc_alive", "disc_other"):
                ws_discussion._pending.pop(sid, None)
                ws_discussion._session_owners.pop(sid, None)
                ws_discussion._owner_registered_at.pop(sid, None)
            with ws_discussion._pending_lock:
                ws_discussion._schedule_next_pending_expiry_locked()
