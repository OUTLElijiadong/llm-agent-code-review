"""MCP Server 配置、发现和调用映射测试。"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from app.services import mcp_tool_provider as module
from app.services.mcp_tool_provider import McpToolProvider, load_mcp_server_configs


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

    async def fake_rpc(_client, _server, method, params, *, request_id, session_id=""):
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
