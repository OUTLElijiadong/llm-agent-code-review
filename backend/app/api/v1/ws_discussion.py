"""多 Agent 讨论 WebSocket 端点 (v2.3 M7 / v2.4 心跳增强)

ws://host:8000/api/ws/discuss/{session_id}

协议:
  服务端 → 客户端: JSON 文本帧
    {"type":"discuss", "session_id":"...", "turn":{...}}
    {"type":"control", "action":"round_start", "payload":{"round":1,"total_rounds":3}}
    {"type":"control", "action":"done"}
    {"type":"session_end"}
    {"type":"pong"}                        # 响应客户端 ping

  客户端 → 服务端: JSON 文本帧
    {"action":"user_input", "content":"..."}
    {"action":"pause"}
    {"action":"resume"}
    {"action":"stop"}
    {"action":"ping"}                      # 客户端心跳,服务端响应 pong

v2.4 心跳机制:
  - 客户端每 30s 发送 ping,服务端立即响应 pong
  - 服务端额外启动 60s 主动探测任务,检测僵尸连接
  - 双向心跳确保 NAT/防火墙场景下连接保活
"""
from __future__ import annotations

import asyncio
import json as json_lib
import time
from urllib.parse import parse_qs

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from app.agents.discussion_bus import DiscussionBus
from app.core.database import SessionLocal
from app.core.dependencies import authenticate_access_token
from app.models.user import User


class PendingDiscussion:
    """待启动的讨论上下文 — 前端 preflight 后暂存"""
    def __init__(self, session_id: str, **kwargs):
        self.session_id = session_id
        raw_version = kwargs.pop("session_token_version", None)
        self.session_token_version = int(raw_version) if raw_version is not None else None
        self.kwargs = kwargs
        self.created_at = time.time()


_pending: dict[str, PendingDiscussion] = {}
# session_id → 发起讨论的用户 id,用于 WebSocket 连接时的归属校验。
_session_owners: dict[str, int] = {}
# session_id → 归属记录注册时间,供过期清理判断(与 _session_owners 同生共死)。
_owner_registered_at: dict[str, float] = {}

# preflight 后一直没有 WebSocket 来消费的 pending、以及会话已从总线清除的
# 归属记录,超过此时长即视为废弃 — 防止两个模块级 dict 随讨论次数无限增长。
_STALE_TTL = 3600.0
_SESSION_CHECK_INTERVAL = 1.0


def _purge_stale():
    """机会性清理:每次 register_pending 时顺带扫一遍过期条目。"""
    now = time.time()
    for sid in [s for s, p in _pending.items() if now - p.created_at > _STALE_TTL]:
        _pending.pop(sid, None)
        _session_owners.pop(sid, None)
        _owner_registered_at.pop(sid, None)
    bus = DiscussionBus.instance()
    for sid in [
        s for s, ts in _owner_registered_at.items()
        if now - ts > _STALE_TTL and s not in _pending and bus.get_session(s) is None
    ]:
        _session_owners.pop(sid, None)
        _owner_registered_at.pop(sid, None)


def register_pending(session_id: str, **kwargs):
    _purge_stale()
    _pending[session_id] = PendingDiscussion(session_id, **kwargs)
    owner = kwargs.get("user_id")
    if owner is not None:
        _session_owners[session_id] = int(owner)
        _owner_registered_at[session_id] = time.time()


def take_pending(session_id: str) -> PendingDiscussion | None:
    """原子领取待启动上下文，供 WebSocket 或用户 Agent 二选一启动。"""
    return _pending.pop(session_id, None)


def _load_active_user(user_id: int):
    """用独立短连接校验用户存在且启用(WebSocket 无 Depends(get_db) 可用)。"""
    from app.core.database import SessionLocal
    from app.models.user import User

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or user.status != 1:
            return None
        return user
    finally:
        db.close()


def _token_from_subprotocol(websocket: WebSocket) -> str | None:
    """从 Sec-WebSocket-Protocol 中读取前端传递的 JWT。"""
    protocol_header = websocket.headers.get("sec-websocket-protocol", "")
    protocols = [part.strip() for part in protocol_header.split(",") if part.strip()]
    if "prism-auth" not in protocols:
        return None
    idx = protocols.index("prism-auth")
    return protocols[idx + 1] if idx + 1 < len(protocols) else None


def _has_auth_subprotocol(websocket: WebSocket) -> bool:
    """判断客户端是否声明了 Prism WebSocket 鉴权子协议。"""
    protocol_header = websocket.headers.get("sec-websocket-protocol", "")
    protocols = [part.strip() for part in protocol_header.split(",") if part.strip()]
    return "prism-auth" in protocols


def _token_from_query(websocket: WebSocket) -> str | None:
    """从 query string 中读取兼容旧前端的 JWT。"""
    qs = websocket.scope.get("query_string", b"").decode()
    params = parse_qs(qs)
    tokens = params.get("token", [])
    return tokens[0] if tokens else None


def _can_access_session(user: User, owner_user_id: int) -> bool:
    """判断当前用户是否可订阅指定讨论会话。

    Args:
        user: 当前 WebSocket 认证用户。
        owner_user_id: 讨论会话创建者 ID。

    Returns:
        bool: owner 或管理员返回 True。
    """
    if owner_user_id <= 0:
        return False
    return user.role in {"admin", "super_admin"} or user.id == owner_user_id


def _load_ws_user(token: str) -> User | None:
    """解析 JWT 并加载当前有效用户。

    Args:
        token: Bearer JWT。

    Returns:
        User | None: token 有效且账号启用时返回用户对象。
    """
    db = SessionLocal()
    try:
        user = authenticate_access_token(token, db)
        db.expunge(user)
        return user
    finally:
        db.close()


def _is_session_version_active(user_id: int, token_version: int) -> bool:
    """检查创建圆桌任务的登录版本是否仍为当前设备会话。"""

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        return bool(
            user
            and user.status == 1
            and int(user.token_version or 0) == token_version
        )
    except Exception:
        return False
    finally:
        db.close()


async def _run_pending_discussion(pending: PendingDiscussion) -> None:
    """运行圆桌编排，并在创建它的登录版本失效时终止后台任务。"""

    from app.ai.discussion_orchestrator import DiscussionOrchestrator

    bus = DiscussionBus.instance()
    user_id = int(pending.kwargs.get("user_id") or 0)
    if pending.session_token_version is not None:
        active = await asyncio.to_thread(
            _is_session_version_active,
            user_id,
            pending.session_token_version,
        )
        if not active:
            bus.request_stop(pending.session_id)
            bus.publish_control(pending.session_id, "cancelled", {"task_id": 0})
            bus.close_session(pending.session_id)
            return

    orchestrator_task = asyncio.create_task(
        DiscussionOrchestrator().start_discussion(
            session_id=pending.session_id,
            **pending.kwargs,
        ),
        name=f"discussion-orchestrator:{pending.session_id}",
    )
    monitor_task: asyncio.Task | None = None
    try:
        if pending.session_token_version is None:
            await orchestrator_task
            return

        async def monitor_session() -> None:
            while True:
                await asyncio.sleep(_SESSION_CHECK_INTERVAL)
                active = await asyncio.to_thread(
                    _is_session_version_active,
                    user_id,
                    pending.session_token_version,
                )
                if not active:
                    return

        monitor_task = asyncio.create_task(
            monitor_session(),
            name=f"discussion-auth:{pending.session_id}",
        )
        done, _ = await asyncio.wait(
            {orchestrator_task, monitor_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if monitor_task in done and not orchestrator_task.done():
            logger.info(
                f"[Discussion] 登录版本失效，取消旧设备圆桌 session={pending.session_id}"
            )
            bus.request_stop(pending.session_id)
            orchestrator_task.cancel()
        await orchestrator_task
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
        if not orchestrator_task.done():
            bus.request_stop(pending.session_id)
            orchestrator_task.cancel()
        pending_tasks = [task for task in (monitor_task, orchestrator_task) if task is not None]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)


def launch_pending_discussion(pending: PendingDiscussion) -> asyncio.Task:
    """按 session_id 唯一登记并启动待处理圆桌任务。"""

    bus = DiscussionBus.instance()
    return bus.start_discussion_task(
        pending.session_id,
        _run_pending_discussion(pending),
    )


async def ws_discuss(websocket: WebSocket, session_id: str):
    """WebSocket 讨论连接"""
    client = websocket.client.host if websocket.client else "unknown"
    logger.info(f"[WS] 讨论连接请求 session={session_id} client={client}")
    # 优先从子协议读取 token；query token 仅用于兼容旧前端。
    use_auth_subprotocol = _has_auth_subprotocol(websocket)
    token = _token_from_subprotocol(websocket) or _token_from_query(websocket)
    if not token:
        logger.warning(f"[WS] 讨论连接拒绝: 缺少 token session={session_id} client={client}")
        await websocket.close(code=4001, reason="缺少 token")
        return
    try:
        user = _load_ws_user(token)
    except Exception:
        user = None
    if not user:
        logger.warning(f"[WS] 讨论连接拒绝: token 无效 session={session_id} client={client}")
        await websocket.close(code=4001, reason="token 无效")
        return

    bus = DiscussionBus.instance()
    pending = _pending.get(session_id)
    session = bus.get_session(session_id)
    owner_user_id = 0
    if pending:
        owner_user_id = int(pending.kwargs.get("user_id") or 0)
    elif session:
        owner_user_id = session.owner_user_id
    elif session_id in _session_owners:
        owner_user_id = _session_owners[session_id]
    else:
        logger.warning(f"[WS] 讨论连接拒绝: session 不存在 session={session_id} client={client}")
        await websocket.close(code=4004, reason="session 不存在")
        return

    if not _can_access_session(user, owner_user_id):
        logger.warning(
            f"[WS] 讨论连接拒绝: 无权访问 session={session_id} user={user.id} owner={owner_user_id}"
        )
        await websocket.close(code=4003, reason="无权访问讨论会话")
        return

    # 检查是否有待启动的讨论。通过鉴权后再 pop,避免越权连接消耗 pending。
    pending = take_pending(session_id)

    await websocket.accept(subprotocol="prism-auth" if use_auth_subprotocol else None)
    logger.info(f"[WS] 讨论连接已接受 session={session_id} pending={bool(pending)} client={client}")
    queue: asyncio.Queue = await bus.subscribe(session_id)

    # 启动讨论编排
    if pending:
        launch_pending_discussion(pending)
    else:
        bus.publish_control(session_id, "info", {
            "message": "已连接,等待讨论启动。可通过 API 触发讨论。",
        })

    # 转发消息 (WS → 客户端, 客户端 → WS)
    async def pump_bus_to_ws():
        try:
            while True:
                msg = await queue.get()
                await websocket.send_text(msg)
        except asyncio.CancelledError:
            pass

    pump_task = asyncio.create_task(pump_bus_to_ws())

    # v2.4: 服务端主动心跳探测任务
    # 每秒复验会话版本；发现新设备登录后主动关闭旧设备连接。
    # 主要作用: 在客户端心跳失效时,服务端主动刷新 NAT 会话,防止中间设备断连
    async def server_heartbeat():
        try:
            while True:
                await asyncio.sleep(_SESSION_CHECK_INTERVAL)
                if websocket.client_state.value == 1:  # CONNECTED
                    try:
                        _load_ws_user(token)
                    except Exception:
                        if int(user.id) == owner_user_id:
                            bus.cancel_discussion_task(session_id)
                        await websocket.close(code=4001, reason="账号已在另一台设备登录")
                        return
                    ts = str(int(asyncio.get_event_loop().time()))
                    await websocket.send_text('{"type":"server_ping","ts":' + ts + '}')
        except asyncio.CancelledError:
            pass
        except Exception:
            # 发送失败说明连接已断开,忽略
            pass

    heartbeat_task = asyncio.create_task(server_heartbeat())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                _load_ws_user(token)
            except Exception:
                if int(user.id) == owner_user_id:
                    bus.cancel_discussion_task(session_id)
                await websocket.close(code=4001, reason="账号已在另一台设备登录")
                return
            try:
                data = json_lib.loads(raw)
                action = data.get("action", "")

                if action == "ping":
                    # 客户端心跳,立即响应 pong
                    await websocket.send_text('{"type":"pong"}')
                    continue

                if action == "user_input":
                    content = data.get("content", "")
                    if content.strip():
                        # 先广播用户的发言到讨论面板
                        from app.agents.events import DiscussionTurn
                        user_turn = DiscussionTurn(
                            turn_id=-1,
                            agent_code="user",
                            agent_name="你",
                            role="user",
                            content=content.strip(),
                        )
                        bus.publish_turn(session_id, user_turn)
                        # 然后通知编排器
                        bus.send_user_input(session_id, content.strip())
                elif action in ("pause", "resume", "stop"):
                    if not bus.control_session(session_id, action):
                        bus.publish_control(session_id, "info", {
                            "message": "讨论尚未启动或已结束。",
                        })
            except json_lib.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        logger.info(f"[WS] 讨论客户端断开 session={session_id}")
    finally:
        pump_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(pump_task, heartbeat_task, return_exceptions=True)
        bus.unsubscribe(session_id, queue)
