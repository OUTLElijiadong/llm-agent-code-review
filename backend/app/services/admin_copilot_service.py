"""管理员副驾驶的对话协议、意图路由与安全确认流程。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from typing import Any, Optional

import jwt
from sqlalchemy.orm import Session

from app.agents.admin_copilot_agent import AdminCopilotAgent, DelegatedAdminAgent
from app.agents.contracts import CONTRACTS
from app.agents.events import new_trace_id
from app.agents.operations_agent import OperationsAgent
from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.agent_governance import AgentProfile
from app.models.user import User
from app.services import (
    admin_agent_tools,
    admin_chat_history_service,
    agent_governance_service,
    observability_service,
)

ASSISTANT_NAME = "Prism 管理副驾驶"
OPENING = (
    "我是管理副驾驶。你可以问我：进度怎么样、还有什么没做；"
    "也可以直接让我：查询用户、调整角色、启停 Agent、生成汇报。需要我做什么？"
)
TOKEN_TTL_MINUTES = 15
_CONFIRMATION_LOCK = Lock()


def handle_message(
    db: Session,
    admin: User,
    *,
    message: str,
    session_id: str,
    action_token: str = "",
    decision: str = "",
    confirmation_text: str = "",
) -> dict[str, Any]:
    """处理并持久化一条管理员对话，不允许写操作越过确认阶段。"""
    normalized = message.strip()
    session = admin_chat_history_service.get_or_create_session(db, admin, session_id)
    if action_token:
        data = _handle_confirmation(
            db,
            admin,
            session_id=session_id,
            action_token=action_token,
            decision=decision,
            confirmation_text=confirmation_text,
        )
        admin_chat_history_service.mark_action(
            db,
            session,
            action_token,
            "confirmed" if decision == "confirm" else "cancelled",
        )
        assistant_row = admin_chat_history_service.append_message(
            db, session, role="assistant", payload=data, agent_code="manager",
        )
        data["message_id"] = assistant_row.id
        return data
    if not normalized:
        raise ValidationError("消息不能为空")

    user_row = admin_chat_history_service.append_user_text(db, session, normalized)
    data = _handle_fresh_message(db, admin, normalized, session)
    agent_code = str(data.pop("_agent_code", "manager"))
    trace_id = str(data.pop("_trace_id", ""))
    assistant_row = admin_chat_history_service.append_message(
        db,
        session,
        role="assistant",
        payload=data,
        agent_code=agent_code,
        trace_id=trace_id,
    )
    data["message_id"] = assistant_row.id
    data["user_message_id"] = user_row.id
    return data


def _handle_fresh_message(
    db: Session,
    admin: User,
    normalized: str,
    session,
) -> dict[str, Any]:
    """先走确定性安全路由，未命中时再调用真实管理 Agent。"""

    # 管理员明确指定 Agent 时，委派意图高于消息中的“风险/用户/状态”等
    # 业务关键词；operations 仍会在 _ai_or_delegate 内进入运维确认边界。
    if any(word in normalized for word in ("调用", "请让", "让")):
        agent_governance_service.sync_profiles(db)
        enabled_profiles = [
            {"code": row.code, "name": row.name}
            for row in db.query(AgentProfile).filter(AgentProfile.is_enabled == 1).all()
        ]
        if _explicit_agent_target(normalized, enabled_profiles):
            return _ai_or_delegate(db, admin, normalized, session)

    write_preview = _match_write_intent(db, admin, normalized, session.session_key)
    if write_preview:
        return write_preview

    ops_response = _match_ops_intent(db, admin, normalized, session)
    if ops_response:
        return ops_response

    lowered = normalized.lower()
    if any(word in normalized for word in ("你好", "你是谁", "能做什么", "帮助")):
        return _text(OPENING)
    if any(word in normalized for word in ("日报", "周报", "进度怎么样", "运行概况", "总体情况")):
        return _build_report(db, admin)
    if any(word in normalized for word in ("异常", "告警", "风险", "阻塞")):
        return _build_alert(db)
    if any(word in normalized for word in ("还有什么没做", "待办", "待审批", "审批事项")):
        return _approval_table(db, admin)
    if any(word in normalized for word in ("用户", "成员", "账号")):
        return _user_table(db, admin, normalized)
    if "角色" in normalized:
        return _role_table(db, admin)
    if "agent" in lowered or "智能体" in normalized:
        return _agent_table(db, admin)
    if any(word in normalized for word in ("服务器", "系统状态", "资源占用", "运行状态")):
        return _system_table(db, admin)

    return _ai_or_delegate(db, admin, normalized, session)


def _ai_or_delegate(db: Session, admin: User, message: str, session) -> dict[str, Any]:
    trace_id = new_trace_id()
    # 将运行时 Agent 和治理 Agent 同步到同一份可委派画像，
    # 确保管理 Agent 不依赖前端页面曾经打开过。
    agent_governance_service.sync_profiles(db)
    profiles = [
        {
            "code": row.code,
            "name": row.name,
            "description": row.description or "",
            "category": row.category,
        }
        for row in db.query(AgentProfile).filter(AgentProfile.is_enabled == 1).order_by(AgentProfile.code.asc()).all()
    ]
    snapshot = _admin_fact_snapshot(db, admin)

    explicit = _explicit_agent_target(message, profiles)
    if explicit:
        task = re.sub(r"^(?:请)?(?:调用|让|请让)\s*", "", message, count=1).strip() or message
        if explicit.get("code") == "operations":
            ops_response = _match_ops_intent(db, admin, task, session)
            if ops_response:
                return ops_response
        return _delegate(db, admin, explicit, task, snapshot, trace_id)

    manager = AdminCopilotAgent()
    plan = manager.plan(
        db,
        admin,
        message=message,
        history=admin_chat_history_service.recent_context(db, session),
        snapshot=snapshot,
        agents=profiles,
        trace_id=trace_id,
    )
    if not plan.success or not isinstance(plan.data, dict):
        return {
            **_text(
                f"管理 Agent 调用失败：{plan.error or '模型没有返回有效 JSON'}。"
                "没有执行任何写操作。",
                status="failed",
            ),
            "_agent_code": "manager",
            "_trace_id": trace_id,
        }
    mode = str(plan.data.get("mode") or "answer")
    if mode == "delegate":
        code = str(plan.data.get("agent_code") or "")
        target = next((item for item in profiles if item["code"] == code), None)
        if not target:
            return {
                **_text("管理 Agent 返回了不可用的委派目标，没有执行调用。", status="failed"),
                "_agent_code": "manager",
                "_trace_id": trace_id,
            }
        return _delegate(db, admin, target, str(plan.data.get("task") or message), snapshot, trace_id)
    return {
        **_text(str(plan.data.get("answer") or "没有查到足够事实，无法给出结论。")),
        "_agent_code": "manager",
        "_trace_id": trace_id,
    }


def _explicit_agent_target(message: str, profiles: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    lowered = message.lower()
    if not any(word in message for word in ("调用", "请让", "让")):
        return None
    for item in sorted(profiles, key=lambda row: len(str(row["code"])), reverse=True):
        if str(item["code"]).lower() in lowered or str(item["name"]) in message:
            return item
    return None


def _delegate(
    db: Session,
    admin: User,
    target: dict[str, Any],
    task: str,
    snapshot: dict[str, Any],
    trace_id: str,
) -> dict[str, Any]:
    code = str(target["code"])
    contract = CONTRACTS.get(code)
    if contract:
        prompt = contract.system_prompt()
    else:
        prompt = (
            f"你是棱镜 Prism 的 {target.get('name') or code}（agent_code={code}）。"
            f"职责：{target.get('description') or '仅按治理画像处理管理员委派'}。"
            "只依据提供事实回答，不执行写操作，不编造数据。"
        )
    delegated = DelegatedAdminAgent(code=code, name=str(target.get("name") or code), system_prompt=prompt)
    result = delegated.run(db, admin, task=task, snapshot=snapshot, trace_id=trace_id)
    if not result.success:
        content = f"{target.get('name') or code} 调用失败：{result.error or '未知错误'}。"
        status = "failed"
    else:
        content = str(result.data or "没有返回内容。")
        status = "completed"
    return {
        **_text(content, status=status),
        "_agent_code": code,
        "_trace_id": trace_id,
    }


def _admin_fact_snapshot(db: Session, admin: User) -> dict[str, Any]:
    users = admin_agent_tools.admin_list_users(db, admin, page_size=1).data or {}
    agents = admin_agent_tools.admin_list_agents(db, admin).data or []
    overview = admin_agent_tools.admin_governance_overview(db, admin).data or {}
    system = admin_agent_tools.admin_system_status(db, admin).data or {}
    return {
        "users_total": int(users.get("total", 0)),
        "agents_total": len(agents),
        "agents_enabled": sum(1 for item in agents if item.get("is_enabled")),
        "governance": overview,
        "system": system,
    }


def _match_write_intent(db: Session, admin: User, message: str, session_id: str) -> Optional[dict[str, Any]]:
    role_match = re.search(
        r"(?:把|将|修改)?\s*(?:用户|账号)?\s*[#＃]?(\d+)\s*(?:的)?\s*(?:角色)?\s*"
        r"(?:改为|修改为|设为|设置为|调整为)\s*(admin|user|reviewer|管理员|普通用户|审查员)",
        message,
        re.IGNORECASE,
    )
    if role_match:
        user_id = int(role_match.group(1))
        role_alias = {"管理员": "admin", "普通用户": "user", "审查员": "reviewer"}
        role = role_alias.get(role_match.group(2), role_match.group(2).lower())
        target = db.get(User, user_id)
        if not target or target.status == -1:
            return _text(f"没有查到用户 #{user_id}，请先核对用户 ID。")
        operation = f"把用户“{target.username}”（#{user_id}）的角色改为 {role}"
        return _confirmation_card(
            admin,
            session_id,
            action="set_user_role",
            params={"user_id": user_id, "role": role},
            operation=operation,
            impact="影响 1 个用户；确认后新角色立即生效并写入系统审计。",
            danger=False,
        )

    delete_match = re.search(r"删除\s*(?:用户|账号)?\s*[#＃]?(\d+)", message)
    if delete_match:
        user_id = int(delete_match.group(1))
        target = db.get(User, user_id)
        if not target or target.status == -1:
            return _text(f"没有查到用户 #{user_id}，请先核对用户 ID。")
        operation = f"删除用户“{target.username}”（#{user_id}）"
        return _confirmation_card(
            admin,
            session_id,
            action="delete_user",
            params={"user_id": user_id},
            operation=operation,
            impact="影响 1 个用户账号；历史项目、帖子和审计记录保留。",
            consequence="账号将被软删除并立即失去登录能力；确认后立即执行，不能在当前界面直接撤销。",
            danger=True,
        )

    toggle_match = re.search(
        r"(启用|开启|停用|禁用|关闭)\s*(?:Agent|agent|智能体)?\s*[“\"']?([\w-]+)[”\"']?",
        message,
        re.IGNORECASE,
    )
    if toggle_match:
        enable = toggle_match.group(1) in ("启用", "开启")
        raw_code = toggle_match.group(2)
        profile = _resolve_agent(db, raw_code)
        if not profile:
            return _text(f"没有查到 Agent“{raw_code}”，请使用 Agent 编码或完整名称。")
        action_text = "启用" if enable else "停用"
        operation = f"{action_text} Agent“{profile.name}”（{profile.code}）"
        return _confirmation_card(
            admin,
            session_id,
            action="toggle_agent",
            params={"agent_code": profile.code, "enable": enable},
            operation=operation,
            impact="影响 1 个 Agent；确认后立即改变其可调度状态并写入系统审计。",
            danger=False,
        )
    return None


def _resolve_agent(db: Session, value: str) -> Optional[AgentProfile]:
    exact = db.query(AgentProfile).filter(AgentProfile.code == value).first()
    if exact:
        return exact
    return db.query(AgentProfile).filter(AgentProfile.name == value).first()


def _match_ops_intent(db: Session, admin: User, message: str, session) -> Optional[dict[str, Any]]:
    """将管理员自然语言限定到宿主机执行器的白名单动作。"""
    normalized = message.strip()
    lowered = normalized.lower()
    action = ""
    params: dict[str, Any] = {}
    operation = ""
    impact = ""
    consequence = ""

    if any(word in normalized for word in ("运维状态", "生产巡检", "健康检查", "服务器巡检", "服务器状态")):
        action = "status"
    elif any(word in normalized for word in ("证书状态", "查看证书", "证书过期")):
        action = "certificate_status"
    elif any(word in normalized for word in ("备份数据库", "创建备份", "立即备份")):
        action = "backup_database"
    elif any(word in normalized for word in ("验证备份", "校验备份")):
        action = "verify_backup"
    elif any(word in normalized for word in ("重载 nginx", "nginx 重载", "reload nginx")):
        action, operation = "nginx_reload", "校验 Nginx 配置并热重载"
        impact = "影响生产入口流量；执行前会先运行 nginx -t。"
    elif any(word in normalized for word in ("续期证书", "更新证书", "刷新证书")):
        action, operation = "renew_certificate", "续期生产 TLS 证书"
        impact = "执行证书续期脚本并重载 Nginx。"
    elif any(word in normalized for word in ("数据库维护", "数据库优化", "检查数据库")):
        action, operation = "database_maintenance", "执行生产数据库检查与维护"
        impact = "可能增加 MySQL I/O 和锁等待，执行结果写入审计。"
    elif any(word in normalized for word in ("清理旧备份", "清理镜像", "清理运维文件")):
        action, operation = "cleanup", "清理白名单内的过期备份和容器构建缓存"
        impact = "会删除过期运维产物，影响的文件无法从聊天界面恢复。"
        consequence = "清理结果不可撤销，仅保留执行时规则允许的备份。"
    elif "回滚" in normalized and any(word in normalized for word in ("应用", "版本", "系统", "生产")):
        action, operation = "rollback_application", "回滚生产应用到已验证的上一版本"
        impact = "会重建生产容器，期间可能出现短暂不可用。"
        consequence = "当前版本将被替换，如果数据库结构不兼容可能导致二次故障。"
    elif any(
        word in normalized
        for word in ("重启服务", "重启容器", "重启后端", "重启前端", "重启 mysql", "重启 redis")
    ):
        service_aliases = {
            "后端": "backend", "backend": "backend", "前端": "frontend", "frontend": "frontend",
            "mysql": "mysql", "数据库": "mysql", "redis": "redis", "clamav": "clamav",
        }
        service = next((value for key, value in service_aliases.items() if key in lowered), "")
        if not service:
            return _text("请指定要重启的服务：backend、frontend、mysql、redis 或 clamav。")
        action, params = "restart_service", {"service": service}
        operation = f"重启生产服务 {service}"
        impact = "该服务会短暂不可用；执行器会等待容器恢复健康。"
    elif any(word in normalized for word in ("修改配置", "更新配置", "设置配置")):
        match = re.search(
            r"(LOG_LEVEL|REVIEW_MAX_CONCURRENCY|(?:BACKEND|FRONTEND|MYSQL|REDIS|CLAMAV)_MEM_LIMIT|OPS_(?:DISK|MEMORY)_MAX_PERCENT)\s*(?:=|为|改为|设为)\s*([A-Za-z0-9]+)",
            normalized,
            re.IGNORECASE,
        )
        if not match:
            return _text("请给出白名单配置键和值，例如：修改配置 LOG_LEVEL 为 INFO。")
        key, value = match.group(1).upper(), match.group(2)
        action, params = "update_config", {"key": key, "value": value}
        operation = f"修改生产配置 {key}={value}"
        impact = "写入生产 .env；本次不自动重启服务，新值可能需重启后生效。"
    elif "恢复数据库" in normalized or "数据库恢复" in normalized:
        match = re.search(r"(code_review_[A-Za-z0-9_.-]+\.sql\.gz)", normalized)
        if not match:
            return _text("请指定要恢复的备份文件名，且必须位于生产 backups 目录。")
        action, params = "restore_database", {"file": match.group(1)}
        operation = f"使用 {match.group(1)} 覆盖恢复生产数据库"
        impact = "当前生产数据将被备份文件覆盖，期间数据库不可用。"
        consequence = "备份时间点之后的数据可能丢失，点击确认按钮即可继续，无需输入确认词。"

    if not action:
        return None
    if action in ("status", "certificate_status", "backup_database", "verify_backup"):
        return _execute_ops_action(db, admin, session, action, params)
    danger = action in ("cleanup", "rollback_application", "restore_database")
    return _confirmation_card(
        admin,
        session.session_key,
        action="ops_execute",
        params={"ops_action": action, "ops_params": params},
        operation=operation,
        impact=impact,
        consequence=consequence,
        danger=danger,
    )


def _execute_ops_action(
    db: Session,
    admin: User,
    session,
    action: str,
    params: Optional[dict[str, Any]] = None,
    *,
    request_id: str = "",
) -> dict[str, Any]:
    trace_id = new_trace_id()
    result = OperationsAgent().execute_action(
        db,
        admin,
        action=action,
        params=params or {},
        request_id=request_id,
        session_db_id=session.id,
    )
    if not result.success or not isinstance(result.data, dict):
        return {
            **_text(f"运维 Agent 执行失败：{result.error or '未知错误'}。", status="failed"),
            "_agent_code": "operations",
            "_trace_id": trace_id,
        }
    return {
        **_ops_execution_receipt(result.data),
        "_agent_code": "operations",
        "_trace_id": trace_id,
    }


def create_ops_confirmation(
    db: Session,
    admin: User,
    *,
    session,
    action: str,
    params: Optional[dict[str, Any]] = None,
    operation: str,
    impact: str,
    consequence: str = "",
    danger: bool = False,
) -> dict[str, Any]:
    """为主动运维异常创建持久化确认卡，供悬浮窗轮询恢复。"""
    card = _confirmation_card(
        admin,
        session.session_key,
        action="ops_execute",
        params={"ops_action": action, "ops_params": params or {}},
        operation=operation,
        impact=impact,
        consequence=consequence,
        danger=danger,
    )
    row = admin_chat_history_service.append_message(
        db,
        session,
        role="assistant",
        payload=card,
        agent_code="operations",
        trace_id=new_trace_id(),
    )
    card["message_id"] = row.id
    return card


def _confirmation_card(
    admin: User,
    session_id: str,
    *,
    action: str,
    params: dict[str, Any],
    operation: str,
    impact: str,
    danger: bool,
    consequence: str = "",
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    token = _encode_action_token(
        admin,
        session_id=session_id,
        action=action,
        params=params,
        danger=danger,
        request_id=request_id,
    )
    return {
        "type": "danger_confirm" if danger else "confirm",
        "title": "危险操作确认" if danger else "操作确认",
        "operation": operation,
        "impact": impact,
        "consequence": consequence,
        "action_token": token,
        "status": "pending",
    }


def _handle_confirmation(
    db: Session,
    admin: User,
    *,
    session_id: str,
    action_token: str,
    decision: str,
    confirmation_text: str,
) -> dict[str, Any]:
    payload = _decode_action_token(action_token, admin, session_id)
    if decision == "cancel":
        return _text("操作已取消，没有修改任何数据。", status="cancelled")
    if decision != "confirm":
        raise ValidationError("确认决定必须是 confirm 或 cancel")
    request_id = str(payload["request_id"])
    with _CONFIRMATION_LOCK:
        action = str(payload["action"])
        params = dict(payload.get("params") or {})
        context = {**params, "copilot_request_id": request_id, "session_id": session_id}
        if action == "set_user_role":
            result = admin_agent_tools.admin_set_user_role(
                db, admin, int(params["user_id"]), str(params["role"]), context=context,
            )
        elif action == "delete_user":
            result = admin_agent_tools.admin_delete_user(
                db, admin, int(params["user_id"]), context=context,
            )
        elif action == "toggle_agent":
            result = admin_agent_tools.admin_toggle_agent(
                db, admin, str(params["agent_code"]), bool(params["enable"]), context=context,
            )
        elif action == "ops_execute":
            session = admin_chat_history_service.get_or_create_session(db, admin, session_id)
            ops_action = str(params.get("ops_action") or "")
            ops_params = params.get("ops_params") if isinstance(params.get("ops_params"), dict) else {}
            return _execute_ops_action(
                db,
                admin,
                session,
                ops_action,
                ops_params,
                request_id=request_id,
            )
        else:
            raise ValidationError("确认令牌中的操作不受支持")
        if not result.success:
            return _text(result.error or "操作未执行", status="failed")
        data = result.data if isinstance(result.data, dict) else {}
        return _execution_receipt(
            int(data["tool_call_id"]),
            str(data.get("title") or "管理操作"),
            duplicate=bool(data.get("duplicate")),
        )


def _encode_action_token(
    admin: User,
    *,
    session_id: str,
    action: str,
    params: dict[str, Any],
    danger: bool,
    request_id: str,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(admin.id),
        "purpose": "admin_copilot_action",
        "session_id": session_id,
        "action": action,
        "params": params,
        "danger": danger,
        "request_id": request_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=TOKEN_TTL_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, _action_signing_key(), algorithm=settings.jwt_algorithm)


def _decode_action_token(token: str, admin: User, session_id: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            _action_signing_key(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "purpose"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ValidationError("确认卡已过期，请重新发起操作") from exc
    except jwt.InvalidTokenError as exc:
        raise ValidationError("确认卡无效，请重新发起操作") from exc
    if payload.get("purpose") != "admin_copilot_action":
        raise ValidationError("确认卡用途无效")
    if str(payload.get("sub")) != str(admin.id) or payload.get("session_id") != session_id:
        raise ValidationError("确认卡与当前管理员会话不匹配")
    return payload


def _action_signing_key() -> str:
    """派生独立签名密钥，防止确认令牌被当作登录令牌使用。"""
    return sha256(f"{settings.jwt_secret}:admin-copilot-action".encode("utf-8")).hexdigest()


def _build_report(db: Session, admin: User) -> dict[str, Any]:
    overview = admin_agent_tools.admin_governance_overview(db, admin).data or {}
    agents = admin_agent_tools.admin_list_agents(db, admin).data or []
    approvals = admin_agent_tools.admin_list_approvals(db, admin, status="").data or []
    alerts = observability_service.list_alerts(db, status="open", limit=3)
    pending = sum(1 for item in approvals if item.get("status") == "pending")
    completed = sum(1 for item in approvals if item.get("status") in ("approved", "auto_approved"))
    enabled = sum(1 for agent in agents if bool(agent.get("is_enabled")))
    risks = [f"{row.severity}：{row.title}" for row in alerts]
    if pending:
        risks.append(f"有 {pending} 条管理操作等待审批")
    healthy = not alerts and pending == 0
    return {
        "type": "report",
        "title": "管理日报",
        "summary": (
            f"运行{'健康' if healthy else '需要关注'}：{enabled}/{len(agents)} 个 Agent 已启用，"
            f"{pending} 条待审批，{int(overview.get('open_alerts', 0))} 条开放告警。"
        ),
        "counts": {"completed": completed, "in_progress": pending, "not_started": 0},
        "count_labels": {"completed": "已完成审批", "in_progress": "待处理审批", "not_started": "未开始"},
        "risks": risks[:3],
        "suggestions": _report_suggestions(pending, len(alerts), enabled, len(agents)),
    }


def _report_suggestions(pending: int, alerts: int, enabled: int, total: int) -> list[str]:
    suggestions: list[str] = []
    if alerts:
        suggestions.append("先处理高等级开放告警，并记录处置结论。")
    if pending:
        suggestions.append("复核待审批操作的影响范围，逐条决定通过或拒绝。")
    if enabled < total:
        suggestions.append("核对停用 Agent 是否符合当前调度计划。")
    if not suggestions:
        suggestions.append("保持当前策略，继续观察工具失败率和告警变化。")
    return suggestions[:3]


def _build_alert(db: Session) -> dict[str, Any]:
    alerts = observability_service.list_alerts(db, status="open", limit=3)
    if not alerts:
        return _text("当前没有查到开放告警。")
    top = alerts[0]
    return {
        "type": "alert",
        "title": top.title,
        "severity": top.severity,
        "description": f"当前共有 {len(alerts)} 条优先告警；最高优先级为“{top.title}”。",
        "impact": "可能影响 Agent 调度、工具执行或治理数据完整性，具体以告警详情为准。",
        "suggestion": "打开监控告警页核对证据和影响范围，再决定是否关闭或调整策略。",
        "action_label": "立即处理",
        "action_prompt": "查询开放告警并给出处理顺序",
    }


def _user_table(db: Session, admin: User, message: str) -> dict[str, Any]:
    keyword_match = re.search(
        r"(?:搜索|查找|查询)(?:用户|成员|账号)?\s+(?:名为)?\s*[“\"']?([^，。\s]+)",
        message,
    )
    keyword = keyword_match.group(1) if keyword_match else ""
    result = admin_agent_tools.admin_list_users(db, admin, keyword=keyword, page_size=100)
    data = result.data or {}
    rows = [
        {
            "ID": item.get("id"),
            "用户名": item.get("username"),
            "昵称": item.get("nickname") or "-",
            "角色": item.get("role"),
            "状态": "启用" if item.get("status") == 1 else "禁用",
        }
        for item in data.get("users", [])
    ]
    return _table("用户列表", ["ID", "用户名", "昵称", "角色", "状态"], rows, int(data.get("total", 0)))


def _role_table(db: Session, admin: User) -> dict[str, Any]:
    rows = [
        {"编码": item.get("code"), "名称": item.get("name"), "说明": item.get("description") or "-"}
        for item in (admin_agent_tools.admin_list_roles(db, admin).data or [])
    ]
    return _table("角色列表", ["编码", "名称", "说明"], rows, len(rows))


def _agent_table(db: Session, admin: User) -> dict[str, Any]:
    agent_governance_service.sync_profiles(db)
    rows = [
        {
            "编码": item.get("agent_code"),
            "名称": item.get("name"),
            "运行状态": item.get("status"),
            "调度": "启用" if item.get("is_enabled") else "停用",
        }
        for item in (admin_agent_tools.admin_list_agents(db, admin).data or [])
    ]
    return _table("Agent 列表", ["编码", "名称", "运行状态", "调度"], rows, len(rows))


def _approval_table(db: Session, admin: User) -> dict[str, Any]:
    rows = [
        {
            "ID": item.get("id"),
            "事项": item.get("title"),
            "动作": item.get("action"),
            "风险": item.get("risk_level"),
            "状态": item.get("status"),
        }
        for item in (admin_agent_tools.admin_list_approvals(db, admin, status="pending").data or [])
    ]
    return _table("待审批事项", ["ID", "事项", "动作", "风险", "状态"], rows, len(rows))


def _system_table(db: Session, admin: User) -> dict[str, Any]:
    status = admin_agent_tools.admin_system_status(db, admin).data or {}
    rows = [
        {"指标": "CPU", "当前值": _percent(status.get("cpu_percent"))},
        {"指标": "内存", "当前值": _percent(status.get("memory_percent"))},
        {"指标": "磁盘", "当前值": _percent(status.get("disk_percent"))},
        {"指标": "进程运行时长", "当前值": f"{int(status.get('process_uptime_seconds') or 0)} 秒"},
    ]
    return _table("服务器状态", ["指标", "当前值"], rows, len(rows))


def _percent(value: Any) -> str:
    return "暂无数据" if value is None else f"{float(value):.1f}%"


def _table(title: str, columns: list[str], rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {
        "type": "table",
        "title": title,
        "columns": columns,
        "rows": rows,
        "total": total,
        "collapsed": len(rows) > 10,
    }


def _text(content: str, *, status: str = "completed") -> dict[str, Any]:
    return {"type": "text", "content": content, "status": status}


def _execution_receipt(tool_call_id: int, title: str, *, duplicate: bool = False) -> dict[str, Any]:
    prefix = "该确认已经执行过" if duplicate else "已确认并执行"
    return _text(
        f"{prefix}（操作记录 #{tool_call_id}）：{title}。结果已写入系统审计。"
        "可在系统操作审计中追溯；角色和 Agent 状态可再次调整，删除账号保留历史记录但需管理员另行恢复。",
        status="confirmed",
    )


def _ops_execution_receipt(data: dict[str, Any]) -> dict[str, Any]:
    duplicate = bool(data.get("duplicate"))
    prefix = "该确认已执行过" if duplicate else "运维 Agent 已执行"
    status = str(data.get("status") or "failed")
    action = str(data.get("action") or "unknown")
    execution_id = data.get("id")
    duration = int(data.get("duration_ms") or 0)
    if status != "success":
        return _text(
            f"{prefix}（运维记录 #{execution_id}）：{action} 失败，"
            f"耗时 {duration} ms。原因：{data.get('error') or '执行器返回失败'}。",
            status="failed",
        )
    summary = ""
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    if action == "status":
        checks = (
            result.get("result", {}).get("checks")
            if isinstance(result.get("result"), dict)
            else result.get("checks")
        )
        if isinstance(checks, dict):
            summary = f"系统状态={checks.get('status', '未知')}"
    elif action == "backup_database":
        output = result.get("result", {}).get("stdout") if isinstance(result.get("result"), dict) else ""
        if output:
            summary = str(output).strip().splitlines()[-1][:160]
    return _text(
        f"{prefix}（运维记录 #{execution_id}）：{action} 成功，耗时 {duration} ms。"
        f"{('结果：' + summary + '。') if summary else ''}执行参数、返回值和时间已写入系统审计。",
        status="confirmed",
    )
