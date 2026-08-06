"""MCP 服务器、工具、Agent 绑定与能力别名治理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.contracts import CONTRACTS
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.agent_capability import (
    AgentCapabilityAlias,
    AgentMcpBinding,
    McpServer,
    McpTool,
)
from app.models.user import User
from app.services import audit_service
from app.services.managed_mcp_adapter import is_live_managed_kind, managed_kind_ready
from app.services.mcp_tool_provider import McpServerConfig, McpToolProvider, validate_mcp_url
from app.utils.api_resolver import decrypt_api_key_with_metadata, encrypt_api_key

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_-]+")
_ALIAS_SEPARATORS = re.compile(r"[^0-9a-z\u3400-\u9fff]+")
_RESERVED_HEADERS = {
    "accept",
    "connection",
    "content-length",
    "content-type",
    "host",
    "mcp-session-id",
    "transfer-encoding",
}

# 受管工具的 Schema 同时是本地 adapter 的登记契约。只有已接入本地
# executor 的种类才会被标为健康并提供给当前请求用户；其余种类保持不可用。
_MANAGED_TOOLS: dict[str, tuple[dict[str, Any], ...]] = {
    "prism-code": (
        {
            "name": "list_project_source",
            "description": "列出当前用户有权项目的源码文件元数据",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer"},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "page_size": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                },
                "required": ["project_id"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "download_project_source",
            "description": "生成当前用户有权项目的完整源码 ZIP",
            "inputSchema": {
                "type": "object",
                "properties": {"project_id": {"type": "integer"}},
                "required": ["project_id"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True},
        },
    ),
    "prism-sandbox": (
        {
            "name": "create_test",
            "description": "创建白盒、黑盒或组合测试沙箱",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer"},
                    "language": {
                        "type": "string",
                        "enum": ["python", "node", "java", "go", "php"],
                    },
                    "test_mode": {
                        "type": "string",
                        "enum": ["whitebox", "blackbox", "combined"],
                    },
                },
                "required": ["project_id", "language", "test_mode"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False},
        },
        {
            "name": "create_deployment",
            "description": "创建持续部署沙箱",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer"},
                    "language": {
                        "type": "string",
                        "enum": ["python", "node", "java", "go", "php"],
                    },
                    "ttl_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                },
                "required": ["project_id", "language"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False},
        },
        {
            "name": "close",
            "description": "关闭当前用户有权访问的沙箱",
            "inputSchema": {
                "type": "object",
                "properties": {"public_id": {"type": "string"}},
                "required": ["public_id"],
                "additionalProperties": False,
            },
            "annotations": {"destructiveHint": True},
        },
        {
            "name": "extend",
            "description": "为当前用户创建且仍在运行的沙箱续期",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "public_id": {"type": "string"},
                    "hours": {"type": "integer", "minimum": 1, "maximum": 168},
                },
                "required": ["public_id", "hours"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False},
        },
    ),
    "playwright": (
        {
            "name": "browser_blackbox",
            "description": "在隔离 worker 内对已授权目标执行浏览器黑盒测试",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sandbox_id": {"type": "string"},
                    "target_url": {"type": "string"},
                },
                "required": ["sandbox_id", "target_url"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False},
        },
    ),
}

_RECOMMENDED_SERVERS: tuple[dict[str, Any], ...] = (
    {
        "code": "prism-code",
        "name": "Prism 源码能力",
        "description": "项目源码、审查和下载的权限内能力",
        "transport": "managed",
        "managed_kind": "prism-code",
        "status": "healthy",
        "enabled": True,
    },
    {
        "code": "prism-sandbox",
        "name": "Prism 沙箱能力",
        "description": "白盒、黑盒测试与隔离部署能力",
        "transport": "managed",
        "managed_kind": "prism-sandbox",
        "status": "healthy",
        "enabled": True,
    },
    {
        "code": "playwright",
        "name": "Playwright 黑盒测试",
        "description": "Playwright MCP 只允许在隔离 worker 中使用",
        "transport": "managed",
        "managed_kind": "playwright",
        "credential_required": False,
        "status": "registered",
    },
    {
        "code": "github",
        "name": "GitHub 官方 MCP",
        "description": "GitHub 托管 MCP；配置 OAuth/PAT 后才能健康检查",
        "transport": "streamable_http",
        "url": "https://api.githubcopilot.com/mcp/",
        "auth_type": "oauth_required",
        "credential_required": True,
        "status": "credential_required",
    },
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(schema).encode("utf-8")).hexdigest()


def _normalize_alias(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _ALIAS_SEPARATORS.sub("", normalized)


def _model_name(server_code: str, tool_name: str) -> str:
    server = _SAFE_COMPONENT.sub("_", server_code).strip("_-").lower()[:18] or "server"
    tool = _SAFE_COMPONENT.sub("_", tool_name).strip("_-").lower()[:29] or "tool"
    digest = hashlib.sha256(f"{server_code}\0{tool_name}".encode()).hexdigest()[:8]
    return f"mcp_{server}_{tool}_{digest}"[:64]


def _validate_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in headers.items():
        key = str(raw_key).strip()
        value = str(raw_value)
        if not key:
            continue
        if key.casefold() in _RESERVED_HEADERS:
            raise ValidationError(f"MCP header {key} 由系统管理，不允许覆盖", code=40001)
        result[key] = value
    return result


def _encrypted_headers(headers: Mapping[str, str]) -> str | None:
    normalized = _validate_headers(headers)
    return encrypt_api_key(_json(normalized)) if normalized else None


def decrypt_server_headers(row: McpServer, db: Session | None = None) -> dict[str, str]:
    """解密服务器请求头；任何异常都失败关闭。"""

    if not row.encrypted_headers:
        return {}
    decryption = decrypt_api_key_with_metadata(row.encrypted_headers)
    if decryption is None:
        raise RuntimeError("MCP 凭据无法解密，服务已失败关闭")
    try:
        value = json.loads(decryption.plaintext)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MCP 凭据格式不合法") from exc
    if not isinstance(value, dict):
        raise RuntimeError("MCP 凭据格式不合法")
    headers = _validate_headers({str(key): str(item) for key, item in value.items()})
    if decryption.needs_rotation:
        if db is None:
            raise RuntimeError("MCP 凭据密钥已过期，无法安全持久化轮换")
        row.encrypted_headers = encrypt_api_key(_json(headers))
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning(f"[mcp] 凭据密钥轮换失败(server_id={row.id})")
            raise RuntimeError("MCP 凭据密钥轮换失败，服务已失败关闭") from exc
    return headers


def _safe_error(exc: Exception, server: McpServer) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    try:
        decryption = decrypt_api_key_with_metadata(server.encrypted_headers or "")
        value = json.loads(decryption.plaintext) if decryption else {}
        if isinstance(value, dict):
            for secret in value.values():
                if secret:
                    message = message.replace(str(secret), "[REDACTED]")
    except Exception:
        pass
    return (message or type(exc).__name__)[:1000]


def _server_dict(row: McpServer, *, tools: int = 0) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "transport": row.transport,
        "url": row.url or "",
        "auth_type": row.auth_type,
        "has_credentials": bool(row.encrypted_headers),
        "managed_kind": row.managed_kind,
        "status": row.status,
        "enabled": bool(row.enabled),
        "credential_required": bool(row.credential_required),
        "last_health_at": row.last_health_at,
        "last_error": row.last_error,
        "tool_count": tools,
    }


def list_servers(db: Session) -> list[dict[str, Any]]:
    rows = db.query(McpServer).order_by(McpServer.id.asc()).all()
    return [
        _server_dict(row, tools=db.query(McpTool).filter(McpTool.server_id == row.id).count())
        for row in rows
    ]


def get_server(db: Session, server_id: int) -> McpServer:
    row = db.get(McpServer, server_id)
    if not row:
        raise NotFoundError("MCP Server 不存在", code=40400)
    return row


def upsert_server(
    db: Session,
    actor: User,
    payload: dict[str, Any],
    server_id: int = 0,
) -> McpServer:
    row = get_server(db, server_id) if server_id else None
    if row is None:
        existing = db.query(McpServer).filter(McpServer.code == payload["code"]).first()
        if existing:
            raise ConflictError("MCP Server code 已存在", code=40901)
        row = McpServer(code=payload["code"], name=payload["name"])
        db.add(row)
    elif payload["code"] != row.code:
        raise ValidationError("MCP Server code 创建后不可修改", code=40001)

    transport = payload.get("transport", "streamable_http")
    managed_kind = payload.get("managed_kind")
    raw_url = str(payload.get("url") or "").strip()
    if transport == "managed":
        if managed_kind not in _MANAGED_TOOLS:
            raise ValidationError("受管 MCP 类型不受支持", code=40001)
        raw_url = ""
    else:
        if managed_kind:
            raise ValidationError("远程 MCP 不能设置 managed_kind", code=40001)
        if not raw_url:
            raise ValidationError("远程 MCP 必须提供 URL", code=40001)
        try:
            raw_url = validate_mcp_url(raw_url)
        except ValueError as exc:
            raise ValidationError(str(exc), code=40001) from exc

    previous_transport = row.transport
    if "headers" in payload and payload["headers"] is not None:
        row.encrypted_headers = _encrypted_headers(payload["headers"])
    row.code = payload["code"]
    row.name = payload["name"]
    row.description = payload.get("description") or ""
    row.transport = transport
    row.url = raw_url or None
    row.auth_type = payload.get("auth_type", "none")
    row.managed_kind = managed_kind
    row.credential_required = int(bool(payload.get("credential_required")))

    requested_enabled = bool(payload.get("enabled"))
    if transport == "managed":
        if is_live_managed_kind(managed_kind) and managed_kind_ready(db, str(managed_kind or "")):
            # Prism 内部执行器与当前 API 进程同源，不需要远程凭据；健康状态
            # 表示 adapter 已注册且可以把调用交给真实服务层。
            row.credential_required = 0
            row.enabled = int(requested_enabled)
            row.status = "healthy" if requested_enabled else "disabled"
        else:
            # Worker 画像未通过真实执行器自检时不能伪造可用。
            row.enabled = 0
            row.status = "credential_required" if row.credential_required else "registered"
    elif row.credential_required and not row.encrypted_headers:
        row.enabled = 0
        row.status = "credential_required"
    else:
        row.enabled = int(requested_enabled)
        row.status = "unknown" if requested_enabled else "disabled"
    row.last_error = None

    db.flush()
    if server_id and previous_transport != row.transport:
        tool_ids = [tool_id for (tool_id,) in db.query(McpTool.id).filter(McpTool.server_id == row.id)]
        if tool_ids:
            db.query(AgentMcpBinding).filter(AgentMcpBinding.tool_id.in_(tool_ids)).update(
                {AgentMcpBinding.enabled: 0},
                synchronize_session=False,
            )
            db.query(McpTool).filter(McpTool.id.in_(tool_ids)).update(
                {McpTool.enabled: 0},
                synchronize_session=False,
            )
    audit_service.log(
        db,
        actor,
        "mcp_server_upsert",
        target_type="mcp_server",
        target_id=row.id,
        detail=f"code={row.code}; transport={row.transport}; enabled={row.enabled}",
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return row


def delete_server(db: Session, actor: User, server_id: int) -> None:
    row = get_server(db, server_id)
    tool_ids = [tool_id for (tool_id,) in db.query(McpTool.id).filter(McpTool.server_id == row.id)]
    if tool_ids:
        db.query(AgentMcpBinding).filter(AgentMcpBinding.tool_id.in_(tool_ids)).delete(
            synchronize_session=False,
        )
        db.query(McpTool).filter(McpTool.id.in_(tool_ids)).delete(synchronize_session=False)
    db.delete(row)
    audit_service.log(
        db,
        actor,
        "mcp_server_delete",
        target_type="mcp_server",
        target_id=server_id,
        detail=f"code={row.code}",
        commit=False,
    )
    db.commit()


def _ensure_default_managed_bindings(db: Session, server: McpServer) -> None:
    """为两个实际 Responses surface 创建一次性默认绑定。

    已存在的绑定（包括因 Schema 漂移而被禁用的绑定）不被自动重启，仍需
    管理员重新审核；仅缺失的初始绑定会被补齐。
    """

    if (
        not server.enabled
        or server.status != "healthy"
        or not is_live_managed_kind(server.managed_kind)
        or not managed_kind_ready(db, str(server.managed_kind or ""))
        or server.managed_kind == "playwright"
    ):
        return
    tools = db.query(McpTool).filter(McpTool.server_id == server.id, McpTool.enabled == 1).all()
    for tool in tools:
        requires_approval = tool.tool_name not in {"list_project_source", "download_project_source"}
        for agent_code in ("chat_assistant", "manager"):
            exists = (
                db.query(AgentMcpBinding.id)
                .filter(
                    AgentMcpBinding.agent_code == agent_code,
                    AgentMcpBinding.tool_id == tool.id,
                )
                .first()
            )
            if exists:
                continue
            db.add(
                AgentMcpBinding(
                    agent_code=agent_code,
                    tool_id=tool.id,
                    permission="allow",
                    requires_approval=int(requires_approval),
                    bound_schema_sha256=tool.schema_sha256,
                    enabled=1,
                )
            )


def seed_recommended_servers(db: Session, actor: User) -> list[dict[str, Any]]:
    for item in _RECOMMENDED_SERVERS:
        activate_managed_defaults = False
        row = db.query(McpServer).filter(McpServer.code == item["code"]).first()
        if row is None:
            row = McpServer(
                code=item["code"],
                name=item["name"],
                description=item["description"],
                transport=item["transport"],
                url=item.get("url"),
                auth_type=item.get("auth_type", "none"),
                managed_kind=item.get("managed_kind"),
                enabled=int(bool(item.get("enabled", False))),
                credential_required=int(bool(item.get("credential_required"))),
                status=item["status"],
            )
            db.add(row)
            db.flush()
            activate_managed_defaults = bool(
                row.enabled
                and is_live_managed_kind(row.managed_kind)
                and managed_kind_ready(db, str(row.managed_kind or ""))
            )
        elif (
            row.transport == "managed"
            and row.managed_kind != "playwright"
            and is_live_managed_kind(row.managed_kind)
            and managed_kind_ready(db, str(row.managed_kind or ""))
        ):
            # 旧版本种子曾把真实 Prism executor 标成 registered/disabled；
            # 再次执行推荐注册时把它迁移到真实健康状态。
            if row.status in {"registered", "unavailable"}:
                row.enabled = 1
                row.status = "healthy"
                row.credential_required = 0
                row.last_error = None
                activate_managed_defaults = True
        if row.transport == "managed":
            _sync_discovered_tools(
                db,
                row,
                [dict(tool) for tool in _MANAGED_TOOLS.get(row.managed_kind or "", ())],
                enable_new=bool(
                    row.enabled
                    and is_live_managed_kind(row.managed_kind)
                    and managed_kind_ready(db, str(row.managed_kind or ""))
                ),
            )
            if activate_managed_defaults:
                db.query(McpTool).filter(McpTool.server_id == row.id).update(
                    {McpTool.enabled: 1},
                    synchronize_session="fetch",
                )
            _ensure_default_managed_bindings(db, row)
    audit_service.log(
        db,
        actor,
        "mcp_seed_recommended",
        target_type="mcp_server",
        detail="registered prism-code, prism-sandbox, playwright and github",
        commit=False,
    )
    db.commit()
    return list_servers(db)


async def _discover_remote(db: Session, server: McpServer) -> list[dict[str, Any]]:
    if server.credential_required and not server.encrypted_headers:
        raise RuntimeError("MCP 凭据未配置")
    headers = decrypt_server_headers(server, db)
    config = McpServerConfig(
        name=server.code,
        url=str(server.url or ""),
        headers=headers,
    )
    provider = McpToolProvider([config])
    return await provider.list_raw_tools(config)


def _run_remote_discovery(db: Session, server: McpServer) -> list[dict[str, Any]]:
    return asyncio.run(_discover_remote(db, server))


def _sync_discovered_tools(
    db: Session,
    server: McpServer,
    discovered: list[dict[str, Any]],
    *,
    enable_new: bool = False,
) -> None:
    seen: set[str] = set()
    for item in discovered:
        tool_name = str(item.get("name") or "").strip()
        if not tool_name or tool_name in seen:
            continue
        schema = item.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, Mapping):
            schema = {"type": "object", "properties": {}}
        annotations = item.get("annotations") if isinstance(item.get("annotations"), Mapping) else {}
        checksum = _checksum(schema)
        row = (
            db.query(McpTool)
            .filter(McpTool.server_id == server.id, McpTool.tool_name == tool_name)
            .first()
        )
        previous_checksum = row.schema_sha256 if row else ""
        if row is None:
            row = McpTool(
                server_id=server.id,
                tool_name=tool_name,
                model_name=_model_name(server.code, tool_name),
                display_name=str(item.get("title") or tool_name),
                input_schema_json="{}",
                schema_sha256=checksum,
                enabled=int(enable_new),
            )
            db.add(row)
            db.flush()
        row.display_name = str(item.get("title") or tool_name)
        row.description = str(item.get("description") or "")
        row.input_schema_json = _json(schema)
        row.annotations_json = _json(annotations)
        row.schema_sha256 = checksum
        row.risk_level = "low" if annotations.get("readOnlyHint") else "medium"
        if previous_checksum and previous_checksum != checksum:
            # Schema 变化后必须重新审核工具并重建绑定。
            row.enabled = 0
            db.query(AgentMcpBinding).filter(AgentMcpBinding.tool_id == row.id).update(
                {AgentMcpBinding.enabled: 0},
                synchronize_session=False,
            )
        seen.add(tool_name)

    for row in db.query(McpTool).filter(McpTool.server_id == server.id).all():
        if row.tool_name not in seen:
            row.enabled = 0
            db.query(AgentMcpBinding).filter(AgentMcpBinding.tool_id == row.id).update(
                {AgentMcpBinding.enabled: 0},
                synchronize_session=False,
            )


def _mark_remote_failure(db: Session, server_id: int, exc: Exception) -> McpServer:
    db.rollback()
    server = get_server(db, server_id)
    server.status = "unhealthy"
    server.last_health_at = datetime.now(timezone.utc)
    server.last_error = _safe_error(exc, server)
    db.commit()
    return server


def check_health(
    db: Session,
    server_id: int,
    actor: User | None = None,
) -> dict[str, Any]:
    server = get_server(db, server_id)
    now = datetime.now(timezone.utc)
    if server.transport == "managed":
        if (
            is_live_managed_kind(server.managed_kind)
            and managed_kind_ready(db, str(server.managed_kind or ""))
        ):
            server.status = "healthy" if server.enabled else "disabled"
            server.last_error = None
            server.last_health_at = now
            _ensure_default_managed_bindings(db, server)
            audit_service.log(
                db,
                actor,
                "mcp_health_check",
                target_type="mcp_server",
                target_id=server.id,
                detail=f"code={server.code}; status={server.status}",
                commit=False,
            )
            db.commit()
            return _server_dict(
                server,
                tools=db.query(McpTool).filter(McpTool.server_id == server.id).count(),
            )
        server.enabled = 0
        if server.credential_required and not server.encrypted_headers:
            server.status = "credential_required"
            server.last_error = "MCP 凭据未配置"
        else:
            server.status = "unavailable"
            server.last_error = "受管 MCP 执行器尚未接入"
        server.last_health_at = now
        audit_service.log(
            db,
            actor,
            "mcp_health_check",
            target_type="mcp_server",
            target_id=server.id,
            detail=f"code={server.code}; status={server.status}",
            status="failed",
            commit=False,
        )
        db.commit()
        return _server_dict(server, tools=db.query(McpTool).filter(McpTool.server_id == server.id).count())
    if server.credential_required and not server.encrypted_headers:
        server.enabled = 0
        server.status = "credential_required"
        server.last_error = "MCP 凭据未配置"
        server.last_health_at = now
        audit_service.log(
            db,
            actor,
            "mcp_health_check",
            target_type="mcp_server",
            target_id=server.id,
            detail=f"code={server.code}; status={server.status}",
            status="failed",
            commit=False,
        )
        db.commit()
        return _server_dict(server, tools=db.query(McpTool).filter(McpTool.server_id == server.id).count())
    try:
        _run_remote_discovery(db, server)
    except Exception as exc:
        failed = _mark_remote_failure(db, server_id, exc)
        audit_service.log(
            db,
            actor,
            "mcp_health_check",
            target_type="mcp_server",
            target_id=failed.id,
            detail=f"code={failed.code}; status={failed.status}",
            status="failed",
        )
        return _server_dict(failed, tools=db.query(McpTool).filter(McpTool.server_id == failed.id).count())
    server.status = "healthy"
    server.last_health_at = now
    server.last_error = None
    audit_service.log(
        db,
        actor,
        "mcp_health_check",
        target_type="mcp_server",
        target_id=server.id,
        detail=f"code={server.code}; status={server.status}",
        commit=False,
    )
    db.commit()
    return _server_dict(server, tools=db.query(McpTool).filter(McpTool.server_id == server.id).count())


def sync_tools(
    db: Session,
    server_id: int,
    actor: User | None = None,
) -> list[McpTool]:
    server = get_server(db, server_id)
    if server.transport == "managed":
        _sync_discovered_tools(
            db,
            server,
            [dict(tool) for tool in _MANAGED_TOOLS.get(server.managed_kind or "", ())],
            enable_new=bool(
                server.enabled
                and is_live_managed_kind(server.managed_kind)
                and managed_kind_ready(db, str(server.managed_kind or ""))
            ),
        )
        if (
            is_live_managed_kind(server.managed_kind)
            and managed_kind_ready(db, str(server.managed_kind or ""))
        ):
            server.status = "healthy" if server.enabled else "disabled"
            server.last_error = None
            _ensure_default_managed_bindings(db, server)
        else:
            server.enabled = 0
            server.status = "credential_required" if server.credential_required else "registered"
            server.last_error = (
                "MCP 凭据未配置"
                if server.credential_required and not server.encrypted_headers
                else "受管 MCP 执行器尚未接入"
            )
        tool_count = db.query(McpTool).filter(McpTool.server_id == server.id).count()
        audit_service.log(
            db,
            actor,
            "mcp_tool_sync",
            target_type="mcp_server",
            target_id=server.id,
            detail=f"code={server.code}; status={server.status}; tools={tool_count}",
            commit=False,
        )
        db.commit()
    else:
        try:
            discovered = _run_remote_discovery(db, server)
            _sync_discovered_tools(db, server, discovered, enable_new=False)
            server.status = "healthy"
            server.last_health_at = datetime.now(timezone.utc)
            server.last_error = None
            tool_count = db.query(McpTool).filter(McpTool.server_id == server.id).count()
            audit_service.log(
                db,
                actor,
                "mcp_tool_sync",
                target_type="mcp_server",
                target_id=server.id,
                detail=f"code={server.code}; status={server.status}; tools={tool_count}",
                commit=False,
            )
            db.commit()
        except Exception as exc:
            failed = _mark_remote_failure(db, server_id, exc)
            audit_service.log(
                db,
                actor,
                "mcp_tool_sync",
                target_type="mcp_server",
                target_id=failed.id,
                detail=f"code={failed.code}; status={failed.status}",
                status="failed",
            )
            raise ValidationError("MCP 工具同步失败，服务已标记为不健康", code=40001) from exc
    return (
        db.query(McpTool)
        .filter(McpTool.server_id == server.id)
        .order_by(McpTool.id.asc())
        .all()
    )


def _schema(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {"type": "object", "properties": {}}
    return parsed if isinstance(parsed, dict) else {"type": "object", "properties": {}}


def _tool_dict(tool: McpTool, server: McpServer) -> dict[str, Any]:
    return {
        "id": tool.id,
        "server_id": server.id,
        "server_code": server.code,
        "server_status": server.status,
        "tool_name": tool.tool_name,
        "model_name": tool.model_name,
        "display_name": tool.display_name,
        "description": tool.description or "",
        "input_schema": _schema(tool.input_schema_json),
        "schema_sha256": tool.schema_sha256,
        "risk_level": tool.risk_level,
        "enabled": bool(tool.enabled),
    }


def list_tools(db: Session, server_id: int = 0) -> list[dict[str, Any]]:
    query = db.query(McpTool, McpServer).join(McpServer, McpServer.id == McpTool.server_id)
    if server_id:
        query = query.filter(McpTool.server_id == server_id)
    rows = query.order_by(McpServer.code, McpTool.tool_name).all()
    return [_tool_dict(tool, server) for tool, server in rows]


def update_tool(
    db: Session,
    actor: User,
    tool_id: int,
    payload: dict[str, Any],
) -> McpTool:
    tool = db.get(McpTool, tool_id)
    if not tool:
        raise NotFoundError("MCP 工具不存在", code=40400)
    server = get_server(db, tool.server_id)
    if payload.get("enabled") is True and not (
        server.enabled
        and server.status == "healthy"
        and (
            server.transport == "streamable_http"
            or (
                server.transport == "managed"
                and is_live_managed_kind(server.managed_kind)
                and managed_kind_ready(db, str(server.managed_kind or ""))
            )
        )
    ):
        raise ValidationError("只能启用已启用且健康的 MCP 工具", code=40001)
    for field in ("display_name", "description", "risk_level"):
        if field in payload and payload[field] is not None:
            setattr(tool, field, payload[field])
    if payload.get("enabled") is not None:
        tool.enabled = int(bool(payload["enabled"]))
        if not tool.enabled:
            db.query(AgentMcpBinding).filter(AgentMcpBinding.tool_id == tool.id).update(
                {AgentMcpBinding.enabled: 0},
                synchronize_session=False,
            )
    audit_service.log(
        db,
        actor,
        "mcp_tool_update",
        target_type="mcp_tool",
        target_id=tool.id,
        detail=f"model_name={tool.model_name}; enabled={tool.enabled}; risk={tool.risk_level}",
        commit=False,
    )
    db.commit()
    db.refresh(tool)
    return tool


def upsert_binding(db: Session, actor: User, payload: dict[str, Any]) -> AgentMcpBinding:
    if payload["agent_code"] not in CONTRACTS:
        raise ValidationError("Agent 编码不存在", code=40001)
    tool = db.get(McpTool, payload["tool_id"])
    if not tool:
        raise NotFoundError("MCP 工具不存在", code=40400)
    server = get_server(db, tool.server_id)
    requested_enabled = bool(payload.get("enabled", True))
    if requested_enabled and payload.get("permission", "allow") != "deny":
        if not tool.enabled or not server.enabled or server.status != "healthy":
            raise ValidationError("只能绑定已启用且健康的 MCP 工具", code=40001)
    row = (
        db.query(AgentMcpBinding)
        .filter(
            AgentMcpBinding.agent_code == payload["agent_code"],
            AgentMcpBinding.tool_id == tool.id,
        )
        .first()
    )
    if row is None:
        row = AgentMcpBinding(
            agent_code=payload["agent_code"],
            tool_id=tool.id,
            bound_schema_sha256=tool.schema_sha256,
        )
        db.add(row)
    row.permission = payload.get("permission", "allow")
    row.requires_approval = int(
        server.managed_kind == "playwright" or bool(payload.get("requires_approval"))
    )
    row.bound_schema_sha256 = tool.schema_sha256
    row.enabled = int(requested_enabled and row.permission != "deny")
    db.flush()
    audit_service.log(
        db,
        actor,
        "mcp_binding_upsert",
        target_type="agent_mcp_binding",
        target_id=row.id,
        detail=f"agent={row.agent_code}; tool={tool.model_name}; permission={row.permission}",
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return row


def _binding_dict(
    binding: AgentMcpBinding,
    tool: McpTool,
    server: McpServer,
) -> dict[str, Any]:
    return {
        "id": binding.id,
        "agent_code": binding.agent_code,
        "tool_id": tool.id,
        "tool_code": tool.model_name,
        "server_code": server.code,
        "permission": binding.permission,
        "requires_approval": bool(binding.requires_approval),
        "schema_current": binding.bound_schema_sha256 == tool.schema_sha256,
        "enabled": bool(binding.enabled),
    }


def list_bindings(db: Session, agent_code: str = "") -> list[dict[str, Any]]:
    query = (
        db.query(AgentMcpBinding, McpTool, McpServer)
        .join(McpTool, McpTool.id == AgentMcpBinding.tool_id)
        .join(McpServer, McpServer.id == McpTool.server_id)
    )
    if agent_code:
        query = query.filter(AgentMcpBinding.agent_code == agent_code)
    rows = query.order_by(AgentMcpBinding.id.asc()).all()
    return [_binding_dict(binding, tool, server) for binding, tool, server in rows]


def delete_binding(db: Session, actor: User, binding_id: int) -> None:
    row = db.get(AgentMcpBinding, binding_id)
    if not row:
        raise NotFoundError("MCP 绑定不存在", code=40400)
    db.delete(row)
    audit_service.log(
        db,
        actor,
        "mcp_binding_delete",
        target_type="agent_mcp_binding",
        target_id=binding_id,
        detail=f"agent={row.agent_code}; tool_id={row.tool_id}",
        commit=False,
    )
    db.commit()


def _alias_dict(row: AgentCapabilityAlias) -> dict[str, Any]:
    return {
        "id": row.id,
        "capability_code": row.capability_code,
        "alias": row.alias,
        "locale": row.locale,
        "weight": float(row.weight),
        "enabled": bool(row.enabled),
    }


def list_aliases(db: Session, capability_code: str = "") -> list[dict[str, Any]]:
    query = db.query(AgentCapabilityAlias)
    if capability_code:
        query = query.filter(AgentCapabilityAlias.capability_code == capability_code)
    return [_alias_dict(row) for row in query.order_by(AgentCapabilityAlias.id.asc()).all()]


def upsert_alias(
    db: Session,
    actor: User,
    payload: dict[str, Any],
    alias_id: int = 0,
) -> AgentCapabilityAlias:
    normalized = _normalize_alias(payload["alias"])
    if not normalized:
        raise ValidationError("别名必须包含文字或数字", code=40001)
    row = db.get(AgentCapabilityAlias, alias_id) if alias_id else None
    if alias_id and row is None:
        raise NotFoundError("能力别名不存在", code=40400)
    duplicate = (
        db.query(AgentCapabilityAlias)
        .filter(
            AgentCapabilityAlias.capability_code == payload["capability_code"],
            AgentCapabilityAlias.locale == payload.get("locale", "zh-CN"),
            AgentCapabilityAlias.normalized_alias == normalized,
        )
        .first()
    )
    if duplicate and (row is None or duplicate.id != row.id):
        raise ConflictError("该能力已存在等价别名", code=40901)
    if row is None:
        row = AgentCapabilityAlias()
        db.add(row)
    row.capability_code = payload["capability_code"]
    row.alias = payload["alias"].strip()
    row.normalized_alias = normalized
    row.locale = payload.get("locale", "zh-CN")
    row.weight = float(payload.get("weight", 1.0))
    row.enabled = int(bool(payload.get("enabled", True)))
    db.flush()
    audit_service.log(
        db,
        actor,
        "capability_alias_upsert",
        target_type="agent_capability_alias",
        target_id=row.id,
        detail=f"capability={row.capability_code}; locale={row.locale}",
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return row


def delete_alias(db: Session, actor: User, alias_id: int) -> None:
    row = db.get(AgentCapabilityAlias, alias_id)
    if not row:
        raise NotFoundError("能力别名不存在", code=40400)
    db.delete(row)
    audit_service.log(
        db,
        actor,
        "capability_alias_delete",
        target_type="agent_capability_alias",
        target_id=alias_id,
        detail=f"capability={row.capability_code}; locale={row.locale}",
        commit=False,
    )
    db.commit()
