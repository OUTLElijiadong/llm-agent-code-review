"""真实 Prism managed MCP adapter 的发现、身份与权限回归。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.exceptions import NotFoundError, PermissionError
from app.core.permission_codes import PermissionCode
from app.models.agent_capability import AgentMcpBinding, McpServer, McpTool
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import agent_responses_service as responses_module
from app.services import managed_mcp_adapter, mcp_governance_service
from app.services.agent_responses_service import PrismToolExecutor
from app.services.deepseek_responses_runtime import ToolCall
from app.services.mcp_tool_provider import McpToolProvider


def _checksum(schema: dict) -> str:
    return hashlib.sha256(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _project_with_file(db, owner: User, *, name: str = "managed-demo") -> Project:
    project = Project(user_id=owner.id, project_name=name, language="python", status="active")
    db.add(project)
    db.flush()
    db.add(
        CodeFile(
            project_id=project.id,
            file_name="main.py",
            file_path="src/main.py",
            language="python",
            content="print('managed')\n",
            size_bytes=17,
            raw_size=17,
            line_count=1,
            version_no=1,
            status="active",
            is_binary=0,
        )
    )
    db.commit()
    return project


def _models_for_server(db, code: str) -> dict[str, str]:
    server = db.query(McpServer).filter(McpServer.code == code).one()
    return {
        row.tool_name: row.model_name
        for row in db.query(McpTool).filter(McpTool.server_id == server.id).all()
    }


def _add_healthy_remote_binding(db, agent_code: str) -> str:
    schema = {"type": "object", "properties": {}}
    server = McpServer(
        code="remote-docs",
        name="Remote docs",
        transport="streamable_http",
        url="https://mcp.example.com/rpc",
        status="healthy",
        enabled=1,
        credential_required=0,
    )
    db.add(server)
    db.flush()
    tool = McpTool(
        server_id=server.id,
        tool_name="read_docs",
        model_name="mcp_remote_docs_read_docs_12345678",
        display_name="Read docs",
        description="remote-only",
        input_schema_json=json.dumps(schema),
        schema_sha256=_checksum(schema),
        risk_level="low",
        enabled=1,
    )
    db.add(tool)
    db.flush()
    db.add(
        AgentMcpBinding(
            agent_code=agent_code,
            tool_id=tool.id,
            permission="allow",
            requires_approval=0,
            bound_schema_sha256=tool.schema_sha256,
            enabled=1,
        )
    )
    db.commit()
    return tool.model_name


@pytest.mark.asyncio
async def test_managed_provider_discovers_project_scoped_tools_and_hides_remote_mcp(
    db,
    super_admin_user,
    monkeypatch,
) -> None:
    member = User(username="managed_member", password="x", role="user", status=1)
    other = User(username="managed_other", password="x", role="user", status=1)
    db.add_all([member, other])
    db.commit()
    project = _project_with_file(db, member)
    denied_project = _project_with_file(db, other, name="private-project")
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    remote_model = _add_healthy_remote_binding(db, "chat_assistant")
    monkeypatch.setattr(managed_mcp_adapter, "check_permission", lambda *_args: True)
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_args: True)

    provider = McpToolProvider(db=db, agent_code="chat_assistant", user=member)
    schemas = await provider.discover()
    names = {schema["name"] for schema in schemas}
    prism_code = _models_for_server(db, "prism-code")
    prism_sandbox = _models_for_server(db, "prism-sandbox")

    assert set(prism_code.values()) <= names
    assert set(prism_sandbox.values()) <= names
    assert remote_model not in names
    assert not provider.has_tool(remote_model)
    assert provider.is_managed_tool(prism_code["list_project_source"])
    assert provider.is_external_tool(prism_code["list_project_source"]) is False

    listed = await provider.call(prism_code["list_project_source"], {"project_id": project.id})
    assert listed["project_id"] == project.id
    assert listed["total"] == 1
    assert listed["files"][0]["file_path"] == "src/main.py"
    assert "content" not in listed["files"][0]

    download = await provider.call(prism_code["download_project_source"], {"project_id": project.id})
    assert download == {
        "project_id": project.id,
        "project_name": project.project_name,
        "download_path": f"/api/projects/{project.id}/source-archive",
        "download_url": f"/api/projects/{project.id}/source-archive",
        "authentication": "current_user",
        "source_mode": "files",
    }

    with pytest.raises(NotFoundError):
        await provider.call(prism_code["list_project_source"], {"project_id": denied_project.id})


def test_managed_download_requires_file_download_permission(monkeypatch) -> None:
    monkeypatch.setattr(
        managed_mcp_adapter,
        "check_permission",
        lambda _db, _user_id, permission: permission == PermissionCode.PROJECT_VIEW,
    )
    with pytest.raises(PermissionError, match="file:download"):
        managed_mcp_adapter._project_source_download(MagicMock(), SimpleNamespace(id=7), 11)


def test_managed_download_requires_project_view_before_returning_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        managed_mcp_adapter,
        "check_permission",
        lambda _db, _user_id, permission: permission == PermissionCode.FILE_DOWNLOAD,
    )
    with pytest.raises(PermissionError, match="project:view"):
        managed_mcp_adapter._project_source_download(MagicMock(), SimpleNamespace(id=7), 11)


@pytest.mark.asyncio
async def test_managed_source_tools_are_hidden_without_project_view(
    db,
    super_admin_user,
    monkeypatch,
) -> None:
    member = User(username="managed_hidden_member", password="x", role="user", status=1)
    db.add(member)
    db.commit()
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    monkeypatch.setattr(
        "app.services.rbac_service.check_permission",
        lambda _db, _user_id, permission: permission != PermissionCode.PROJECT_VIEW,
    )
    provider = McpToolProvider(db=db, agent_code="chat_assistant", user=member)

    schemas = await provider.discover()
    names = {schema["name"] for schema in schemas}
    source_models = set(_models_for_server(db, "prism-code").values())

    assert names.isdisjoint(source_models)


@pytest.mark.asyncio
async def test_managed_sandbox_adapter_forwards_only_current_user_and_bounded_arguments(
    db,
    super_admin_user,
    monkeypatch,
) -> None:
    member = User(username="sandbox_member", password="x", role="user", status=1)
    db.add(member)
    db.commit()
    project = _project_with_file(db, member, name="sandbox-project")
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    provider = McpToolProvider(db=db, agent_code="chat_assistant", user=member)
    await provider.discover()
    models = _models_for_server(db, "prism-sandbox")
    seen: list[tuple[str, object, dict | str | int]] = []
    environment = SimpleNamespace(public_id="sbx_abcdefghijklmnopqrstuvwx")

    def fake_create(_db, actor, payload):
        seen.append(("create", actor, dict(payload)))
        return environment

    def fake_dict(_db, row):
        assert row is environment
        return {"public_id": row.public_id, "status": "queued"}

    def fake_stop(_db, actor, public_id):
        seen.append(("close", actor, public_id))
        return {"public_id": public_id, "status": "stopped"}

    def fake_extend(_db, actor, public_id, hours):
        seen.append(("extend", actor, {"public_id": public_id, "hours": hours}))
        return {"public_id": public_id, "status": "ready"}

    monkeypatch.setattr(managed_mcp_adapter.sandbox_service, "create_environment", fake_create)
    monkeypatch.setattr(managed_mcp_adapter.sandbox_service, "environment_to_dict", fake_dict)
    monkeypatch.setattr(managed_mcp_adapter.sandbox_service, "stop_environment", fake_stop)
    monkeypatch.setattr(managed_mcp_adapter.sandbox_service, "extend_environment", fake_extend)

    created = await provider.call(
        models["create_test"],
        {"project_id": project.id, "language": "python", "test_mode": "whitebox"},
    )
    deployed = await provider.call(
        models["create_deployment"],
        {"project_id": project.id, "language": "php", "ttl_hours": 24},
    )
    public_id = environment.public_id
    closed = await provider.call(models["close"], {"public_id": public_id})
    extended = await provider.call(models["extend"], {"public_id": public_id, "hours": 12})

    assert created["status"] == "queued"
    assert deployed["status"] == "queued"
    assert closed["status"] == "stopped"
    assert extended["status"] == "ready"
    assert seen == [
        ("create", member, {
            "project_id": project.id,
            "purpose": "test",
            "language": "python",
            "test_mode": "whitebox",
        }),
        ("create", member, {
            "project_id": project.id,
            "purpose": "deploy",
            "language": "php",
            "test_mode": "deploy",
            "ttl_hours": 24,
        }),
        ("close", member, public_id),
        ("extend", member, {"public_id": public_id, "hours": 12}),
    ]

    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        await provider.call(
            models["create_test"],
            {
                "project_id": project.id,
                "language": "python",
                "test_mode": "whitebox",
                "owner_id": super_admin_user.id,
            },
        )


def test_playwright_adapter_uses_current_user_and_saved_sandbox_boundary(monkeypatch) -> None:
    actor = SimpleNamespace(id=41)
    db = MagicMock()
    seen: list[tuple[object, object, str, str]] = []
    monkeypatch.setattr(managed_mcp_adapter, "managed_kind_ready", lambda *_args: True)
    monkeypatch.setattr(
        managed_mcp_adapter.sandbox_service,
        "run_browser_blackbox",
        lambda actual_db, actual_actor, sandbox_id, target_url: (
            seen.append((actual_db, actual_actor, sandbox_id, target_url))
            or {"passed": True}
        ),
    )

    result = managed_mcp_adapter.call_managed_tool(
        db,
        actor,
        "playwright",
        "browser_blackbox",
        {"sandbox_id": "sbx_abcdefghijklmnopqrstuvwx", "target_url": "https://example.com/"},
    )

    assert result == {"passed": True}
    assert seen == [(db, actor, "sbx_abcdefghijklmnopqrstuvwx", "https://example.com/")]
    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        managed_mcp_adapter.call_managed_tool(
            db,
            actor,
            "playwright",
            "browser_blackbox",
            {
                "sandbox_id": "sbx_abcdefghijklmnopqrstuvwx",
                "target_url": "https://example.com/",
                "user_id": 99,
            },
        )


@pytest.mark.asyncio
async def test_user_responses_executor_exposes_and_executes_only_managed_mcp(
    db,
    super_admin_user,
    monkeypatch,
) -> None:
    member = User(username="responses_member", password="x", role="user", status=1)
    db.add(member)
    db.commit()
    project = _project_with_file(db, member, name="responses-project")
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    _add_healthy_remote_binding(db, "chat_assistant")
    provider = McpToolProvider(db=db, agent_code="chat_assistant", user=member)
    monkeypatch.setattr(managed_mcp_adapter, "check_permission", lambda *_args: True)
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_args: True)
    monkeypatch.setattr(responses_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    executor = PrismToolExecutor(
        db,
        member,
        surface="user",
        run_id="managed-mcp-user-run",
        mcp_provider=provider,
    )

    schemas = await executor.tool_schemas()
    names = {schema["name"] for schema in schemas}
    source_model = _models_for_server(db, "prism-code")["list_project_source"]
    assert source_model in names
    assert not any(name.startswith("mcp_remote_docs_") for name in names)

    result = await executor.execute(
        ToolCall("managed-source-list", source_model, {"project_id": project.id}, "")
    )
    assert result.status == "success"
    assert result.output["project_id"] == project.id
