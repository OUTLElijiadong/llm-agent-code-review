"""把 Streamable HTTP MCP Server 映射为 DeepSeek function tools。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.user import User
from app.utils.api_resolver import validate_ai_base_url
from app.utils.public_http import PinnedPublicUrl, pin_public_http_url

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class McpServerConfig:
    """单个 MCP Streamable HTTP Server 配置。"""

    name: str
    url: str
    headers: Dict[str, str]


@dataclass(frozen=True)
class McpToolBinding:
    """模型工具名到 MCP Server 原始工具名的绑定。"""

    model_name: str
    server: McpServerConfig
    tool_name: str
    description: str
    input_schema: Dict[str, Any]
    requires_approval: bool = True
    managed_kind: str = ""

    def as_responses_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": self.model_name,
            "description": self.description,
            "parameters": self.input_schema,
        }


def load_mcp_server_configs(raw: Optional[str] = None) -> list[McpServerConfig]:
    """解析并校验 MCP Server JSON；凭据只保留在请求头，不写日志。"""

    source = settings.mcp_servers_json if raw is None else raw
    try:
        values = json.loads(source or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("MCP_SERVERS_JSON 不是有效 JSON") from exc
    if not isinstance(values, list):
        raise ValueError("MCP_SERVERS_JSON 必须是数组")

    result: list[McpServerConfig] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError("每个 MCP Server 配置必须是对象")
        name = _safe_component(str(value.get("name") or ""))
        if not name or name in seen:
            raise ValueError("MCP Server name 不能为空且不能重复")
        raw_url = str(value.get("url") or "").strip()
        url = validate_mcp_url(raw_url)
        headers_value = value.get("headers") or {}
        if not isinstance(headers_value, Mapping):
            raise ValueError(f"MCP Server {name} 的 headers 必须是对象")
        headers = {str(key): str(item) for key, item in headers_value.items() if str(key).strip()}
        result.append(McpServerConfig(name=name, url=url, headers=headers))
        seen.add(name)
    return result


class McpToolProvider:
    """发现 MCP 工具并执行调用；每个实例绑定一次 Agent HTTP 请求。"""

    # Responses 用户面需要发现内部受管能力；外部 MCP 仍由下方的当前用户
    # 超管判断过滤。旧的测试替身没有这个标记，因此不会改变其行为。
    supports_user_scoped_managed_tools = True

    def __init__(
        self,
        configs: Optional[Iterable[McpServerConfig]] = None,
        *,
        db: Optional[Session] = None,
        agent_code: str = "",
        user: Optional[User] = None,
    ) -> None:
        self._db = db
        self._agent_code = agent_code
        self._user = user
        self._explicit_configs = configs is not None
        self._configs = list(configs) if configs is not None else []
        self._bindings: Dict[str, McpToolBinding] = {}

    async def discover(self) -> list[Dict[str, Any]]:
        """从所有配置 Server 读取 tools/list 并转为 Responses tool schema。"""

        if self._db is not None and not self._explicit_configs and self._registry_configured():
            return self._discover_registry()
        if not self._explicit_configs:
            # 数据库模式未登记任何服务时，也不能让普通用户回退到环境变量
            # 中的远程 MCP 配置。只有唯一 admin 超管可以使用兼容回退。
            if self._db is not None:
                from app.services import rbac_service

                if self._user is None or not rbac_service.is_super_admin_user(
                    self._db, int(self._user.id)
                ):
                    self._bindings = {}
                    return []
            self._configs = load_mcp_server_configs()
        return await self._discover_live()

    async def _discover_live(self) -> list[Dict[str, Any]]:
        bindings: Dict[str, McpToolBinding] = {}
        for server in self._configs:
            tools = await self.list_raw_tools(server)
            for item in tools:
                if not isinstance(item, Mapping):
                    continue
                tool_name = str(item.get("name") or "").strip()
                if not tool_name:
                    continue
                model_name = f"mcp_{_safe_component(server.name)}_{_safe_component(tool_name)}"[:64]
                if model_name in bindings:
                    raise RuntimeError(f"MCP 工具映射重名: {model_name}")
                schema = item.get("inputSchema") or {"type": "object", "properties": {}}
                if not isinstance(schema, Mapping):
                    schema = {"type": "object", "properties": {}}
                bindings[model_name] = McpToolBinding(
                    model_name=model_name,
                    server=server,
                    tool_name=tool_name,
                    description=str(item.get("description") or f"MCP {server.name} 工具 {tool_name}"),
                    input_schema=dict(schema),
                    requires_approval=True,
                )
        self._bindings = bindings
        return [binding.as_responses_tool() for binding in bindings.values()]

    async def list_raw_tools(self, server: McpServerConfig) -> list[Dict[str, Any]]:
        """返回单个远程 Server 的原始 tools/list 结果。"""

        payload = await self._session_rpc(server, "tools/list", {})
        tools = payload.get("tools") if isinstance(payload, Mapping) else None
        if not isinstance(tools, list):
            raise RuntimeError(f"MCP Server {server.name} 的 tools/list 缺少 tools 数组")
        return [dict(item) for item in tools if isinstance(item, Mapping)]

    def _registry_configured(self) -> bool:
        bind = self._db.get_bind() if self._db is not None else None
        if bind is None:
            return False
        if "mcp_server" not in inspect(bind).get_table_names():
            return False
        from app.models.agent_capability import McpServer

        # 表存在后的查询故障必须直接向上抛出，不能误回退到
        # MCP_SERVERS_JSON 绕过数据库治理。
        return bool(self._db.query(McpServer.id).first())

    def _discover_registry(self) -> list[Dict[str, Any]]:
        """数据库注册表存在时必须以它为权威来源，不允许 env 绕过治理。"""

        if (
            self._db is None
            or not self._agent_code
            or self._user is None
            or int(getattr(self._user, "status", 0) or 0) != 1
        ):
            self._bindings = {}
            return []
        from app.models.agent_capability import AgentMcpBinding, McpServer, McpTool
        from app.services.managed_mcp_adapter import (
            is_live_managed_kind,
            managed_kind_ready,
            managed_tool_permissions,
        )
        from app.services.mcp_governance_service import decrypt_server_headers

        rows = (
            self._db.query(AgentMcpBinding, McpTool, McpServer)
            .join(McpTool, McpTool.id == AgentMcpBinding.tool_id)
            .join(McpServer, McpServer.id == McpTool.server_id)
            .filter(
                AgentMcpBinding.agent_code == self._agent_code,
                AgentMcpBinding.enabled == 1,
                AgentMcpBinding.permission != "deny",
                AgentMcpBinding.bound_schema_sha256 == McpTool.schema_sha256,
                McpTool.enabled == 1,
                McpServer.enabled == 1,
                McpServer.status == "healthy",
            )
            .order_by(AgentMcpBinding.id.asc())
            .all()
        )
        bindings: Dict[str, McpToolBinding] = {}
        for binding, tool, server in rows:
            managed_kind = str(server.managed_kind or "")
            if server.transport == "managed":
                # 只有已接入的内部执行器允许普通用户发现；Playwright 等
                # 尚未隔离/配置的 managed 服务始终不可用。
                if (
                    self._user is None
                    or not is_live_managed_kind(managed_kind)
                    or not managed_kind_ready(self._db, managed_kind)
                ):
                    continue
                required_permissions = managed_tool_permissions(managed_kind, tool.tool_name)
                if required_permissions:
                    from app.services import rbac_service

                    if not all(
                        rbac_service.check_permission(
                            self._db,
                            int(self._user.id),
                            permission,
                        )
                        for permission in required_permissions
                    ):
                        continue
            elif server.transport == "streamable_http":
                # 远程凭据和网络能力仅暴露给当前唯一 admin 超级管理员。
                from app.services import rbac_service

                if not rbac_service.is_super_admin_user(self._db, int(self._user.id)):
                    continue
            else:
                continue
            if tool.model_name in bindings:
                raise RuntimeError(f"MCP 工具映射重名: {tool.model_name}")
            try:
                schema = json.loads(tool.input_schema_json)
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError(f"MCP 工具 Schema 无效: {tool.model_name}") from exc
            if not isinstance(schema, dict):
                raise RuntimeError(f"MCP 工具 Schema 无效: {tool.model_name}")
            server_config = McpServerConfig(
                name=server.code,
                url=str(server.url or ""),
                headers=(
                    {}
                    if server.transport == "managed"
                    else decrypt_server_headers(server, self._db)
                ),
            )
            bindings[tool.model_name] = McpToolBinding(
                model_name=tool.model_name,
                server=server_config,
                tool_name=tool.tool_name,
                description=tool.description or f"MCP {server.code} 工具 {tool.tool_name}",
                input_schema=schema,
                requires_approval=bool(
                    binding.requires_approval or binding.permission == "escalate"
                ),
                managed_kind=managed_kind if server.transport == "managed" else "",
            )
        self._bindings = bindings
        return [item.as_responses_tool() for item in bindings.values()]

    def has_tool(self, model_name: str) -> bool:
        return model_name in self._bindings

    def requires_approval(self, model_name: str) -> bool:
        binding = self._bindings.get(model_name)
        if binding is None:
            return True
        return binding.managed_kind == "playwright" or binding.requires_approval

    def is_managed_tool(self, model_name: str) -> bool:
        """返回当前已发现工具是否由本地受管执行器提供。"""

        binding = self._bindings.get(model_name)
        return bool(binding and binding.managed_kind)

    def is_external_tool(self, model_name: str) -> bool:
        """返回当前已发现工具是否需要超级管理员的远程 MCP 权限。"""

        binding = self._bindings.get(model_name)
        return bool(binding and not binding.managed_kind)

    async def call(self, model_name: str, arguments: Mapping[str, Any]) -> Any:
        binding = self._bindings.get(model_name)
        if binding is None:
            raise KeyError(f"未知 MCP 工具: {model_name}")
        if binding.managed_kind:
            if self._db is None or self._user is None:
                raise RuntimeError("受管 MCP 调用缺少当前用户上下文")
            from app.services.managed_mcp_adapter import call_managed_tool

            return call_managed_tool(
                self._db,
                self._user,
                binding.managed_kind,
                binding.tool_name,
                arguments,
            )
        return await self._session_rpc(
            binding.server,
            "tools/call",
            {"name": binding.tool_name, "arguments": dict(arguments)},
        )

    async def _session_rpc(
        self,
        server: McpServerConfig,
        method: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        # 一个 MCP 会话内所有请求复用同一次 DNS 校验结果，避免初始化与工具调用之间发生重绑定。
        target = pin_public_http_url(server.url)
        timeout = httpx.Timeout(settings.mcp_timeout)
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            initial, session_id = await _rpc(
                client,
                server,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "prism-responses", "version": "1.0"},
                },
                request_id=1,
                target=target,
            )
            if not isinstance(initial, Mapping):
                raise RuntimeError(f"MCP Server {server.name} initialize 返回无效")
            await _notification(
                client,
                server,
                "notifications/initialized",
                {},
                session_id,
                target=target,
            )
            result, _ = await _rpc(
                client,
                server,
                method,
                params,
                request_id=2,
                session_id=session_id,
                target=target,
            )
            return result


async def _rpc(
    client: httpx.AsyncClient,
    server: McpServerConfig,
    method: str,
    params: Mapping[str, Any],
    *,
    request_id: int,
    session_id: str = "",
    target: Optional[PinnedPublicUrl] = None,
) -> tuple[Mapping[str, Any], str]:
    target = target or pin_public_http_url(server.url)
    headers = _headers(server, session_id, host_header=target.host_header)
    response = await client.post(
        target.request_url,
        headers=headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
        extensions=target.request_extensions,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"MCP Server {server.name} HTTP {response.status_code}")
    payload = _decode_rpc_response(response)
    error = payload.get("error")
    if error:
        message = error.get("message") if isinstance(error, Mapping) else str(error)
        raise RuntimeError(f"MCP Server {server.name} 调用失败: {message}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError(f"MCP Server {server.name} 返回缺少 result")
    return result, response.headers.get("mcp-session-id", session_id)


async def _notification(
    client: httpx.AsyncClient,
    server: McpServerConfig,
    method: str,
    params: Mapping[str, Any],
    session_id: str,
    *,
    target: Optional[PinnedPublicUrl] = None,
) -> None:
    target = target or pin_public_http_url(server.url)
    response = await client.post(
        target.request_url,
        headers=_headers(server, session_id, host_header=target.host_header),
        json={"jsonrpc": "2.0", "method": method, "params": dict(params)},
        extensions=target.request_extensions,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"MCP Server {server.name} 初始化通知失败: HTTP {response.status_code}")


def _decode_rpc_response(response: httpx.Response) -> Mapping[str, Any]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        value = response.json()
        if not isinstance(value, Mapping):
            raise RuntimeError("MCP JSON-RPC 响应必须是对象")
        return value
    for frame in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
        if not data:
            continue
        value = json.loads(data)
        if isinstance(value, Mapping):
            return value
    raise RuntimeError("MCP SSE 响应缺少 JSON-RPC data")


def _headers(server: McpServerConfig, session_id: str, *, host_header: str = "") -> Dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    # Host 必须与固定连接目标的原始域名一致，不允许配置头覆盖。
    reserved = {
        "accept",
        "connection",
        "content-length",
        "content-type",
        "host",
        "mcp-session-id",
        "transfer-encoding",
    }
    headers.update(
        {key: value for key, value in server.headers.items() if key.lower() not in reserved}
    )
    if host_header:
        headers["Host"] = host_header
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _safe_component(value: str) -> str:
    return _SAFE_NAME.sub("_", value.strip()).strip("_-")


def validate_mcp_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.path in {"", "/"}:
        try:
            base = validate_ai_base_url(
                value,
                resolve_host=settings.enforce_ai_base_url_dns_check,
                allow_private=settings.mcp_allow_private_urls,
            )
        except ValidationError as exc:
            raise ValueError(exc.message) from exc
        return base
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("MCP Server URL 必须是无内嵌凭据的 http(s) URL")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        validate_ai_base_url(
            origin,
            resolve_host=settings.enforce_ai_base_url_dns_check,
            allow_private=settings.mcp_allow_private_urls,
        )
    except ValidationError as exc:
        raise ValueError(exc.message) from exc
    if parsed.query or parsed.fragment:
        raise ValueError("MCP Server URL 不能包含 query 或 fragment")
    return value.rstrip("/")


# 保留旧的私有名称，避免破坏已有测试和调用方。
_validate_mcp_url = validate_mcp_url
