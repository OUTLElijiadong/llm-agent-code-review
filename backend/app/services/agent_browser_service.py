"""持久、单次认领的站内页面桥；客户端观察不授予权限，也不证明业务成功。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.permission_codes import PermissionCode
from app.models.agent_browser_action import AgentBrowserAction
from app.models.agent_response_run import AgentResponseRun
from app.models.user import User
from app.schemas.agent_browser import BrowserAckIn
from app.services import rbac_service

ACTION_TTL_SECONDS = 120
SNAPSHOT_TTL_SECONDS = 120
POLL_SECONDS = 0.2
_ACTIVE = frozenset({"running", "approving", "answering"})
_MUTATIONS = frozenset({"click", "fill", "select", "check"})
_SENSITIVE = re.compile(
    r"password|passwd|api[ _-]?key|secret|token|credential|authorization|private[ _-]?key|"
    r"密码|口令|密钥|令牌|凭据|验证码",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b", re.I
)
_SAFE_TYPES = frozenset(
    {
        "",
        "text",
        "search",
        "email",
        "number",
        "tel",
        "url",
        "textarea",
        "button",
        "submit",
        "reset",
        "checkbox",
        "radio",
        "select",
        "date",
        "datetime-local",
        "time",
        "file",
    }
)
_EVENT_SIGNING_KEY = secrets.token_bytes(32)
_EVENT_FIELDS = (
    "type",
    "action_id",
    "run_id",
    "session_id",
    "surface",
    "account_id",
    "client_nonce",
    "action",
    "args",
    "observation_snapshot_id",
    "expires_at",
    "status",
)
_ROUTES = (
    (r"/", ()),
    (r"/dashboard", ()),
    (r"/projects(?:/\d+)?", ("project:view",)),
    (r"/reviews/start", ("review:start",)),
    (r"/reviews(?:/\d+)?", ("review:view",)),
    (r"/pentests/start", ("pentest:start",)),
    (r"/pentests(?:/[A-Za-z0-9_-]+)?", ("pentest:view",)),
    (r"/(?:security|rules)", ("security:view", "rule:view")),
    (r"/reports(?:/\d+)?", ("report:view",)),
    (r"/report/templates", ("report:template_manage",)),
    (r"/audit", ("audit:view",)),
    (r"/code(?:/\d+(?:/file/\d+(?:/versions)?)?)?", ("file:view",)),
    (r"/issues", ("issue:view",)),
    (r"/agents", ("agent:view", "agent_asset:create")),
    (r"/agent-studio", ("agent_asset:create",)),
    (r"/sandboxes", ()),
    (r"/forum(?:/(?:new|\d+(?:/edit)?))?", ()),
    (r"/knowledge", ()),
    (r"/support(?:/(?:maintenance|feedback))?", ()),
    (r"/profile(?:/(?:personalization|password|api-config))?", ()),
    (
        r"/admin(?:/(?:governance|operations|security-center|access|platform|overview|agents|approvals|agent-releases|beta-codes|policies|tools|knowledge|jobs|observability|rewards|rollback|users|rbac/(?:roles|permissions|users)|ai-logs|report-templates|audit|evolution|skills|embedding|mcp-workers|llm))?",
        (),
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _clean(value: str, limit: int = 500) -> str:
    # 消息、标签只允许有限摘要，不接受控件当前输入值。
    return _SECRET_VALUE.sub("[敏感值已隐藏]", str(value or ""))[:limit]


def authorize_route(db: Session, user: User, route: str) -> str:
    if not isinstance(route, str) or len(route) > 500 or not route.startswith("/") or route.startswith("//"):
        raise ValidationError("页面路径必须是本站已登记的路由")
    if any(char in route for char in ("\\", "%", "#")) or any(ord(char) < 32 for char in route):
        raise ValidationError("页面路径包含不支持的编码或控制字符")
    parts = urlsplit(route)
    if parts.scheme or parts.netloc or parts.fragment:
        raise ValidationError("禁止外部 URL 或脚本导航")
    found = next((permissions for pattern, permissions in _ROUTES if re.fullmatch(pattern, parts.path)), None)
    if found is None:
        raise ValidationError("页面未登记，停止猜测目标")
    if parts.path.startswith("/admin") and not rbac_service.is_admin_user(db, int(user.id)):
        raise ForbiddenError("当前账号不能操作管理页面")
    if parts.path == "/admin/security-center" and not rbac_service.is_super_admin_user(db, int(user.id)):
        raise ForbiddenError("安全中心仅最高管理员可用")
    if found and not any(rbac_service.check_permission(db, int(user.id), permission) for permission in found):
        raise ForbiddenError("当前账号没有此页面权限")
    return route


def _owner_run(
    db: Session, user: User, *, run_id: str, session_key: str, surface: str, expected_token_version: int | None = None
) -> AgentResponseRun:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", session_key or ""):
        raise ValidationError("页面操作必须绑定有效的当前会话")
    original_version = int(user.token_version or 0) if expected_token_version is None else expected_token_version
    actor = db.get(User, int(user.id), populate_existing=True)
    if actor is None or actor.status != 1 or not rbac_service.check_permission(db, actor.id, PermissionCode.AGENT_CHAT):
        raise ForbiddenError("当前账号已停用或小菱权限已撤销")
    if int(actor.token_version or 0) != original_version:
        raise ForbiddenError("页面动作的登录凭据已变更，请重新发起")
    if surface not in {"user", "admin"} or (surface == "admin" and not rbac_service.is_admin_user(db, actor.id)):
        raise ForbiddenError("当前账号没有该会话权限")
    run = (
        db.query(AgentResponseRun)
        .filter_by(run_id=run_id, user_id=actor.id, session_key=session_key, surface=surface)
        .populate_existing()
        .first()
    )
    if run is None:
        raise NotFoundError("页面操作运行不存在")
    if run.status not in _ACTIVE:
        raise ConflictError("小菱运行已停止，不能继续页面操作")
    return run


def _owner_action(db: Session, user: User, action_id: int, session_key: str, surface: str) -> AgentBrowserAction:
    row = (
        db.query(AgentBrowserAction)
        .filter_by(id=action_id, user_id=int(user.id), session_key=session_key, surface=surface)
        .populate_existing()
        .first()
    )
    if row is None:
        raise NotFoundError("页面操作不存在")
    _owner_run(
        db, user, run_id=row.run_id, session_key=session_key, surface=surface, expected_token_version=row.token_version
    )
    return row


def _resolve_arguments(
    db: Session, user: User, run: AgentResponseRun, action: str, args: Mapping[str, Any]
) -> dict[str, Any]:
    keys = {
        "inspect": set(),
        "navigate": {"route"},
        "scroll": {"direction"},
        "click": {"control_handle"},
        "fill": {"control_handle", "value"},
        "select": {"control_handle", "value"},
        "check": {"control_handle", "checked"},
    }
    if action not in keys or not isinstance(args, Mapping) or set(args) != keys[action]:
        raise ValidationError("页面动作参数不符合已登记契约")
    if action == "inspect":
        return {}
    if action == "navigate":
        return {"route": authorize_route(db, user, args["route"])}
    if action == "scroll":
        if args["direction"] not in {"up", "down"}:
            raise ValidationError("滚动方向仅支持 up/down")
        return {"direction": args["direction"]}
    latest = (
        db.query(AgentBrowserAction)
        .filter_by(user_id=int(user.id), run_id=run.run_id, session_key=run.session_key, surface=run.surface)
        .order_by(AgentBrowserAction.id.desc())
        .first()
    )
    if (
        latest is None
        or latest.status != "completed"
        or not latest.observation_json
        or (_now() - latest.update_time).total_seconds() > SNAPSHOT_TTL_SECONDS
    ):
        raise ConflictError("当前页面快照已过期或状态不确定，请先重新读取页面")
    snapshot = json.loads(latest.observation_json)
    route = authorize_route(db, user, snapshot["route"])
    control = next((item for item in snapshot["controls"] if item["control_handle"] == args["control_handle"]), None)
    if control is None:
        raise ConflictError("控件不属于最新页面快照，请重新读取页面")
    if _SENSITIVE.search(control["label"] + " " + control["type"]) or control["type"] == "password":
        raise ForbiddenError("密码、密钥等秘密只能由用户在安全输入框内操作")
    if control["type"] == "file":
        raise ConflictError("文件选择需要用户在浏览器内选择，不能代读本地文件")
    if action != "click" and control["readonly"]:
        raise ForbiddenError("控件当前不可编辑")
    if action == "fill" and control["role"] not in {"textbox", "combobox"}:
        raise ValidationError("该控件不接受文本输入")
    if action == "select" and control["role"] not in {"combobox", "option"}:
        raise ValidationError("该控件不是选择器")
    if action == "check" and (control["role"] not in {"checkbox", "radio"} or not isinstance(args["checked"], bool)):
        raise ValidationError("勾选动作仅接受布尔值和勾选控件")
    resolved = {"route": route, "snapshot_id": snapshot["snapshot_id"], "client_handle": control["client_handle"]}
    if "value" in args:
        if not isinstance(args["value"], str) or len(args["value"]) > 20_000 or _SECRET_VALUE.search(args["value"]):
            raise ValidationError("输入必须为非秘密文本，长度不超过 20000 字符")
        resolved["value"] = args["value"]
    if "checked" in args:
        resolved["checked"] = args["checked"]
    return resolved


def _required(row: AgentBrowserAction, nonce: str) -> dict[str, Any]:
    return {
        "type": "response.browser.required",
        "action_id": row.id,
        "run_id": row.run_id,
        "session_id": row.session_key,
        "surface": row.surface,
        "account_id": row.user_id,
        "client_nonce": nonce,
        "action": row.action,
        "args": json.loads(row.arguments_json),
        "observation_snapshot_id": row.observation_snapshot_id,
        "expires_at": row.expires_at.replace(tzinfo=timezone.utc).isoformat(),
        "status": row.status,
    }


def public_required_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """仅接受本进程 helper 签发的实时事件，上游同名事件不能进入执行桥。"""
    public = {key: event.get(key) for key in _EVENT_FIELDS}
    signature = str(event.get("_browser_signature") or "")
    expected = hmac.new(_EVENT_SIGNING_KEY, _json(public).encode(), hashlib.sha256).hexdigest()
    return public if hmac.compare_digest(signature, expected) else None


def _signed_required(row: AgentBrowserAction, nonce: str) -> dict[str, Any]:
    event = _required(row, nonce)
    return {
        **event,
        "_browser_signature": hmac.new(_EVENT_SIGNING_KEY, _json(event).encode(), hashlib.sha256).hexdigest(),
    }


def _expire(db: Session, row: AgentBrowserAction) -> None:
    if row.status in {"queued", "executing"} and row.expires_at <= _now():
        # 认领后可能已经写入，绝不自动重排为 queued。
        status = "indeterminate" if row.status == "executing" else "failed"
        db.query(AgentBrowserAction).filter_by(id=row.id, status=row.status).update(
            {"status": status, "error": "浏览器未按时交回观察；不会重放可能已执行的操作"}, synchronize_session=False
        )
        db.commit()
        db.refresh(row)


def _result(row: AgentBrowserAction) -> dict[str, Any]:
    observation = json.loads(row.observation_json) if row.observation_json else None
    if observation:
        observation = {
            **observation,
            "controls": [
                {key: value for key, value in item.items() if key != "client_handle"}
                for item in observation["controls"]
            ],
        }
    return {
        "action_id": row.id,
        "status": row.status,
        "completed": row.status == "completed",
        "verification_status": "client_observed" if observation else "unverified",
        "evidence_kind": "client_observed",
        "server_verified": False,
        "observation": observation,
        "error": row.error or "",
        "retryable": False,
        "notice": "这是当前浏览器观察，业务写入是否成功须用业务接口独立核验",
    }


async def request_action(
    db: Session,
    user: User,
    *,
    run_id: str,
    session_key: str,
    surface: str,
    call_id: str,
    action: str,
    args: Mapping[str, Any],
    event_sink,
    approved: bool = False,
) -> dict[str, Any]:
    """只接收主控已复核的动作；返回观察摘要，不向模型返回客户端凭据。"""
    run = _owner_run(db, user, run_id=run_id, session_key=session_key, surface=surface)
    if action in _MUTATIONS and not approved:
        raise ForbiddenError("页面操作尚未由当前账号确认")
    request_id = _hash(f"{user.id}:{run_id}:{call_id}")
    fingerprint = _hash(_json({"action": action, "args": dict(args)}))
    row = db.query(AgentBrowserAction).filter_by(request_id=request_id).first()
    if row is not None:
        if row.action != action or row.input_sha256 != fingerprint:
            raise ConflictError("同一工具调用不得改变页面动作")
        _expire(db, row)
        # 重入时不给第二个执行凭据；queued 由所属客户端 pending 恢复。
        if row.status not in {"queued", "executing"}:
            return _result(row)
    else:
        # 锁住当前 run，使同一会话每次只有一个未确认动作。
        db.query(AgentResponseRun).filter_by(id=run.id).with_for_update().first()
        active = (
            db.query(AgentBrowserAction)
            .filter_by(user_id=user.id, run_id=run_id)
            .filter(AgentBrowserAction.status.in_(("queued", "executing")))
            .first()
        )
        if active is not None:
            _expire(db, active)
            if active.status in {"queued", "executing"}:
                raise ConflictError("上一页面动作尚未交回观察，不能并行猜测下一步")
        resolved = _resolve_arguments(db, user, run, action, args)
        nonce = secrets.token_urlsafe(32)
        row = AgentBrowserAction(
            request_id=request_id,
            input_sha256=fingerprint,
            user_id=user.id,
            token_version=int(user.token_version or 0),
            run_id=run_id,
            session_key=session_key,
            surface=surface,
            call_id=call_id,
            action=action,
            status="queued",
            arguments_json=_json(resolved),
            observation_snapshot_id=secrets.token_hex(16),
            nonce_sha256=_hash(nonce),
            expires_at=_now() + timedelta(seconds=ACTION_TTL_SECONDS),
        )
        db.add(row)
        db.commit()
        if event_sink is not None:
            emitted = event_sink(_signed_required(row, nonce))
            if inspect.isawaitable(emitted):
                await emitted
    while True:
        await asyncio.sleep(POLL_SECONDS)
        # ACK 由独立 HTTP 请求提交。MySQL 默认 REPEATABLE READ 会让连续
        # refresh 仍停留在首次 queued 快照；每轮结束读事务，才能观察真实回执
        # 及账号撤权。此处不保留业务写入事务，不能靠增加超时掩盖旧快照。
        db.commit()
        db.refresh(row)
        try:
            _owner_run(
                db,
                user,
                run_id=run_id,
                session_key=session_key,
                surface=surface,
                expected_token_version=row.token_version,
            )
        except (ForbiddenError, ConflictError):
            db.query(AgentBrowserAction).filter_by(id=row.id, status=row.status).update(
                {
                    "status": "indeterminate" if row.status == "executing" else "cancelled",
                    "error": "运行或账号权限已变更，停止页面动作，不重放可能已执行的操作",
                },
                synchronize_session=False,
            )
            db.commit()
            raise
        _expire(db, row)
        if row.status not in {"queued", "executing"}:
            return _result(row)


def pending_actions(db: Session, user: User, *, run_id: str, session_key: str, surface: str) -> dict[str, Any]:
    _owner_run(db, user, run_id=run_id, session_key=session_key, surface=surface)
    rows = (
        db.query(AgentBrowserAction)
        .filter_by(user_id=user.id, run_id=run_id, session_key=session_key, surface=surface)
        .filter(AgentBrowserAction.status.in_(("queued", "executing")))
        .all()
    )
    actions = []
    for row in rows:
        if row.token_version != int(user.token_version or 0):
            continue
        _expire(db, row)
        if row.status == "queued":
            nonce = secrets.token_urlsafe(32)
            changed = (
                db.query(AgentBrowserAction)
                .filter_by(id=row.id, status="queued")
                .update({"nonce_sha256": _hash(nonce)}, synchronize_session=False)
            )
            db.commit()
            db.refresh(row)
            if changed and row.status == "queued":
                actions.append(_required(row, nonce))
        elif row.status == "executing":
            actions.append(
                {
                    "action_id": row.id,
                    "run_id": row.run_id,
                    "session_id": row.session_key,
                    "surface": row.surface,
                    "account_id": row.user_id,
                    "status": "executing",
                }
            )
    return {"actions": actions}


def claim_action(
    db: Session, user: User, action_id: int, *, session_key: str, surface: str, nonce: str
) -> dict[str, Any]:
    row = _owner_action(db, user, action_id, session_key, surface)
    _expire(db, row)
    claim = secrets.token_urlsafe(32)
    changed = (
        db.query(AgentBrowserAction)
        .filter_by(id=row.id, status="queued", nonce_sha256=_hash(nonce))
        .filter(AgentBrowserAction.expires_at > _now())
        .update({"status": "executing", "claim_sha256": _hash(claim)}, synchronize_session=False)
    )
    db.commit()
    if not changed:
        raise ConflictError("动作已被另一页面认领、已过期或凭据失效；禁止重复点击")
    return {"action_id": row.id, "claim_token": claim}


def acknowledge_action(db: Session, user: User, action_id: int, payload: BrowserAckIn) -> dict[str, Any]:
    row = _owner_action(db, user, action_id, payload.session_id, payload.surface)
    _expire(db, row)
    if (
        row.status != "executing"
        or not hmac.compare_digest(row.nonce_sha256, _hash(payload.client_nonce))
        or not hmac.compare_digest(row.claim_sha256 or "", _hash(payload.claim_token))
    ):
        raise ConflictError("页面动作已结束或认领凭据无效")
    observation = payload.observation
    if observation.snapshot_id != row.observation_snapshot_id:
        raise ConflictError("观察不属于本次动作的页面快照")
    route = authorize_route(db, user, observation.route)
    controls = []
    seen = set()
    for item in observation.controls:
        if item.client_handle in seen:
            raise ValidationError("页面控件句柄重复")
        seen.add(item.client_handle)
        if (
            item.type not in _SAFE_TYPES
            or _SENSITIVE.search(item.label + " " + item.type)
            or _SECRET_VALUE.search(item.label)
        ):
            continue
        controls.append(
            {**item.model_dump(), "label": _clean(item.label, 160), "control_handle": "ctrl_" + secrets.token_hex(16)}
        )
    saved = {
        "snapshot_id": observation.snapshot_id,
        "route": route,
        "title": _clean(observation.title, 160),
        "controls": controls,
        "messages": [_clean(message) for message in observation.messages],
        "evidence_kind": "client_observed",
    }
    changed = (
        db.query(AgentBrowserAction)
        .filter_by(id=row.id, status="executing", claim_sha256=_hash(payload.claim_token))
        .update(
            {"status": payload.outcome, "observation_json": _json(saved), "error": _clean(payload.error)},
            synchronize_session=False,
        )
    )
    db.commit()
    if not changed:
        raise ConflictError("观察已经交回，不能重复确认")
    db.refresh(row)
    return _result(row)
