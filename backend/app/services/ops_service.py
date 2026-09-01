"""OperationsAgent 到宿主机白名单执行器的受控调用与审计。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.event_bus import emit_event
from app.agents.events import AgentEventType
from app.core.config import settings
from app.core.exceptions import ConflictError
from app.models.admin_chat import OpsExecution
from app.models.agent_governance import ToolCallLog
from app.models.user import User
from app.services import audit_service, rbac_service

ACTION_RISKS = {
    "status": "low",
    "certificate_status": "low",
    "backup_database": "medium",
    "verify_backup": "medium",
    "restart_service": "high",
    "nginx_reload": "high",
    "renew_certificate": "high",
    "database_maintenance": "high",
    "update_config": "high",
    "rollback_application": "critical",
    "restore_database": "critical",
    "cleanup": "critical",
    "host_inventory": "low",
    "list_directory": "low",
    "read_text_file": "low",
    "journal_query": "low",
    "systemd_unit_action": "critical",
    "docker_container_action": "critical",
    "write_text_file": "critical",
    "package_action": "critical",
    "firewall_action": "critical",
    "account_action": "critical",
    "ssh_authorized_key_action": "critical",
    "ssh_login_events": "low",
    "flytrap_attack_events": "low",
    "nginx_attack_events": "low",
    "backup_audit": "low",
    "db_threat_signals": "low",
    "db_health": "low",
    "ip_attribution": "low",
}
# 无交互系统身份只服务于固定健康巡检。其他只读动作同样可能泄露
# 目录、日志或主机拓扑，必须由唯一超级管理员在交互会话中发起。
AUTO_ACTIONS = frozenset({"status", "certificate_status"})
READ_ONLY_ACTIONS = frozenset({
    "status", "certificate_status", "host_inventory", "list_directory", "read_text_file", "journal_query",
    "ssh_login_events", "flytrap_attack_events", "nginx_attack_events", "backup_audit", "db_threat_signals",
    "db_health", "ip_attribution",
})
# 无交互安全监控调度可自动执行的只读安全动作；交互调用仍要求唯一超级管理员。
SCHEDULER_READ_ACTIONS = frozenset({
    "ssh_login_events", "flytrap_attack_events", "nginx_attack_events", "backup_audit", "db_threat_signals",
    "db_health", "ip_attribution",
})
ACTION_PARAM_KEYS = {
    "status": set(),
    "certificate_status": set(),
    "backup_database": set(),
    "verify_backup": {"file"},
    "restart_service": {"service"},
    "nginx_reload": set(),
    "renew_certificate": set(),
    "database_maintenance": set(),
    "update_config": {"key", "value"},
    "rollback_application": {"target"},
    "restore_database": {"file"},
    "cleanup": set(),
    "host_inventory": set(),
    "list_directory": {"path", "limit"},
    "read_text_file": {"path", "max_bytes"},
    "journal_query": {"unit", "since", "lines"},
    "systemd_unit_action": {"unit", "operation"},
    "docker_container_action": {"container", "operation"},
    "write_text_file": {"path", "content", "expected_sha256", "mode"},
    "package_action": {"operation", "packages"},
    "firewall_action": {"operation", "target_type", "value", "zone"},
    "account_action": {"operation", "username", "shell", "remove_home"},
    "ssh_authorized_key_action": {"operation", "username", "public_key", "fingerprint"},
    "ssh_login_events": {"since_hours", "limit", "focus"},
    "flytrap_attack_events": {"since_hours", "limit"},
    "nginx_attack_events": {"since_hours", "limit"},
    "backup_audit": set(),
    "db_threat_signals": {"since_hours", "limit"},
    "db_health": set(),
    "ip_attribution": {"ip"},
}
ACTION_REQUIRED_PARAMS = {
    "restart_service": {"service"},
    "update_config": {"key", "value"},
    "restore_database": {"file"},
    "list_directory": {"path"},
    "read_text_file": {"path"},
    "journal_query": {"unit"},
    "systemd_unit_action": {"operation"},
    "docker_container_action": {"container", "operation"},
    "write_text_file": {"path", "content"},
    "package_action": {"operation", "packages"},
    "firewall_action": {"operation", "target_type", "value"},
    "account_action": {"operation", "username"},
    "ssh_authorized_key_action": {"operation", "username"},
    "ip_attribution": {"ip"},
}
ACTION_PARAM_TYPES = {
    "file": str,
    "service": str,
    "key": str,
    "value": str,
    "target": str,
    "path": str,
    "limit": int,
    "max_bytes": int,
    "unit": str,
    "since": str,
    "lines": int,
    "operation": str,
    "container": str,
    "content": str,
    "expected_sha256": str,
    "mode": str,
    "packages": list,
    "target_type": str,
    "zone": str,
    "username": str,
    "shell": str,
    "remove_home": bool,
    "public_key": str,
    "fingerprint": str,
    "since_hours": int,
    "focus": str,
    "ip": str,
}


def _object_schema(properties: dict[str, Any], required: set[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required or set()),
        "additionalProperties": False,
    }


_STRING = {"type": "string", "maxLength": 262_144}
ACTION_PARAM_SCHEMAS = {
    "status": _object_schema({}),
    "certificate_status": _object_schema({}),
    "backup_database": _object_schema({}),
    "verify_backup": _object_schema({"file": {"type": "string", "maxLength": 200}}),
    "restart_service": _object_schema({"service": {"type": "string", "enum": ["backend", "frontend", "mysql", "redis", "clamav"]}}, {"service"}),  # noqa: E501
    "nginx_reload": _object_schema({}),
    "renew_certificate": _object_schema({}),
    "database_maintenance": _object_schema({}),
    "update_config": _object_schema({"key": {"type": "string", "maxLength": 80}, "value": {"type": "string", "maxLength": 80}}, {"key", "value"}),  # noqa: E501
    "rollback_application": _object_schema({"target": {"type": "string", "enum": ["all", "backend", "frontend"]}}),
    "restore_database": _object_schema({"file": {"type": "string", "maxLength": 200}}, {"file"}),
    "cleanup": _object_schema({}),
    "host_inventory": _object_schema({}),
    "list_directory": _object_schema({"path": {"type": "string", "maxLength": 4096}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, {"path"}),  # noqa: E501
    "read_text_file": _object_schema({"path": {"type": "string", "maxLength": 4096}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 262_144}}, {"path"}),  # noqa: E501
    "journal_query": _object_schema({"unit": {"type": "string", "maxLength": 128}, "since": {"type": "string", "maxLength": 64}, "lines": {"type": "integer", "minimum": 1, "maximum": 500}}, {"unit"}),  # noqa: E501
    "systemd_unit_action": _object_schema({"unit": {"type": "string", "maxLength": 128}, "operation": {"type": "string", "enum": ["start", "stop", "restart", "reload", "enable", "disable", "daemon_reload"]}}, {"operation"}),  # noqa: E501
    "docker_container_action": _object_schema({"container": {"type": "string", "maxLength": 128}, "operation": {"type": "string", "enum": ["start", "stop", "restart", "pause", "unpause"]}}, {"container", "operation"}),  # noqa: E501
    "write_text_file": _object_schema({"path": {"type": "string", "maxLength": 4096}, "content": _STRING, "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "mode": {"type": "string", "pattern": "^0?[0-7]{3}$"}}, {"path", "content"}),  # noqa: E501
    "package_action": _object_schema({"operation": {"type": "string", "enum": ["install", "upgrade", "remove"]}, "packages": {"type": "array", "items": {"type": "string", "maxLength": 128}, "minItems": 1, "maxItems": 20, "uniqueItems": True}}, {"operation", "packages"}),  # noqa: E501
    "firewall_action": _object_schema({"operation": {"type": "string", "enum": ["add", "remove"]}, "target_type": {"type": "string", "enum": ["port", "service"]}, "value": {"type": "string", "maxLength": 64}, "zone": {"type": "string", "maxLength": 32}}, {"operation", "target_type", "value"}),  # noqa: E501
    "account_action": _object_schema({"operation": {"type": "string", "enum": ["create_system", "lock", "unlock", "delete"]}, "username": {"type": "string", "maxLength": 32}, "shell": {"type": "string", "enum": ["/sbin/nologin", "/usr/sbin/nologin", "/bin/bash"]}, "remove_home": {"type": "boolean"}}, {"operation", "username"}),  # noqa: E501
    "ssh_authorized_key_action": _object_schema({"operation": {"type": "string", "enum": ["add", "remove"]}, "username": {"type": "string", "maxLength": 32}, "public_key": {"type": "string", "maxLength": 16_384}, "fingerprint": {"type": "string", "maxLength": 80}}, {"operation", "username"}),  # noqa: E501
    "ssh_login_events": _object_schema({"since_hours": {"type": "integer", "minimum": 1, "maximum": 720}, "limit": {"type": "integer", "minimum": 1, "maximum": 5000}, "focus": {"type": "string", "enum": ["all", "accepted", "failed"]}}),  # noqa: E501
    "flytrap_attack_events": _object_schema({"since_hours": {"type": "integer", "minimum": 1, "maximum": 720}, "limit": {"type": "integer", "minimum": 1, "maximum": 5000}}),  # noqa: E501
    "nginx_attack_events": _object_schema({"since_hours": {"type": "integer", "minimum": 1, "maximum": 720}, "limit": {"type": "integer", "minimum": 1, "maximum": 5000}}),  # noqa: E501
    "backup_audit": _object_schema({}),
    "db_threat_signals": _object_schema({"since_hours": {"type": "integer", "minimum": 1, "maximum": 720}, "limit": {"type": "integer", "minimum": 1, "maximum": 20000}}),  # noqa: E501
    "db_health": _object_schema({}),
    "ip_attribution": _object_schema({"ip": {"type": "string", "maxLength": 64}}, {"ip"}),  # noqa: E501
}

# 每个运维动作给模型看的一句话说明(含常见意图示例),帮助小菱把用户意图
# 正确映射到白名单动作。只读动作用于实时获取服务器信息,写动作会等待审批。
ACTION_DESCRIPTIONS = {
    "status": "查看服务器整体实时状态(CPU/内存/磁盘/负载/运行时长)",
    "certificate_status": "查看 HTTPS 证书有效期与状态",
    "backup_database": "对生产数据库执行一次完整备份",
    "verify_backup": "校验指定备份文件是否完整可恢复(参数 file=备份文件名)",
    "restart_service": "重启平台服务(service 可选 backend/frontend/mysql/redis/clamav)",
    "nginx_reload": "平滑重载 Nginx 配置,不断开现有连接",
    "renew_certificate": "重新签发并部署 HTTPS 证书",
    "database_maintenance": "执行数据库维护(优化表/清理)",
    "update_config": "更新平台运行配置项(key=配置键,value=配置值)",
    "rollback_application": "回滚应用发布(target=all/backend/frontend)",
    "restore_database": "从指定备份文件恢复数据库(file=备份文件名)",
    "cleanup": "清理过期构建产物、临时文件与旧备份",
    "host_inventory": "获取宿主机资产清单(CPU/内存/磁盘/网络/系统信息),用于实时了解服务器配置",
    "list_directory": "列出服务器指定目录内容(path=绝对路径,可选 limit)",
    "read_text_file": "读取服务器指定文本文件内容(path=绝对路径,可选 max_bytes)",
    "journal_query": "查询 systemd 服务日志(unit=服务名,可选 since/lines),用于实时查看运行日志",
    "systemd_unit_action": "对 systemd 单元执行 start/stop/restart/reload/enable/disable(unit=单元名)",
    "docker_container_action": "对 Docker 容器执行 start/stop/restart/pause/unpause(container=容器名)",
    "write_text_file": "写入或覆盖服务器文本文件(path=绝对路径,content=内容,可选 mode/expected_sha256)",
    "package_action": "安装/升级/移除系统软件包(operation=install/upgrade/remove,packages=包名列表)",
    "firewall_action": (
        "开放或关闭防火墙端口/服务(operation=add/remove,target_type=port/service,"
        "value=端口号或服务名,可选 zone)。用户说开放某端口时使用 add+port+端口号,关闭时用 remove"
    ),
    "account_action": "管理系统账号(operation=create_system/lock/unlock/delete,username=用户名)",
    "ssh_authorized_key_action": "添加或移除用户 SSH 授权公钥(operation=add/remove,username,public_key)",
    "ssh_login_events": "查询 SSH 登录事件(since_hours/limit/focus=all/accepted/failed)",
    "flytrap_attack_events": "查询蜜罐攻击事件",
    "nginx_attack_events": "查询 Nginx 攻击事件",
    "backup_audit": "查询备份审计记录",
    "db_threat_signals": "查询数据库内部威胁信号",
    "db_health": "查询数据库健康状态",
    "ip_attribution": "查询 IP 归属地信息(ip=IP 地址)",
}


class OpsExecutorError(RuntimeError):
    """宿主机执行器返回的结构化失败。"""

    def __init__(self, message: str, *, status_code: int, payload: dict[str, Any]):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    @property
    def outcome_known(self) -> bool:
        """除冲突/服务端异常外，4xx 表示执行器已确认动作未成功。"""
        return 400 <= self.status_code < 500 and self.status_code != 409


def _request_params_json(action: str, params: dict[str, Any]) -> str:
    return json.dumps(_audit_params(action, params), ensure_ascii=False, sort_keys=True, default=str)


def _assert_existing_request_matches(row: OpsExecution, action: str, params: dict[str, Any]) -> None:
    try:
        recorded_params = json.loads(row.params_json or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConflictError("request_id 对应的运维审计已损坏，禁止重新绑定") from exc
    if row.action != action or recorded_params != _audit_params(action, params):
        raise ConflictError("request_id 已绑定其他运维请求，禁止重新使用")


def _uncertain_execution(row: OpsExecution, error: str, *, duplicate: bool) -> dict[str, Any]:
    result = _execution_dict(row, duplicate=duplicate)
    result.update({
        "error": error[:4000],
        "retryable": False,
        "next_action": "动作结果尚未确认；请使用同一请求编号重试查询，禁止新建编号盲目重放",
    })
    return result


def _finalize_execution(
    db: Session,
    row: OpsExecution,
    actor: Optional[User],
    *,
    source: str,
    result: dict[str, Any],
    duration_ms: int,
    duplicate: bool,
) -> dict[str, Any]:
    row.status = "success" if result.get("ok") else "failed"
    row.result_json = json.dumps(_redact_value(result), ensure_ascii=False, default=str)[:200_000]
    row.error = None if result.get("ok") else str(result.get("error") or "执行器返回失败")[:4000]
    row.duration_ms = max(0, int(duration_ms))
    row.finished_at = datetime.now(timezone.utc)

    existing_call = db.query(ToolCallLog).filter(ToolCallLog.copilot_request_id == row.request_id).first()
    if existing_call is None:
        db.add(ToolCallLog(
            agent_code="operations",
            tool_code=f"ops.{row.action}",
            action=f"operations.{row.action}",
            resource="production",
            status=row.status,
            risk_level=row.risk_level,
            decision="automatic" if actor is None or row.action in AUTO_ACTIONS else "confirmed",
            input_summary=f"source={source}; params={row.params_json[:1000]}",
            output_summary=json.dumps(_redact_value(result), ensure_ascii=False, default=str)[:4000],
            error=row.error,
            duration_ms=row.duration_ms,
            copilot_request_id=row.request_id,
        ))
        audit_service.log(
            db,
            actor,
            f"admin_copilot.ops.{row.action}",
            target_type="production_ops",
            target_id=row.request_id,
            detail=f"运维动作 {row.action}：{row.status}；source={source}",
            status=row.status,
            commit=False,
        )
    db.commit()
    db.refresh(row)
    return _execution_dict(row, duplicate=duplicate)


def execute(
    db: Session,
    actor: Optional[User],
    *,
    action: str,
    params: Optional[dict[str, Any]] = None,
    request_id: str = "",
    session_db_id: Optional[int] = None,
    source: str = "admin_copilot",
) -> dict[str, Any]:
    if action not in ACTION_RISKS:
        raise ValueError(f"不支持的运维动作: {action}")
    safe_params = validate_action_params(action, params or {})
    if actor is not None and not rbac_service.is_super_admin_user(db, actor.id):
        raise PermissionError("仅超级管理员 admin 可执行运维动作")
    if actor is None and action not in AUTO_ACTIONS and not (
        source == "scheduler" and action in SCHEDULER_READ_ACTIONS
    ) and not (
        # 自动备份：仅当最高管理员显式开启 backup_schedule_enabled 时，
        # 无交互调度身份才允许触发 backup_database（中等风险写动作）。
        source == "scheduler" and action == "backup_database" and settings.backup_schedule_enabled
    ):
        raise PermissionError("无交互调度只允许运维只读动作")
    request_id = request_id or uuid.uuid4().hex
    existing = db.query(OpsExecution).filter(OpsExecution.request_id == request_id).first()
    if existing:
        _assert_existing_request_matches(existing, action, safe_params)
        if existing.status != "running":
            return _execution_dict(existing, duplicate=True)

        # API 进程可能在宿主机动作完成后、数据库回执落盘前中断。同一
        # request_id 重送给执行器只会读取幂等账本，不会重复副作用。
        try:
            recovered = _call_executor(action, safe_params, request_id)
        except OpsExecutorError as exc:
            if not exc.outcome_known:
                return _uncertain_execution(existing, str(exc), duplicate=True)
            recovered = {"ok": False, "action": action, "error": str(exc), "executor": exc.payload}
        except Exception as exc:  # noqa: BLE001 - 网络结果未知时保持 running，禁止盲目重放
            return _uncertain_execution(existing, str(exc), duplicate=True)

        # 并发恢复者在这里串行确认，避免重复写 ToolCallLog/AuditLog。
        locked = (
            db.query(OpsExecution)
            .filter(OpsExecution.request_id == request_id)
            .with_for_update()
            .first()
        )
        if locked is None:
            raise RuntimeError("运维执行记录在恢复期间消失")
        if locked.status != "running":
            return _execution_dict(locked, duplicate=True)
        reconciled = _finalize_execution(
            db,
            locked,
            actor,
            source=source,
            result=recovered,
            duration_ms=locked.duration_ms or 0,
            duplicate=True,
        )
        _emit(
            AgentEventType.COMPLETE if locked.status == "success" else AgentEventType.FAILED,
            f"trc_ops_{request_id[:16]}",
            actor,
            action,
            "运维动作回执已恢复" if locked.status == "success" else "运维动作已确认失败",
        )
        return reconciled

    risk = ACTION_RISKS[action]
    started = datetime.now(timezone.utc)
    row = OpsExecution(
        request_id=request_id,
        session_id=session_db_id,
        actor_id=actor.id if actor else None,
        action=action,
        risk_level=risk,
        status="running",
        params_json=_request_params_json(action, safe_params),
        started_at=started,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(OpsExecution).filter(OpsExecution.request_id == request_id).first()
        if existing:
            # 与常规幂等分支走同一套参数绑定和未知结果对账，
            # 不把并发插入冲突误报成“仍在执行”。
            return execute(
                db,
                actor,
                action=action,
                params=safe_params,
                request_id=request_id,
                session_db_id=session_db_id,
                source=source,
            )
        raise
    db.refresh(row)

    trace_id = f"trc_ops_{request_id[:16]}"
    _emit(AgentEventType.DISPATCH, trace_id, actor, action, "运维 Agent 已接收任务")
    _emit(AgentEventType.PROGRESS, trace_id, actor, action, "正在执行白名单运维动作")
    t0 = time.monotonic()
    try:
        result = _call_executor(action, safe_params, request_id)
    except OpsExecutorError as exc:
        if exc.outcome_known:
            result = {"ok": False, "action": action, "error": str(exc), "executor": exc.payload}
        else:
            row.error = str(exc)[:4000]
            row.result_json = json.dumps(_redact_value(exc.payload), ensure_ascii=False, default=str)[:200_000]
            row.duration_ms = int((time.monotonic() - t0) * 1000)
            db.commit()
            return _uncertain_execution(row, str(exc), duplicate=False)
    except Exception as exc:  # noqa: BLE001 - 必须落盘失败证据
        row.error = str(exc)[:4000]
        row.result_json = json.dumps(
            _redact_value({"ok": False, "action": action, "error": str(exc)}),
            ensure_ascii=False,
            default=str,
        )
        row.duration_ms = int((time.monotonic() - t0) * 1000)
        db.commit()
        return _uncertain_execution(row, str(exc), duplicate=False)

    execution = _finalize_execution(
        db,
        row,
        actor,
        source=source,
        result=result,
        duration_ms=int((time.monotonic() - t0) * 1000),
        duplicate=False,
    )
    _emit(
        AgentEventType.COMPLETE if row.status == "success" else AgentEventType.FAILED,
        trace_id,
        actor,
        action,
        "运维动作完成" if row.status == "success" else "运维动作失败",
    )
    return execution


def validate_action_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """后端动作契约；执行器会再次校验，拒绝模型传入的额外字段。"""
    if not isinstance(params, dict):
        raise ValueError("运维 params 必须是对象")
    allowed = ACTION_PARAM_KEYS.get(action)
    if allowed is None:
        raise ValueError(f"不支持的运维动作: {action}")
    extra = set(params) - allowed
    if extra:
        raise ValueError(f"动作 {action} 包含未允许参数: {sorted(extra)}")
    missing = ACTION_REQUIRED_PARAMS.get(action, set()) - set(params)
    if missing:
        raise ValueError(f"动作 {action} 缺少必填参数: {sorted(missing)}")
    for key, value in params.items():
        expected = ACTION_PARAM_TYPES[key]
        if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"参数 {key} 必须是整数")
        if expected is not int and not isinstance(value, expected):
            raise ValueError(f"参数 {key} 类型不正确")
    operation = str(params.get("operation") or "")
    if action == "systemd_unit_action" and operation != "daemon_reload" and not params.get("unit"):
        raise ValueError("动作 systemd_unit_action 缺少必填参数: ['unit']")
    if action == "ssh_authorized_key_action":
        required_key = "public_key" if operation == "add" else "fingerprint"
        if not params.get(required_key):
            raise ValueError(f"动作 ssh_authorized_key_action 缺少必填参数: ['{required_key}']")
    return dict(params)


def audit_action_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(params)
    if action == "write_text_file" and "content" in sanitized:
        content = str(sanitized.pop("content"))
        sanitized["content_bytes"] = len(content.encode("utf-8"))
        sanitized["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if action == "ssh_authorized_key_action" and "public_key" in sanitized:
        public_key = str(sanitized.pop("public_key"))
        sanitized["public_key_fingerprint"] = hashlib.sha256(public_key.encode("utf-8")).hexdigest()
    return sanitized


def _audit_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    return audit_action_params(action, params)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        import re

        redacted = re.sub(
            r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
            value,
        )
        return re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{8,}", r"\1[REDACTED]", redacted)
    return value


def _call_executor(action: str, params: dict[str, Any], request_id: str) -> dict[str, Any]:
    if not settings.ops_executor_token:
        raise RuntimeError("运维执行器令牌未配置")
    transport = httpx.HTTPTransport(uds=settings.ops_executor_socket)
    with httpx.Client(transport=transport, base_url="http://prism-ops", timeout=900, trust_env=False) as client:
        response = client.post(
            "/execute",
            headers={"Authorization": f"Bearer {settings.ops_executor_token}"},
            json={"action": action, "params": params, "request_id": request_id},
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"运维执行器返回非 JSON（HTTP {response.status_code}）") from exc
    if response.status_code >= 400:
        raise OpsExecutorError(
            str(payload.get("error") or f"运维执行器 HTTP {response.status_code}"),
            status_code=response.status_code,
            payload=payload if isinstance(payload, dict) else {},
        )
    return payload


def _emit(type_: AgentEventType, trace_id: str, actor: Optional[User], action: str, message: str) -> None:
    emit_event(
        type_,
        agent="operations",
        trace_id=trace_id,
        parent="manager",
        message=message,
        payload={"action": action},
        user_id=actor.id if actor else None,
    )


def _execution_dict(row: OpsExecution, *, duplicate: bool) -> dict[str, Any]:
    try:
        result = json.loads(row.result_json or "{}")
    except (TypeError, json.JSONDecodeError):
        result = {}
    payload = {
        "id": row.id,
        "request_id": row.request_id,
        "action": row.action,
        "risk_level": row.risk_level,
        "status": row.status,
        "result": result,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "duplicate": duplicate,
    }
    if row.status == "running":
        payload.update({
            "retryable": False,
            "next_action": "动作结果尚未确认；请使用同一请求编号查询，禁止新建编号盲目重放",
        })
    return payload
