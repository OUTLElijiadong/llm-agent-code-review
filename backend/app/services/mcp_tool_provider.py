"""把 Streamable HTTP MCP Server 映射为 DeepSeek function tools。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.utils.api_resolver import validate_ai_base_url

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
        url = _validate_mcp_url(raw_url)
        headers_value = value.get("headers") or {}
        if not isinstance(headers_value, Mapping):
            raise ValueError(f"MCP Server {name} 的 headers 必须是对象")
        headers = {str(key): str(item) for key, item in headers_value.items() if str(key).strip()}
        result.append(McpServerConfig(name=name, url=url, headers=headers))
        seen.add(name)
    return result


class McpToolProvider:
    """发现 MCP 工具并执行调用；每个实例绑定一次 Agent HTTP 请求。"""

    def __init__(self, configs: Optional[Iterable[McpServerConfig]] = None) -> None:
        self._configs = list(configs) if configs is not None else load_mcp_server_configs()
        self._bindings: Dict[str, McpToolBinding] = {}

    async def discover(self) -> list[Dict[str, Any]]:
        """从所有配置 Server 读取 tools/list 并转为 Responses tool schema。"""

        bindings: Dict[str, McpToolBinding] = {}
        for server in self._configs:
            payload = await self._session_rpc(server, "tools/list", {})
            tools = payload.get("tools") if isinstance(payload, Mapping) else None
            if not isinstance(tools, list):
                raise RuntimeError(f"MCP Server {server.name} 的 tools/list 缺少 tools 数组")
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
                )
        self._bindings = bindings
        return [binding.as_responses_tool() for binding in bindings.values()]

    def has_tool(self, model_name: str) -> bool:
        return model_name in self._bindings

    async def call(self, model_name: str, arguments: Mapping[str, Any]) -> Any:
        binding = self._bindings.get(model_name)
        if binding is None:
            raise KeyError(f"未知 MCP 工具: {model_name}")
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
        timeout = httpx.Timeout(settings.mcp_timeout)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
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
            )
            if not isinstance(initial, Mapping):
                raise RuntimeError(f"MCP Server {server.name} initialize 返回无效")
            await _notification(client, server, "notifications/initialized", {}, session_id)
            result, _ = await _rpc(
                client,
                server,
                method,
                params,
                request_id=2,
                session_id=session_id,
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
) -> tuple[Mapping[str, Any], str]:
    headers = _headers(server, session_id)
    response = await client.post(
        server.url,
        headers=headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
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
) -> None:
    response = await client.post(
        server.url,
        headers=_headers(server, session_id),
        json={"jsonrpc": "2.0", "method": method, "params": dict(params)},
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


def _headers(server: McpServerConfig, session_id: str) -> Dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **server.headers,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _safe_component(value: str) -> str:
    return _SAFE_NAME.sub("_", value.strip()).strip("_-")


def _validate_mcp_url(value: str) -> str:
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
