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
import contextlib
import json as json_lib
import threading
import time
from urllib.parse import parse_qs

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from app.agents.discussion_bus import DiscussionBus
from app.core.database import SessionLocal
from app.core.dependencies import authenticate_access_token
from app.models.roundtable import RoundtableSession
from app.models.user import User
from app.services.roundtable_report_lifecycle import cancel_running_discussion_report


class PendingDiscussion:
    """待启动的讨论上下文 — 前端 preflight 后暂存"""
    def __init__(self, session_id: str, **kwargs):
        self.session_id = session_id
        raw_version = kwargs.pop("session_token_version", None)
        self.session_token_version = int(raw_version) if raw_version is not None else None
        self.kwargs = kwargs
        self.created_at = time.time()


_pending: dict[str, PendingDiscussion] = {}
_pending_lock = threading.RLock()
_pending_expiry_timer: threading.Timer | None = None
_pending_expiry_generation = 0
# session_id → 发起讨论的用户 id,用于 WebSocket 连接时的归属校验。
_session_owners: dict[str, int] = {}
# session_id → 归属记录注册时间,供过期清理判断(与 _session_owners 同生共死)。
_owner_registered_at: dict[str, float] = {}

# preflight 后一直没有 WebSocket 来消费的 pending、以及会话已从总线清除的
# 归属记录,超过此时长即视为废弃 — 防止两个模块级 dict 随讨论次数无限增长。
_STALE_TTL = 3600.0
_SESSION_CHECK_INTERVAL = 1.0
_MAX_DISCUSSION_USER_INPUT_CHARS = 20_000


def _finish_expired_pending(bus: DiscussionBus, session_id: str, owner_user_id: int) -> bool:
    """把未在 TTL 内启动的预检会话记为失败终态，不消耗讨论模型调用。"""
    session = bus.get_session(session_id, owner_user_id=owner_user_id)
    if session is None or session.status in {"concluded", "partial", "failed", "cancelled", "interrupted"}:
        return True
    task = bus.get_discussion_task(session_id)
    if task is not None and not task.done():
        return False
    if session.status != "pending" and session.progress.get("phase") != "pending":
        return False
    message = "圆桌等待 WebSocket 启动超时，任务未执行，也未消耗讨论模型调用。"
    bus.publish_control(session_id, "done", {
        "task_id": 0,
        "status": "failed",
        "error": message,
    })
    bus.close_session(session_id)
    return True


def _publish_cancelled_terminal(
    bus: DiscussionBus,
    session_id: str,
    *,
    error: str,
    task_id: int = 0,
) -> bool:
    """以幂等终态通知收敛已停止的会话和前端连接。"""
    bus.request_stop(session_id, reason=error)
    session = bus.get_session(session_id)
    if session is None or session.terminal_status != "cancelled":
        return False
    payload = {
        "task_id": int(
            task_id
            or getattr(session, "report_task_id", 0)
            or getattr(session, "task_id", 0)
            or 0
        ),
        "status": "cancelled",
        "error": session.terminal_error or error,
    }
    bus.publish_control(session_id, "cancelled", payload)
    bus.publish_control(session_id, "done", payload)
    bus.close_session(session_id)
    return True


def _schedule_next_pending_expiry_locked() -> None:
    """用单一 daemon 定时器收敛最早过期项，避免每个待启动会话各占一线程。"""
    global _pending_expiry_timer, _pending_expiry_generation
    if _pending_expiry_timer is not None:
        _pending_expiry_timer.cancel()
        _pending_expiry_timer = None
    _pending_expiry_generation += 1
    if not _pending:
        return
    expires_at = min(pending.created_at + _STALE_TTL for pending in _pending.values())
    timer = threading.Timer(
        max(0.05, expires_at - time.time()),
        _expire_pending_at_deadline,
        args=(_pending_expiry_generation,),
    )
    timer.daemon = True
    _pending_expiry_timer = timer
    timer.start()


def _expire_pending_at_deadline(generation: int) -> None:
    """到期后扫描并收敛 pending；代次令被取消的旧定时器不能影响新状态。"""
    global _pending_expiry_timer
    with _pending_lock:
        if generation != _pending_expiry_generation:
            return
        _pending_expiry_timer = None
    purge_stale_pending()


def purge_stale_pending():
    """收敛过期预检会话，并清理进程内归属表和重启后遗留的数据库 pending。"""
    now = time.time()
    bus = DiscussionBus.instance()
    with _pending_lock:
        stale = [
            (sid, pending) for sid, pending in _pending.items()
            if now - pending.created_at >= _STALE_TTL
        ]
        for sid, pending in stale:
            owner = int(pending.kwargs.get("user_id") or 0)
            may_remove_owner = _finish_expired_pending(bus, sid, owner) if owner else True
            _pending.pop(sid, None)
            if may_remove_owner:
                _session_owners.pop(sid, None)
                _owner_registered_at.pop(sid, None)

    # pending 状态也必须在进程重启后收敛。普通 active/paused 会话由总线恢复逻辑
    # 标为 interrupted；只有 pending 且超过 TTL 的预检会话在此标为 failed。
    if getattr(bus, "_persist", False):
        try:
            from app.agents.discussion_bus import utc_timestamp
            from app.models.roundtable import RoundtableSession

            with bus._session_maker()() as db:
                persisted_pending = db.query(RoundtableSession).filter(
                    RoundtableSession.status == "pending",
                ).all()
                stale_ids = [
                    (row.session_id, int(row.owner_user_id))
                    for row in persisted_pending
                    if utc_timestamp(row.updated_at or row.created_at) <= now - _STALE_TTL
                ]
            for sid, owner in stale_ids:
                may_remove_owner = _finish_expired_pending(bus, sid, owner)
                with _pending_lock:
                    _pending.pop(sid, None)
                    if may_remove_owner:
                        _session_owners.pop(sid, None)
                        _owner_registered_at.pop(sid, None)
        except Exception:
            logger.exception("[WS] 收敛持久化过期 pending 圆桌失败")

    with _pending_lock:
        stale_owners = [
            sid for sid, registered_at in _owner_registered_at.items()
            if now - registered_at > _STALE_TTL and sid not in _pending
            and bus.get_session(sid, owner_user_id=_session_owners.get(sid)) is None
        ]
        for sid in stale_owners:
            _session_owners.pop(sid, None)
            _owner_registered_at.pop(sid, None)
        _schedule_next_pending_expiry_locked()


def register_pending(session_id: str, **kwargs):
    purge_stale_pending()
    with _pending_lock:
        pending = PendingDiscussion(session_id, **kwargs)
        _pending[session_id] = pending
        owner = kwargs.get("user_id")
        if owner is not None:
            _session_owners[session_id] = int(owner)
            _owner_registered_at[session_id] = time.time()
        _schedule_next_pending_expiry_locked()


def take_pending(session_id: str) -> PendingDiscussion | None:
    """原子领取待启动上下文，供 WebSocket 或用户 Agent 二选一启动。"""
    purge_stale_pending()
    with _pending_lock:
        pending = _pending.pop(session_id, None)
        _schedule_next_pending_expiry_locked()
        return pending


def cancel_pending(session_id: str, owner_user_id: int) -> bool:
    """按所有者撤销尚未被 WebSocket 消费的启动上下文。"""
    with _pending_lock:
        pending = _pending.get(session_id)
        if pending is None or int(pending.kwargs.get("user_id") or 0) != int(owner_user_id):
            return False
        _pending.pop(session_id, None)
        _schedule_next_pending_expiry_locked()
        return True


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


def _can_access_session(user: User, owner_user_id: int, db: Session | None = None) -> bool:
    """判断当前用户是否可订阅指定讨论会话。

    Args:
        user: 当前 WebSocket 认证用户。
        owner_user_id: 讨论会话创建者 ID。

    Returns:
        bool: 仅明确归属当前账号时返回 True，管理员无私人会话豁免。
    """
    return (
        isinstance(owner_user_id, int) and not isinstance(owner_user_id, bool)
        and owner_user_id > 0 and user.id == owner_user_id
    )


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


def _is_discussion_execution_active(user_id: int, project_id: int, token_version: int | None = None) -> bool:
    """Fresh execution authorization; history subscription itself remains a read operation."""
    if project_id <= 0:
        return False
    if token_version is not None and not _is_session_version_active(user_id, token_version):
        return False
    db = SessionLocal()
    try:
        from app.ai.discussion_orchestrator import _require_discussion_actor
        _require_discussion_actor(db, user_id, project_id)
        return True
    except Exception:
        return False
    finally:
        db.close()


def _is_parent_response_run_cancelled(user_id: int, agent_run_id: int) -> bool:
    """仅当归属用户的父级 Responses 运行明确进入 cancelled 时返回 True。"""
    if user_id <= 0 or agent_run_id <= 0:
        return False
    from app.models.agent_response_run import AgentResponseRun

    db = SessionLocal()
    try:
        row = (
            db.query(AgentResponseRun.status)
            .filter(
                AgentResponseRun.id == int(agent_run_id),
                AgentResponseRun.user_id == int(user_id),
            )
            .one_or_none()
        )
        return bool(row and str(row[0]).lower() == "cancelled")
    except Exception:
        # 瞬时数据库错误不能误杀仍在执行的圆桌；后续轮询会再检查。
        return False
    finally:
        db.close()


async def _run_pending_discussion(pending: PendingDiscussion) -> None:
    """运行圆桌编排，并监视登录失效及发起它的父级 Responses 取消。"""

    from app.ai.discussion_orchestrator import DiscussionOrchestrator

    bus = DiscussionBus.instance()
    session = bus.get_session(pending.session_id)
    if not session or session.status != "pending" or session.cancel_event.is_set():
        logger.info("[WS] 丢弃已终止或不存在的待启动圆桌 session=%s", pending.session_id)
        return
    bus.publish_control(pending.session_id, "started", {"status": "active"})
    user_id = int(pending.kwargs.get("user_id") or 0)
    project_id = int(pending.kwargs.get("project_id") or 0)
    usage_origin = pending.kwargs.get("usage_origin") or {}
    parent_run_id = int(usage_origin.get("agent_run_id") or 0)
    active = await asyncio.to_thread(
        _is_discussion_execution_active, user_id, project_id, pending.session_token_version,
    )
    parent_cancelled = bool(
        parent_run_id
        and await asyncio.to_thread(_is_parent_response_run_cancelled, user_id, parent_run_id)
    )
    if not active or parent_cancelled:
        error = "发起圆桌的小菱运行已取消" if parent_cancelled else "登录会话已失效"
        _publish_cancelled_terminal(bus, pending.session_id, error=error)
        return

    # 权限/父任务检查期间用户可能已停止该会话；在创建编排器前再读一次，
    # 避免一个迟到的 pending 启动任务重新进入模型流程。
    session = bus.get_session(pending.session_id)
    if not session or session.status != "active" or session.cancel_event.is_set():
        logger.info("[WS] 启动前会话已终止，跳过圆桌编排 session=%s", pending.session_id)
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
        async def monitor_session() -> str:
            while True:
                await asyncio.sleep(_SESSION_CHECK_INTERVAL)
                active_now = await asyncio.to_thread(
                    _is_discussion_execution_active,
                    user_id,
                    project_id,
                    pending.session_token_version,
                )
                if not active_now:
                    return "session_expired"
                if parent_run_id and await asyncio.to_thread(
                    _is_parent_response_run_cancelled, user_id, parent_run_id,
                ):
                    return "parent_cancelled"

        monitor_task = asyncio.create_task(
            monitor_session(),
            name=f"discussion-auth:{pending.session_id}",
        )
        done, _ = await asyncio.wait(
            {orchestrator_task, monitor_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if monitor_task in done and not orchestrator_task.done():
            invalidation = monitor_task.result()
            error = (
                "发起圆桌的小菱运行已取消"
                if invalidation == "parent_cancelled"
                else "登录会话已失效"
            )
            logger.info(
                f"[Discussion] 取消关联圆桌 reason={invalidation} session={pending.session_id}"
            )
            _publish_cancelled_terminal(bus, pending.session_id, error=error)
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


_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def bind_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """记录 Web 进程主事件循环；团队调度线程据此把圆桌编排投递回主循环。"""

    global _MAIN_LOOP
    _MAIN_LOOP = loop


def launch_pending_discussion_from_thread(pending: PendingDiscussion, *, timeout: float = 10.0) -> bool:
    """在非事件循环线程（团队调度器）里启动圆桌。

    圆桌订阅队列属于主事件循环，编排任务必须在主循环创建，不能在工作线程另起循环。
    """

    loop = _MAIN_LOOP
    if loop is None or loop.is_closed() or not loop.is_running():
        raise RuntimeError("Web 主事件循环不可用，无法启动圆桌")

    async def _launch() -> bool:
        return launch_pending_discussion(pending) is not None

    return bool(asyncio.run_coroutine_threadsafe(_launch(), loop).result(timeout=timeout))


def launch_pending_discussion(pending: PendingDiscussion) -> asyncio.Task | None:
    """按 session_id 唯一登记并启动待处理圆桌任务。"""

    bus = DiscussionBus.instance()
    if time.time() - pending.created_at >= _STALE_TTL:
        owner = int(pending.kwargs.get("user_id") or 0)
        if owner:
            _finish_expired_pending(bus, pending.session_id, owner)
        with _pending_lock:
            _pending.pop(pending.session_id, None)
            _session_owners.pop(pending.session_id, None)
            _owner_registered_at.pop(pending.session_id, None)
            _schedule_next_pending_expiry_locked()
        return None
    session = bus.get_session(pending.session_id)
    current = bus.get_discussion_task(pending.session_id)
    if current is not None:
        return current
    if session is None or session.status != "pending" or session.cancel_event.is_set():
        logger.info(
            "[WS] 拒绝自动启动非 pending 圆桌 session=%s status=%s",
            pending.session_id,
            getattr(session, "status", "missing"),
        )
        return None
    return bus.start_discussion_task(
        pending.session_id,
        _run_pending_discussion(pending),
    )


async def ws_discuss(websocket: WebSocket, session_id: str):
    """WebSocket 讨论连接"""
    purge_stale_pending()
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
    session = bus.get_session(session_id, owner_user_id=int(user.id))
    if session is None and pending is None and isinstance(bus, DiscussionBus):
        # 浏览器可直接按旧 WS 地址重连，不依赖先访问圆桌 REST 列表。
        with SessionLocal() as db:
            bus.enable_persistence(sessionmaker(bind=db.get_bind(), expire_on_commit=False))
        purge_stale_pending()
        session = bus.get_session(session_id, owner_user_id=int(user.id))
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
    if session is not None and session.status == "concluded":
        from app.services.roundtable_followup_service import ensure_followup_worker

        ensure_followup_worker(bus, session_id, owner_user_id)

    # 启动讨论编排
    if pending:
        launch_pending_discussion(pending)
    else:
        current = bus.get_session(session_id, owner_user_id=owner_user_id)
        if current is not None and current.status in {"active", "paused"}:
            bus.publish_control(session_id, "info", {
                "message": "已连接,等待讨论启动。可通过 API 触发讨论。",
            })

    # 转发消息 (WS → 客户端, 客户端 → WS)
    async def pump_bus_to_ws():
        last_sent_seq = int(getattr(queue, "replay_after_seq", 0))

        async def send_missing_turns() -> None:
            """以账本为准补发所有未送达发言，允许补发期间继续产生新发言。"""
            nonlocal last_sent_seq
            while True:
                page = bus.get_turns_after(
                    session_id, owner_user_id, after_seq=last_sent_seq, limit=100,
                )
                if not page:
                    current = bus.get_session(session_id, owner_user_id=owner_user_id)
                    if current is not None and current.last_turn_seq > last_sent_seq:
                        raise RuntimeError("圆桌发言账本存在无法补齐的序号缺口")
                    return
                for turn in page:
                    if turn.seq != last_sent_seq + 1:
                        raise RuntimeError("圆桌发言账本序号不连续")
                    await websocket.send_text(json_lib.dumps({
                        "type": "discuss", "session_id": session_id,
                        "turn": turn.to_dict(),
                    }, ensure_ascii=False))
                    last_sent_seq = turn.seq

        try:
            while True:
                if getattr(queue, "needs_resync", False):
                    queue.needs_resync = False
                    # 队列里的旧帧与账本补页会重复；清空后从最后已发送 seq 修复。
                    while not queue.empty():
                        queue.get_nowait()
                    await send_missing_turns()
                    for frame in bus.recovery_frames(session_id, owner_user_id):
                        await websocket.send_text(frame)
                    continue
                msg = await queue.get()
                if getattr(queue, "needs_resync", False):
                    continue
                parsed = json_lib.loads(msg)
                delivered_seq = 0
                if parsed.get("type") == "discuss":
                    seq = int(parsed.get("turn", {}).get("seq") or 0)
                    if seq > 0:
                        if seq > last_sent_seq + 1:
                            await send_missing_turns()
                        if seq <= last_sent_seq:
                            continue
                        if seq != last_sent_seq + 1:
                            raise RuntimeError("圆桌实时发言序号不连续")
                        delivered_seq = seq
                elif parsed.get("type") == "session_end" or (
                    parsed.get("type") == "control" and parsed.get("action") == "done"
                ):
                    await send_missing_turns()
                await websocket.send_text(msg)
                if delivered_seq:
                    last_sent_seq = delivered_seq
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                "[WS] 圆桌发言同步失败 session={} error_type={}",
                session_id, type(exc).__name__,
            )
            with contextlib.suppress(Exception):
                await websocket.send_text(json_lib.dumps({
                    "type": "control", "session_id": session_id,
                    "action": "resync_required",
                    "payload": {"after_seq": last_sent_seq},
                }, ensure_ascii=False))
                await websocket.close(code=1013, reason="圆桌发言同步失败，请重新连接")

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

                if action in {"user_input", "resume"}:
                    current = bus.get_session(session_id, owner_user_id=owner_user_id)
                    project_id = int(getattr(current, "project_id", 0) or 0)
                    active = await asyncio.to_thread(_is_discussion_execution_active, int(user.id), project_id)
                    if not active:
                        await websocket.send_text(json_lib.dumps({
                            "type": "control", "action": "input_rejected",
                            "payload": {"reason": "当前项目执行权限已撤销，仍可查看历史"},
                        }, ensure_ascii=False))
                        continue
                if action == "user_input":
                    content = data.get("content", "")
                    if not isinstance(content, str) or not content.strip():
                        await websocket.send_text(json_lib.dumps({
                            "type": "control", "action": "input_rejected",
                            "payload": {"reason": "消息不能为空"},
                        }, ensure_ascii=False))
                    elif len(content.strip()) > _MAX_DISCUSSION_USER_INPUT_CHARS:
                        await websocket.send_text(json_lib.dumps({
                            "type": "control", "action": "input_rejected",
                            "payload": {
                                "reason": (
                                    f"这条插话有 {len(content.strip()):,} 个字符，超过单条上限 "
                                    f"{_MAX_DISCUSSION_USER_INPUT_CHARS:,}；内容未截断也未接收，请拆分后重发。"
                                ),
                            },
                        }, ensure_ascii=False))
                    elif bus.accept_user_input(session_id, content):
                        accepted_session = bus.get_session(session_id, owner_user_id=int(user.id))
                        if accepted_session is not None and accepted_session.status == "concluded":
                            from app.services.roundtable_followup_service import ensure_followup_worker

                            ensure_followup_worker(bus, session_id, owner_user_id)
                        await websocket.send_text(json_lib.dumps({
                            "type": "control", "action": "input_accepted",
                            "payload": {"seq": int(getattr(accepted_session, "last_turn_seq", 0) or 0)},
                        }, ensure_ascii=False))
                    else:
                        current_session = bus.get_session(session_id, owner_user_id=int(user.id))
                        phase = str(getattr(current_session, "progress", {}).get("phase") or "")
                        reason = (
                            "报告正在整理，当前消息未发送；结束后可发起续会"
                            if phase in {"summarizing", "extracting", "reporting"}
                            else "讨论尚未启动或追问窗口已结束，消息未发送"
                        )
                        await websocket.send_text(json_lib.dumps({
                            "type": "control", "action": "input_rejected",
                            "payload": {"reason": reason},
                        }, ensure_ascii=False))
                elif action in ("pause", "resume", "stop"):
                    if action == "stop":
                        stop_error = "圆桌讨论已由用户停止"
                        current_session = bus.get_session(
                            session_id, owner_user_id=owner_user_id,
                        )
                        try:
                            with SessionLocal() as stop_db:
                                row = stop_db.query(RoundtableSession).filter(
                                    RoundtableSession.session_id == session_id,
                                    RoundtableSession.owner_user_id == owner_user_id,
                                ).one_or_none()
                                report_task_id = int(
                                    getattr(current_session, "report_task_id", 0)
                                    or getattr(current_session, "task_id", 0)
                                    or getattr(row, "report_task_id", 0)
                                    or getattr(row, "task_id", 0)
                                    or 0
                                )
                                report_disposition = (
                                    cancel_running_discussion_report(
                                        stop_db,
                                        task_id=report_task_id,
                                        owner_user_id=owner_user_id,
                                    )
                                    if report_task_id else "missing"
                                )
                        except Exception:
                            logger.exception(
                                f"[WS] 圆桌停止前无法确认报告状态 session={session_id}"
                            )
                            bus.publish_control(session_id, "info", {
                                "message": "报告状态暂时无法核验，本次未停止圆桌；请稍后重试。",
                            })
                            continue

                        if report_disposition == "terminal":
                            bus.publish_control(session_id, "info", {
                                "message": "报告已经结束，本次未改写已有结果。",
                            })
                            continue

                        cancel_pending(session_id, owner_user_id)
                        if report_task_id:
                            bus.link_report_task(session_id, report_task_id)
                        stop_accepted = bus.request_stop(session_id, reason=stop_error)
                        if stop_accepted:
                            bus.cancel_discussion_task(session_id)
                        if not _publish_cancelled_terminal(
                            bus, session_id, error=stop_error, task_id=report_task_id,
                        ):
                            bus.publish_control(session_id, "info", {
                                "message": "圆桌已结束，未修改其既有终态。",
                            })
                    elif not bus.control_session(session_id, action):
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
