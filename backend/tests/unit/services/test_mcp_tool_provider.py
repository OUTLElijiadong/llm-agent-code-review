"""MCP Server 配置、发现和调用映射测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services import mcp_tool_provider as module
from app.services.mcp_tool_provider import McpServerConfig, McpToolProvider, load_mcp_server_configs


def test_load_mcp_configs_validates_names_urls_and_headers(monkeypatch) -> None:
    monkeypatch.setattr(module.settings, "mcp_allow_private_urls", False)
    configs = load_mcp_server_configs(
        '[{"name":"docs","url":"https://mcp.example.com/rpc",'
        '"headers":{"Authorization":"Bearer secret"}}]'
    )
    assert configs[0].name == "docs"
    assert configs[0].url == "https://mcp.example.com/rpc"
    assert configs[0].headers["Authorization"] == "Bearer secret"

    with pytest.raises(ValueError):
        load_mcp_server_configs('[{"name":"x","url":"http://127.0.0.1:9000/mcp"}]')


@pytest.mark.asyncio
async def test_discover_maps_tools_and_call_uses_original_name(monkeypatch) -> None:
    configs = load_mcp_server_configs('[{"name":"repo","url":"https://mcp.example.com/rpc"}]')
    observed: list[tuple[str, Mapping[str, Any]]] = []

    monkeypatch.setattr(
        module,
        "pin_public_http_url",
        lambda url: SimpleNamespace(
            request_url=url,
            host_header="mcp.example.com",
            request_extensions={"sni_hostname": "mcp.example.com"},
        ),
    )

    async def fake_rpc(_client, _server, method, params, *, request_id, session_id="", target=None):
        observed.append((method, params))
        if method == "initialize":
            return {"protocolVersion": "2025-06-18"}, "session-1"
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "read/file",
                        "description": "读取仓库文件",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ]
            }, "session-1"
        return {"content": [{"type": "text", "text": "ok"}]}, "session-2"

    async def fake_notification(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(module, "_rpc", fake_rpc)
    monkeypatch.setattr(module, "_notification", fake_notification)
    provider = McpToolProvider(configs)
    tools = await provider.discover()

    assert tools[0]["name"] == "mcp_repo_read_file"
    result = await provider.call("mcp_repo_read_file", {"path": "README.md"})
    assert result["content"][0]["text"] == "ok"
    assert observed[-1] == ("tools/call", {"name": "read/file", "arguments": {"path": "README.md"}})


@pytest.mark.asyncio
async def test_registry_query_failure_never_falls_back_to_environment(db, monkeypatch) -> None:
    """MCP 治理表存在时，数据库异常必须失败关闭。"""

    monkeypatch.setattr(
        module,
        "load_mcp_server_configs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("registry failure must not use environment fallback")
        ),
    )
    monkeypatch.setattr(
        Session,
        "query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
    )

    provider = McpToolProvider(db=db, agent_code="manager")
    with pytest.raises(RuntimeError, match="registry unavailable"):
        await provider.discover()


@pytest.mark.asyncio
async def test_session_rpc_pins_ip_preserves_host_sni_and_disables_env_proxy(monkeypatch) -> None:
    """MCP 请求必须连接已校验 IP，同时保留 Host 和 TLS SNI。"""

    pin_calls: list[str] = []
    client_options: list[dict[str, Any]] = []
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_pin(url: str):
        pin_calls.append(url)
        return SimpleNamespace(
            request_url="https://203.0.113.10:8443/rpc",
            host_header="mcp.example.com:8443",
            request_extensions={"sni_hostname": "mcp.example.com"},
        )

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload: Mapping[str, Any], *, session_id: str = "") -> None:
            self._payload = payload
            self.headers = {"content-type": "application/json"}
            if session_id:
                self.headers["mcp-session-id"] = session_id

        def json(self) -> Mapping[str, Any]:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            client_options.append(dict(kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            posts.append((url, dict(kwargs)))
            method = str(kwargs["json"]["method"])
            if method == "initialize":
                return FakeResponse(
                    {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}},
                    session_id="session-1",
                )
            if method == "tools/list":
                return FakeResponse({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
            return FakeResponse({})

    monkeypatch.setattr(module, "pin_public_http_url", fake_pin, raising=False)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    server = McpServerConfig(
        name="secure",
        url="https://mcp.example.com:8443/rpc",
        headers={"Authorization": "Bearer secret", "Host": "attacker.invalid"},
    )

    result = await McpToolProvider([server])._session_rpc(server, "tools/list", {})

    assert result == {"tools": []}
    assert pin_calls == [server.url]
    assert client_options[0]["trust_env"] is False
    assert client_options[0]["follow_redirects"] is False
    assert len(posts) == 3
    for url, request in posts:
        assert url == "https://203.0.113.10:8443/rpc"
        assert request["headers"]["Host"] == "mcp.example.com:8443"
        assert request["headers"]["Authorization"] == "Bearer secret"
        assert request["extensions"] == {"sni_hostname": "mcp.example.com"}


@pytest.mark.asyncio
async def test_session_rpc_rejects_private_dns_before_opening_client(monkeypatch) -> None:
    """MCP 域名解析到私网时不得创建 HTTP 客户端。"""

    monkeypatch.setattr(
        "app.utils.public_http.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.0.0.8", 443))],
    )
    opened = False

    class NoHttpClient:
        def __init__(self, **_kwargs: Any) -> None:
            nonlocal opened
            opened = True
            raise AssertionError("private MCP target must be rejected before client creation")

    monkeypatch.setattr(module.httpx, "AsyncClient", NoHttpClient)
    server = McpServerConfig(name="private-alias", url="https://mcp.example.com/rpc", headers={})

    with pytest.raises(ValidationError, match="内网或保留地址"):
        await McpToolProvider([server])._session_rpc(server, "tools/list", {})
    assert opened is False
