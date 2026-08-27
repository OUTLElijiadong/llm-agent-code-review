"""讨论会话过期清理测试 — 防止 DiscussionBus/_pending/_session_owners 无限增长"""
import asyncio
import time

import pytest

import app.agents.discussion_bus as bus_mod
from app.agents.discussion_bus import DiscussionBus


def _fresh_bus() -> DiscussionBus:
    bus = DiscussionBus()
    DiscussionBus._instance = bus
    return bus


class TestBusPurge:
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
        assert bus.get_session("disc_task").status == "concluded"


class TestWsRegistryPurge:
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

    def test_owner_kept_while_bus_session_alive(self):
        from app.api.v1 import ws_discussion

        bus = _fresh_bus()
        ws_discussion.register_pending("disc_alive", user_id=9)
        # 模拟 WS 连接消费掉 pending 且编排器已建会话
        ws_discussion._pending.pop("disc_alive", None)
        bus.create_session("disc_alive", task_id=1, file_name="a.py")
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
