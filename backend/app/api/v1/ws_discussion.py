"""多 Agent 讨论 WebSocket 端点 (v2.3 M7)

ws://host:8000/api/ws/discuss/{session_id}?token=xxx

协议:
  服务端 → 客户端: JSON 文本帧
    {"type":"discuss", "session_id":"...", "turn":{...}}
    {"type":"control", "action":"round_start", "payload":{"round":1,"total_rounds":3}}
    {"type":"control", "action":"done"}
    {"type":"session_end"}

  客户端 → 服务端: JSON 文本帧
    {"action":"user_input", "content":"..."}
    {"action":"pause"}
    {"action":"resume"}
    {"action":"stop"}
"""
from __future__ import annotations

import asyncio
import json as json_lib
from urllib.parse import parse_qs

import jwt
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from app.agents.discussion_bus import DiscussionBus
from app.core.config import settings


class PendingDiscussion:
    """待启动的讨论上下文 — 前端 preflight 后暂存"""
    def __init__(self, session_id: str, **kwargs):
        self.session_id = session_id
        self.kwargs = kwargs


_pending: dict[str, PendingDiscussion] = {}
# session_id → 发起讨论的用户 id,用于 WebSocket 连接时的归属校验。
_session_owners: dict[str, int] = {}


def register_pending(session_id: str, **kwargs):
    _pending[session_id] = PendingDiscussion(session_id, **kwargs)
    owner = kwargs.get("user_id")
    if owner is not None:
        _session_owners[session_id] = int(owner)


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


async def ws_discuss(websocket: WebSocket, session_id: str):
    """WebSocket 讨论连接"""
    # 从 query string 验证 token
    token = None
    qs = websocket.scope.get("query_string", b"").decode()
    params = parse_qs(qs)
    tokens = params.get("token", [])
    if tokens:
        token = tokens[0]
    if not token:
        await websocket.close(code=4001, reason="缺少 token")
        return
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
        ws_user_id = int(claims["sub"])
    except Exception:
        await websocket.close(code=4001, reason="token 无效")
        return

    # 校验用户存在且未禁用,并做会话归属校验:仅会话发起人或管理员可连接。
    ws_user = _load_active_user(ws_user_id)
    if ws_user is None:
        await websocket.close(code=4003, reason="账号不存在或已禁用")
        return
    owner_id = _session_owners.get(session_id)
    if owner_id is not None and owner_id != ws_user_id and ws_user.role != "admin":
        logger.warning(
            f"[WS] 讨论连接拒绝: 越权访问 session={session_id} "
            f"owner={owner_id} user={ws_user_id}"
        )
        await websocket.close(code=4003, reason="无权访问该讨论会话")
        return

    # 检查是否有待启动的讨论
    pending = _pending.pop(session_id, None)

    await websocket.accept()
    bus = DiscussionBus.instance()
    queue: asyncio.Queue = await bus.subscribe(session_id)

    # 启动讨论编排
    if pending:
        from app.ai.discussion_orchestrator import DiscussionOrchestrator
        orch = DiscussionOrchestrator()
        asyncio.create_task(orch.start_discussion(
            session_id=session_id, **pending.kwargs,
        ))
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

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json_lib.loads(raw)
                action = data.get("action", "")

                if action == "ping":
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
                    cb = bus._control_callbacks.get(session_id)
                    if cb:
                        cb(action, {"session_id": session_id})
                    else:
                        bus.publish_control(session_id, "info", {
                            "message": "讨论尚未启动或已结束。",
                        })
            except json_lib.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        logger.info(f"[WS] 讨论客户端断开 session={session_id}")
    finally:
        pump_task.cancel()
        bus.unsubscribe(session_id, queue)
