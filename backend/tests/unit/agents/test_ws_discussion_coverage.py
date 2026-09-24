"""讨论 WebSocket 鉴权、消息协议与资源清理补充测试。"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import WebSocketDisconnect

from app.api.v1 import ws_discussion as module

_ORIGINAL_ASYNCIO_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _restore_ws_registries() -> Iterator[None]:
    """隔离并恢复 pending、owner 与 owner 注册时间三份模块状态。"""
    old_pending = dict(module._pending)
    old_owners = dict(module._session_owners)
    old_registered = dict(module._owner_registered_at)
    module._pending.clear()
    module._session_owners.clear()
    module._owner_registered_at.clear()
    try:
        yield
    finally:
        module._pending.clear()
        module._pending.update(old_pending)
        module._session_owners.clear()
        module._session_owners.update(old_owners)
        module._owner_registered_at.clear()
        module._owner_registered_at.update(old_registered)


class FakeWebSocket:
    """提供 ws_discuss 所需的最小 WebSocket 行为与调用记录。"""

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        query: bytes = b"",
        incoming: list[str | BaseException] | None = None,
        connected: bool = False,
    ) -> None:
        """初始化请求元数据、入站帧和出站调用记录。"""
        self.headers = headers or {}
        self.scope = {"query_string": query}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.client_state = SimpleNamespace(value=1 if connected else 0)
        self.incoming = list(incoming or [])
        self.closed: list[tuple[int, str]] = []
        self.accepted: list[str | None] = []
        self.sent: list[str] = []

    async def close(self, code: int, reason: str) -> None:
        """记录服务端拒绝连接的关闭码与原因。"""
        self.closed.append((code, reason))

    async def accept(self, subprotocol: str | None = None) -> None:
        """记录握手接受时选择的子协议。"""
        self.accepted.append(subprotocol)

    async def send_text(self, message: str) -> None:
        """记录服务端发往客户端的文本帧。"""
        self.sent.append(message)

    async def receive_text(self) -> str:
        """逐条返回入站帧，耗尽后模拟客户端正常断线。"""
        await _ORIGINAL_ASYNCIO_SLEEP(0)
        if not self.incoming:
            raise WebSocketDisconnect(code=1000)
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeDiscussionBus:
    """记录讨论总线订阅、广播、控制与清理行为。"""

    def __init__(self, session: Any = None, outbound: list[str] | None = None) -> None:
        """初始化可选既有会话和预置下行消息。"""
        self.session = session
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        for message in outbound or []:
            self.queue.put_nowait(message)
        self.controls: list[tuple[str, str, dict[str, Any]]] = []
        self.turns: list[tuple[str, Any]] = []
        self.user_inputs: list[tuple[str, str]] = []
        self.unsubscribed: list[tuple[str, asyncio.Queue[str]]] = []
        self._control_callbacks: dict[str, Any] = {}
        self.discussion_tasks: dict[str, asyncio.Task] = {}
        self.cancelled_sessions: list[str] = []

    def get_session(self, session_id: str, *, owner_user_id: int | None = None) -> Any:
        """返回为测试配置的既有会话。"""
        return self.session

    async def subscribe(self, session_id: str) -> asyncio.Queue[str]:
        """返回预置下行消息的订阅队列。"""
        return self.queue

    def publish_control(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """记录控制广播。"""
        self.controls.append((session_id, action, payload or {}))

    def publish_turn(self, session_id: str, turn: Any) -> None:
        """记录用户发言广播。"""
        self.turns.append((session_id, turn))

    def send_user_input(self, session_id: str, content: str) -> bool:
        """记录传给讨论编排器的用户输入。"""
        self.user_inputs.append((session_id, content))
        return True

    def accept_user_input(self, session_id: str, content: str) -> bool:
        """模拟先确认编排器接收，再公开用户发言的原子入口。"""
        if not self.send_user_input(session_id, content.strip()):
            return False
        from app.agents.events import DiscussionTurn

        self.publish_turn(session_id, DiscussionTurn(
            turn_id=-1, agent_code="user", agent_name="你",
            role="user", content=content.strip(),
        ))
        return True

    def control_session(self, session_id: str, action: str) -> bool:
        """按真实总线契约把控制命令转发给已注册的编排器。"""
        callback = self._control_callbacks.get(session_id)
        if callback is None:
            return False
        callback(action, {"session_id": session_id})
        return True

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[str]) -> None:
        """记录连接断开后的订阅释放。"""
        self.unsubscribed.append((session_id, queue))

    def start_discussion_task(self, session_id: str, awaitable: Any) -> asyncio.Task:
        """按真实总线契约登记测试后台任务。"""
        task = asyncio.create_task(awaitable)
        self.discussion_tasks[session_id] = task
        return task

    def request_stop(self, session_id: str) -> None:
        """记录会话停止请求。"""
        self.cancelled_sessions.append(session_id)

    def cancel_discussion_task(self, session_id: str) -> bool:
        """取消测试后台任务并记录会话。"""
        self.cancelled_sessions.append(session_id)
        task = self.discussion_tasks.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True


def _install_bus(monkeypatch: pytest.MonkeyPatch, bus: FakeDiscussionBus) -> None:
    """把 DiscussionBus 单例替换为当前测试的 fake 实例。"""
    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(lambda cls: bus))


def _user(user_id: int, *, role: str = "user", status: int = 1) -> SimpleNamespace:
    """构造 WebSocket 鉴权所需的轻量用户对象。"""
    return SimpleNamespace(id=user_id, role=role, status=status)


def test_token_helpers_parse_subprotocol_and_query() -> None:
    """子协议 token 应优先按声明位置解析，query token 保持旧客户端兼容。"""
    websocket = FakeWebSocket(
        headers={"sec-websocket-protocol": "chat, prism-auth, jwt-123"},
        query=b"token=query-token&x=1",
    )
    assert module._has_auth_subprotocol(websocket) is True
    assert module._token_from_subprotocol(websocket) == "jwt-123"
    assert module._token_from_query(websocket) == "query-token"

    missing = FakeWebSocket(headers={"sec-websocket-protocol": "prism-auth"})
    assert module._token_from_subprotocol(missing) is None
    assert module._token_from_query(missing) is None
    assert module._has_auth_subprotocol(FakeWebSocket()) is False


def test_load_active_user_and_ws_user_close_database_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户加载 helper 应拒绝禁用账号，并始终关闭独立数据库会话。"""
    import app.core.database as database_module

    class FakeDb:
        """模拟用户查询、对象分离和会话关闭。"""

        def __init__(self, user: Any) -> None:
            """保存待返回用户并初始化调用记录。"""
            self.user = user
            self.closed = False
            self.expunged: list[Any] = []
            self.get_calls: list[tuple[Any, int]] = []

        def get(self, model: Any, user_id: int) -> Any:
            """记录模型和主键后返回配置用户。"""
            self.get_calls.append((model, user_id))
            return self.user

        def expunge(self, user: Any) -> None:
            """记录从会话分离的用户对象。"""
            self.expunged.append(user)

        def close(self) -> None:
            """标记数据库会话已关闭。"""
            self.closed = True

    active = _user(3)
    active_db = FakeDb(active)
    monkeypatch.setattr(database_module, "SessionLocal", lambda: active_db)
    assert module._load_active_user(3) is active
    assert active_db.closed is True

    disabled_db = FakeDb(_user(4, status=0))
    monkeypatch.setattr(database_module, "SessionLocal", lambda: disabled_db)
    assert module._load_active_user(4) is None
    assert disabled_db.closed is True

    ws_db = FakeDb(active)
    monkeypatch.setattr(module, "authenticate_access_token", lambda token, db: active)
    monkeypatch.setattr(module, "SessionLocal", lambda: ws_db)
    assert module._load_ws_user("jwt") is active
    assert ws_db.expunged == [active]
    assert ws_db.closed is True

    inactive_ws_db = FakeDb(_user(5, status=0))
    monkeypatch.setattr(module, "SessionLocal", lambda: inactive_ws_db)
    monkeypatch.setattr(module, "authenticate_access_token", MagicMock(side_effect=ValueError("inactive")))
    with pytest.raises(ValueError, match="inactive"):
        module._load_ws_user("jwt")
    assert inactive_ws_db.expunged == []
    assert inactive_ws_db.closed is True


def test_register_pending_purges_only_stale_unowned_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册新讨论时应清理过期废弃记录并保留仍在总线中的活跃会话。"""
    now = 10_000.0
    stale = module.PendingDiscussion("stale", user_id=1)
    stale.created_at = now - module._STALE_TTL - 1
    module._pending["stale"] = stale
    module._session_owners.update({"stale": 1, "orphan": 2, "active": 3})
    module._owner_registered_at.update(
        {
            "stale": now - module._STALE_TTL - 1,
            "orphan": now - module._STALE_TTL - 1,
            "active": now - module._STALE_TTL - 1,
        },
    )

    class PurgeBus(FakeDiscussionBus):
        """仅把 active 会话报告为仍然存在。"""

        def get_session(self, session_id: str, *, owner_user_id: int | None = None) -> Any:
            """active 返回会话，其余返回不存在。"""
            return SimpleNamespace(owner_user_id=3) if session_id == "active" else None

    # 该同步用例只使用 get_session，不应为未使用的 asyncio.Queue
    # 依赖 pytest-asyncio 在全局安装事件循环。
    _install_bus(monkeypatch, object.__new__(PurgeBus))
    monkeypatch.setattr(module.time, "time", lambda: now)

    module.register_pending("fresh", user_id="8", code="print(1)")

    assert "stale" not in module._pending
    assert "stale" not in module._session_owners
    assert "orphan" not in module._session_owners
    assert module._session_owners["active"] == 3
    assert module._session_owners["fresh"] == 8
    assert module._pending["fresh"].kwargs["code"] == "print(1)"


@pytest.mark.asyncio
async def test_ws_rejects_missing_or_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 token 或用户加载异常时应以 4001 拒绝且不接受连接。"""
    missing = FakeWebSocket()
    await module.ws_discuss(missing, "session")
    assert missing.closed == [(4001, "缺少 token")]
    assert missing.accepted == []

    invalid = FakeWebSocket(query=b"token=bad")

    def fail_load(token: str) -> None:
        """模拟 JWT 解码或数据库加载异常。"""
        raise ValueError("bad token")

    monkeypatch.setattr(module, "_load_ws_user", fail_load)
    await module.ws_discuss(invalid, "session")
    assert invalid.closed == [(4001, "token 无效")]
    assert invalid.accepted == []


@pytest.mark.asyncio
async def test_ws_rejects_unknown_and_unauthorized_sessions_without_consuming_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知会话返回 4004，越权会话返回 4003 且 pending 必须保留。"""
    bus = FakeDiscussionBus()
    _install_bus(monkeypatch, bus)
    monkeypatch.setattr(module, "_load_ws_user", lambda token: _user(1))

    unknown = FakeWebSocket(query=b"token=ok")
    await module.ws_discuss(unknown, "unknown")
    assert unknown.closed == [(4004, "session 不存在")]

    module.register_pending("owned", user_id=2, code="secret")
    monkeypatch.setattr(module, "_can_access_session", lambda _user, _owner: False)
    unauthorized = FakeWebSocket(query=b"token=ok")
    await module.ws_discuss(unauthorized, "owned")
    assert unauthorized.closed == [(4003, "无权访问讨论会话")]
    assert "owned" in module._pending


@pytest.mark.asyncio
async def test_ws_message_flow_uses_owner_registry_and_cleans_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已登记会话应处理心跳、用户输入和控制指令，并在断线后取消订阅。"""
    session_id = "registry-session"
    module._session_owners[session_id] = 11
    module._owner_registered_at[session_id] = time.time()
    bus = FakeDiscussionBus(
        session=SimpleNamespace(owner_user_id=11, status="active"),
        outbound=['{"type":"control","action":"ready"}'],
    )
    callback_calls: list[tuple[str, dict[str, Any]]] = []

    def callback(action: str, payload: dict[str, Any]) -> None:
        """记录首个控制动作后移除回调，以覆盖无控制器降级分支。"""
        callback_calls.append((action, payload))
        bus._control_callbacks.pop(session_id, None)

    bus._control_callbacks[session_id] = callback
    _install_bus(monkeypatch, bus)
    monkeypatch.setattr(module, "_load_ws_user", lambda token: _user(11))
    incoming = [
        "not-json",
        json.dumps({"action": "ping"}),
        json.dumps({"action": "user_input", "content": "   "}),
        json.dumps({"action": "user_input", "content": "  请检查这里  "}),
        json.dumps({"action": "pause"}),
        json.dumps({"action": "resume"}),
        json.dumps({"action": "stop"}),
        json.dumps({"action": "unknown"}),
    ]
    websocket = FakeWebSocket(query=b"token=query-jwt", incoming=incoming)

    await module.ws_discuss(websocket, session_id)
    await _ORIGINAL_ASYNCIO_SLEEP(0)

    assert websocket.accepted == [None]
    assert '{"type":"pong"}' in websocket.sent
    assert '{"type":"control","action":"ready"}' in websocket.sent
    assert len(bus.turns) == 1
    turn = bus.turns[0][1]
    assert turn.role == "user"
    assert turn.content == "请检查这里"
    assert bus.user_inputs == [(session_id, "请检查这里")]
    assert callback_calls == [("pause", {"session_id": session_id})]
    info_controls = [item for item in bus.controls if item[1] == "info"]
    assert len(info_controls) == 3
    assert bus.unsubscribed == [(session_id, bus.queue)]


@pytest.mark.asyncio
async def test_ws_rejects_late_input_without_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """终态 WS 仍连着时，迟到消息只给发送者拒绝回执。"""
    from app.agents.discussion_bus import DiscussionBus

    bus = DiscussionBus()
    bus.create_session("disc_late", 0, "main.py", owner_user_id=7)
    bus.close_session("disc_late")
    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(lambda cls: bus))
    monkeypatch.setattr(module, "_load_ws_user", lambda _token: _user(7))
    websocket = FakeWebSocket(
        query=b"token=ok",
        incoming=[json.dumps({"action": "user_input", "content": "迟到的意见"})],
    )

    await module.ws_discuss(websocket, "disc_late")

    assert websocket.accepted == [None]
    assert bus.get_session("disc_late").turns == []
    rejected = [json.loads(frame) for frame in websocket.sent if "input_rejected" in frame]
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_ws_accepts_completed_roundtable_followup_and_starts_background_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """圆桌主流程结束后，当前账号可经同一 WS 追问并获得持久接收回执。"""
    from app.agents.discussion_bus import DiscussionBus
    from app.services import roundtable_followup_service

    bus = DiscussionBus()
    session = bus.create_session("disc_followup_ws", 42, "main.py", owner_user_id=7)
    session.report_task_id = 42
    bus.publish_control(session.session_id, "done", {"status": "success", "task_id": 42})
    bus.close_session(session.session_id)
    scheduled = []
    monkeypatch.setattr(roundtable_followup_service, "ensure_followup_worker",
                        lambda *_args: scheduled.append(True))
    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(lambda cls: bus))
    monkeypatch.setattr(module, "_load_ws_user", lambda _token: _user(7))
    websocket = FakeWebSocket(
        query=b"token=ok",
        incoming=[json.dumps({"action": "user_input", "content": "报告中的证据在哪？"})],
    )
    await module.ws_discuss(websocket, session.session_id)
    assert websocket.accepted == [None]
    assert [turn.content for turn in session.turns] == ["报告中的证据在哪？"]
    assert scheduled == [True, True]  # 重连恢复待答问题，以及本次新问题。
    accepted = [json.loads(frame) for frame in websocket.sent if "input_accepted" in frame]
    assert len(accepted) == 1
    assert accepted[0]["payload"]["seq"] == 1


@pytest.mark.asyncio
async def test_ws_direct_reconnect_restores_persisted_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务重启后直接打开旧 WS 地址，无需先 GET 列表。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.agents.discussion_bus import DiscussionBus
    from app.agents.events import DiscussionTurn
    from app.models.roundtable import RoundtableSession, RoundtableTurn

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    original = DiscussionBus(persist=True, session_factory=factory)
    original.create_session("disc_restore", 0, "main.py", owner_user_id=7)
    original.publish_turn("disc_restore", DiscussionTurn(
        turn_id=1, agent_code="general", agent_name="通用质量代理",
        role="agent", content="已审查",
    ))
    original.close_session("disc_restore")

    restored = DiscussionBus()
    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(lambda cls: restored))
    monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(module, "_load_ws_user", lambda _token: _user(7))
    websocket = FakeWebSocket(query=b"token=ok")

    await module.ws_discuss(websocket, "disc_restore")

    assert websocket.accepted == [None]
    assert restored.get_session("disc_restore").owner_user_id == 7
    assert restored.get_session("disc_restore").turns[0].seq == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_foreign_ws_reconnect_cannot_interrupt_persisted_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知账号即使知道旧 WS 地址，也不能触发有副作用的会话恢复。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.agents.discussion_bus import DiscussionBus
    from app.models.roundtable import RoundtableSession, RoundtableTurn

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    original = DiscussionBus(persist=True, session_factory=factory)
    original.create_session("disc_foreign", 0, "main.py", owner_user_id=7)

    restored = DiscussionBus()
    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(lambda cls: restored))
    monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(module, "_load_ws_user", lambda _token: _user(8))
    websocket = FakeWebSocket(query=b"token=ok")
    await module.ws_discuss(websocket, "disc_foreign")

    assert websocket.accepted == []
    assert websocket.closed[0][0] == 4004
    with factory() as db:
        assert db.get(RoundtableSession, "disc_foreign").status == "active"
    engine.dispose()


@pytest.mark.asyncio
async def test_ws_queue_overflow_replays_every_turn_and_terminal_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史回放占满订阅队列时，慢客户端也能接到后续全部发言。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.agents.discussion_bus import DiscussionBus
    from app.agents.events import DiscussionTurn
    from app.models.roundtable import RoundtableSession, RoundtableTurn

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    RoundtableSession.__table__.create(engine)
    RoundtableTurn.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bus = DiscussionBus(persist=True, session_factory=factory)
    bus.create_session("disc_overflow", 0, "main.py", owner_user_id=7)
    for index in range(1, 151):
        bus.publish_turn("disc_overflow", DiscussionTurn(
            turn_id=index, agent_code="general", agent_name="通用质量代理",
            role="agent", content=f"发言 {index}",
        ))

    class SlowWebSocket(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__(query=b"token=ok")
            self.terminal_received = asyncio.Event()
            self.published = False
            self.published_during_replay = False
            self.last_seq = 0

        async def receive_text(self) -> str:
            if not self.published:
                self.published = True
                for index in range(151, 191):
                    bus.publish_turn("disc_overflow", DiscussionTurn(
                        turn_id=index, agent_code="general", agent_name="通用质量代理",
                        role="agent", content=f"发言 {index}",
                    ))
            await asyncio.wait_for(self.terminal_received.wait(), timeout=3)
            raise WebSocketDisconnect(code=1000)

        async def send_text(self, message: str) -> None:
            await super().send_text(message)
            parsed = json.loads(message)
            if parsed.get("type") == "discuss":
                self.last_seq = int(parsed["turn"].get("seq") or 0)
                if self.last_seq == 75 and not self.published_during_replay:
                    self.published_during_replay = True
                    for index in range(191, 221):
                        bus.publish_turn("disc_overflow", DiscussionTurn(
                            turn_id=index, agent_code="general", agent_name="通用质量代理",
                            role="agent", content=f"发言 {index}",
                        ))
                    bus.publish_control("disc_overflow", "done", {"status": "success"})
                    bus.close_session("disc_overflow")
            elif parsed.get("type") == "control" and parsed.get("action") == "done":
                if self.last_seq == 220:
                    self.terminal_received.set()

    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(lambda cls: bus))
    monkeypatch.setattr(module, "_load_ws_user", lambda _token: _user(7))
    websocket = SlowWebSocket()
    await module.ws_discuss(websocket, "disc_overflow")

    delivered = [json.loads(message)["turn"]["seq"] for message in websocket.sent
                 if json.loads(message).get("type") == "discuss"]
    assert delivered == list(range(51, 221))
    assert websocket.published_during_replay is True
    assert any(json.loads(message).get("action") == "done" for message in websocket.sent)
    with factory() as db:
        assert db.get(RoundtableSession, "disc_overflow").last_turn_seq == 220
    engine.dispose()


@pytest.mark.asyncio
async def test_ws_pending_session_starts_orchestrator_with_auth_subprotocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法 pending 会话应在鉴权后消费上下文并异步启动讨论编排器。"""
    import app.ai.discussion_orchestrator as orchestrator_module

    session_id = "pending-session"
    bus = FakeDiscussionBus()
    _install_bus(monkeypatch, bus)
    module.register_pending(
        session_id,
        user_id=21,
        code="print('hello')",
        language="python",
        profiles=("security",),
    )
    monkeypatch.setattr(module, "_load_ws_user", lambda token: _user(21))
    starts: list[dict[str, Any]] = []

    class FakeOrchestrator:
        """记录 pending WebSocket 触发的讨论启动参数。"""

        async def start_discussion(self, **kwargs: Any) -> None:
            """保存讨论启动参数并立即完成。"""
            starts.append(kwargs)

    monkeypatch.setattr(orchestrator_module, "DiscussionOrchestrator", FakeOrchestrator)
    websocket = FakeWebSocket(
        headers={"sec-websocket-protocol": "prism-auth, jwt-subprotocol"},
    )

    await module.ws_discuss(websocket, session_id)
    await _ORIGINAL_ASYNCIO_SLEEP(0)

    assert websocket.accepted == ["prism-auth"]
    assert session_id not in module._pending
    assert starts == [
        {
            "session_id": session_id,
            "user_id": 21,
            "code": "print('hello')",
            "language": "python",
            "profiles": ("security",),
        },
    ]
    assert bus.controls == []
    assert bus.unsubscribed == [(session_id, bus.queue)]


@pytest.mark.asyncio
async def test_pending_discussion_is_cancelled_when_owner_session_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建圆桌的登录版本失效后，应取消后台编排而不是继续写入结果。"""
    import app.ai.discussion_orchestrator as orchestrator_module
    from app.agents.discussion_bus import DiscussionBus

    session_id = "expired-owner-session"
    bus = DiscussionBus()
    bus.create_session(session_id, task_id=0, file_name="expired.py", owner_user_id=21)
    monkeypatch.setattr(module.DiscussionBus, "instance", classmethod(lambda cls: bus))
    monkeypatch.setattr(module, "_SESSION_CHECK_INTERVAL", 0.001)
    session_checks = iter((True, False))
    monkeypatch.setattr(
        module,
        "_is_session_version_active",
        lambda *_args: next(session_checks, False),
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeOrchestrator:
        async def start_discussion(self, **_kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(orchestrator_module, "DiscussionOrchestrator", FakeOrchestrator)
    pending = module.PendingDiscussion(
        session_id,
        user_id=21,
        session_token_version=4,
    )

    task = module.launch_pending_discussion(pending)
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert bus.get_session(session_id).status == "concluded"
    assert bus.get_discussion_task(session_id) is None


@pytest.mark.asyncio
async def test_ws_existing_session_allows_admin_and_sends_server_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理员可连接既有会话，连接状态正常时服务端应发送主动心跳。"""
    session_id = "existing-session"
    session = SimpleNamespace(owner_user_id=99, status="active")
    bus = FakeDiscussionBus(session=session)
    _install_bus(monkeypatch, bus)
    monkeypatch.setattr(module, "_load_ws_user", lambda token: _user(1, role="admin"))
    monkeypatch.setattr(module, "_can_access_session", lambda _user, _owner: True)
    heartbeat_calls = 0

    async def controlled_sleep(delay: float) -> None:
        """让心跳首次立即执行，后续等待直到 endpoint 取消任务。"""
        nonlocal heartbeat_calls
        if delay == module._SESSION_CHECK_INTERVAL:
            heartbeat_calls += 1
            if heartbeat_calls == 1:
                return
            await asyncio.Event().wait()
        else:
            await _ORIGINAL_ASYNCIO_SLEEP(0)

    monkeypatch.setattr(module.asyncio, "sleep", controlled_sleep)
    websocket = FakeWebSocket(query=b"token=admin", connected=True)

    await module.ws_discuss(websocket, session_id)
    await _ORIGINAL_ASYNCIO_SLEEP(0)

    assert websocket.accepted == [None]
    assert any(message.startswith('{"type":"server_ping","ts":') for message in websocket.sent)
    assert bus.controls[0][1] == "info"
    assert bus.unsubscribed == [(session_id, bus.queue)]
