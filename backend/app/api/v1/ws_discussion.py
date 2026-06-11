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


def register_pending(session_id: str, **kwargs):
    _pending[session_id] = PendingDiscussion(session_id, **kwargs)


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
        jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except Exception:
        logger.warning(f"[WS] 讨论连接拒绝: token 无效 session={session_id} client={client}")
        await websocket.close(code=4001, reason="token 无效")
        return

    # 检查是否有待启动的讨论
    pending = _pending.pop(session_id, None)

    await websocket.accept(subprotocol="prism-auth" if use_auth_subprotocol else None)
    logger.info(f"[WS] 讨论连接已接受 session={session_id} pending={bool(pending)} client={client}")
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
