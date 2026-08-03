"""MCP 注册、密钥、Schema 漂移和权限检索测试。"""

from __future__ import annotations

import hashlib
import json

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.models.agent_capability import AgentCapabilityAlias, AgentMcpBinding, McpServer, McpTool, SandboxWorker
from app.models.audit_log import AuditLog
from app.services import capability_catalog_service, mcp_governance_service
from app.services.mcp_tool_provider import McpToolProvider
from app.utils.api_resolver import _derive_fernet_key, decrypt_api_key_with_metadata


def _checksum(schema: dict) -> str:
    payload = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _persist_aliases(db, capability_code: str, aliases: tuple[str, ...]) -> None:
    for alias in aliases:
        db.add(
            AgentCapabilityAlias(
                capability_code=capability_code,
                alias=alias,
                normalized_alias=capability_catalog_service.normalize_text(alias),
                locale="zh-CN",
                weight=1.0,
                enabled=1,
            )
        )
    db.commit()


def _remote_tool(db, *, schema: dict | None = None):
    schema = schema or {"type": "object", "properties": {"path": {"type": "string"}}}
    server = McpServer(
        code="docs",
        name="Docs",
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
        tool_name="read_file",
        model_name="mcp_docs_read_file_12345678",
        display_name="读取文件",
        description="读取文档内容",
        input_schema_json=json.dumps(schema),
        schema_sha256=_checksum(schema),
        risk_level="low",
        enabled=1,
    )
    db.add(tool)
    db.flush()
    binding = AgentMcpBinding(
        agent_code="manager",
        tool_id=tool.id,
        permission="allow",
        requires_approval=0,
        bound_schema_sha256=tool.schema_sha256,
        enabled=1,
    )
    db.add(binding)
    db.commit()
    return server, tool, binding


def test_recommended_servers_enable_only_real_internal_managed_executors(db, super_admin_user) -> None:
    rows = mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    by_code = {row["code"]: row for row in rows}

    assert by_code["prism-code"]["status"] == "healthy"
    assert by_code["prism-code"]["enabled"] is True
    assert by_code["prism-sandbox"]["status"] == "healthy"
    assert by_code["prism-sandbox"]["enabled"] is True
    assert by_code["github"]["status"] == "credential_required"
    assert by_code["playwright"]["status"] == "registered"
    assert by_code["github"]["enabled"] is False
    assert by_code["playwright"]["enabled"] is False
    prism_servers = db.query(McpServer).filter(McpServer.code.in_(("prism-code", "prism-sandbox"))).all()
    prism_server_ids = {row.id for row in prism_servers}
    prism_tools = db.query(McpTool).filter(McpTool.server_id.in_(prism_server_ids)).all()
    assert prism_tools and all(tool.enabled == 1 for tool in prism_tools)
    binding_count = (
        db.query(AgentMcpBinding)
        .filter(AgentMcpBinding.tool_id.in_([tool.id for tool in prism_tools]))
        .count()
    )
    assert binding_count == len(prism_tools) * 2


def test_headers_are_encrypted_and_never_returned(db, super_admin_user, monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_governance_service,
        "validate_mcp_url",
        lambda value: value.rstrip("/"),
    )
    row = mcp_governance_service.upsert_server(
        db,
        super_admin_user,
        {
            "code": "secret-docs",
            "name": "Secret docs",
            "description": "",
            "transport": "streamable_http",
            "url": "https://mcp.example.com/rpc",
            "auth_type": "bearer",
            "headers": {"Authorization": "Bearer top-secret"},
            "enabled": False,
            "credential_required": True,
        },
    )
    stored = str(row.encrypted_headers)
    output = mcp_governance_service.list_servers(db)[0]

    assert "top-secret" not in stored
    assert mcp_governance_service.decrypt_server_headers(row, db) == {
        "Authorization": "Bearer top-secret"
    }
    assert output["has_credentials"] is True
    assert "encrypted_headers" not in output
    assert "headers" not in output


def test_headers_are_rotated_from_previous_encryption_key(db, monkeypatch) -> None:
    current = "current-mcp-encryption-secret-123456789"
    previous = "previous-mcp-encryption-secret-12345678"
    monkeypatch.setattr(settings, "api_key_encryption_keys", [current, previous])
    plaintext = json.dumps({"Authorization": "Bearer rotated-secret"})
    old_ciphertext = Fernet(_derive_fernet_key(previous)).encrypt(plaintext.encode()).decode()
    row = McpServer(
        code="rotation-test",
        name="Rotation test",
        transport="streamable_http",
        url="https://mcp.example.com/rpc",
        status="disabled",
        enabled=0,
        encrypted_headers=old_ciphertext,
    )
    db.add(row)
    db.commit()

    headers = mcp_governance_service.decrypt_server_headers(row, db)
    db.refresh(row)
    rotated = decrypt_api_key_with_metadata(row.encrypted_headers)

    assert headers == {"Authorization": "Bearer rotated-secret"}
    assert row.encrypted_headers != old_ciphertext
    assert rotated is not None
    assert rotated.needs_rotation is False


def test_schema_drift_disables_tool_and_binding(db, monkeypatch) -> None:
    server, tool, binding = _remote_tool(db)
    changed_schema = {"type": "object", "properties": {"uri": {"type": "string"}}}
    monkeypatch.setattr(
        mcp_governance_service,
        "_run_remote_discovery",
        lambda _db, _server: [
            {
                "name": "read_file",
                "description": "changed",
                "inputSchema": changed_schema,
                "annotations": {"readOnlyHint": True},
            }
        ],
    )

    mcp_governance_service.sync_tools(db, server.id)
    db.refresh(tool)
    db.refresh(binding)

    assert tool.schema_sha256 == _checksum(changed_schema)
    assert tool.enabled == 0
    assert binding.enabled == 0
    assert binding.bound_schema_sha256 != tool.schema_sha256


@pytest.mark.asyncio
async def test_database_registry_filters_binding_before_discovery_and_suppresses_env(
    db,
    super_admin_user,
    monkeypatch,
) -> None:
    _server, tool, binding = _remote_tool(db)
    monkeypatch.setattr(
        "app.services.mcp_tool_provider.load_mcp_server_configs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("env fallback must not run")),
    )

    provider = McpToolProvider(db=db, agent_code="manager", user=super_admin_user)
    tools = await provider.discover()
    assert [item["name"] for item in tools] == [tool.model_name]
    assert provider.requires_approval(tool.model_name) is False

    binding.bound_schema_sha256 = "0" * 64
    db.commit()
    drifted = McpToolProvider(db=db, agent_code="manager", user=super_admin_user)
    assert await drifted.discover() == []
    assert not drifted.has_tool(tool.model_name)


def test_alias_query_happens_after_authorization_and_ordinary_admin_never_sees_mcp(
    db,
    admin_user,
    monkeypatch,
) -> None:
    _server, _tool, _binding = _remote_tool(db)
    unauthorized = AgentCapabilityAlias(
        capability_code="mcp:docs:read_file",
        alias="秘密远程能力",
        normalized_alias="秘密远程能力",
        locale="zh-CN",
        weight=1.0,
        enabled=1,
    )
    db.add(unauthorized)
    db.commit()
    monkeypatch.setattr(
        capability_catalog_service.embedding_service,
        "embed_texts",
        lambda _db, values: ([[0.0] * 8 for _ in values], "test"),
    )

    rows = capability_catalog_service.search_capabilities(db, admin_user, "秘密远程能力", 20)

    assert not any(row["source"] == "mcp" for row in rows)
    assert not any("秘密远程能力" in row["aliases"] for row in rows)


def test_superadmin_can_find_authorized_mcp_via_alias(db, super_admin_user, monkeypatch) -> None:
    _remote_tool(db)
    alias = AgentCapabilityAlias(
        capability_code="mcp:docs:read_file",
        alias="帮我看文档",
        normalized_alias="帮我看文档",
        locale="zh-CN",
        weight=1.0,
        enabled=1,
    )
    db.add(alias)
    db.commit()
    monkeypatch.setattr(
        capability_catalog_service.embedding_service,
        "embed_texts",
        lambda _db, values: ([[0.0] * 8 for _ in values], "test"),
    )

    rows = capability_catalog_service.search_capabilities(db, super_admin_user, "帮我看文档", 5)

    assert rows[0]["code"] == "mcp:docs:read_file"
    assert rows[0]["score"] == 1.0


@pytest.mark.parametrize(
    "query",
    ("源码下载", "下载代码", "导出源码", "远程下载代码"),
)
def test_superadmin_can_find_real_managed_source_download_by_synonym(
    db,
    super_admin_user,
    monkeypatch,
    query,
) -> None:
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    _persist_aliases(db, "mcp:prism-code:download_project_source", (query,))
    monkeypatch.setattr(
        capability_catalog_service.embedding_service,
        "embed_texts",
        lambda _db, values: ([[0.0] * 8 for _ in values], "test"),
    )

    rows = capability_catalog_service.search_capabilities(db, super_admin_user, query, 5)

    assert rows[0]["code"] == "mcp:prism-code:download_project_source"
    assert rows[0]["source"] == "mcp"
    assert rows[0]["score"] == 1.0
    assert query in rows[0]["aliases"]


def test_managed_source_catalog_follows_app_permissions_without_server_access(
    db,
    super_admin_user,
    admin_user,
    monkeypatch,
) -> None:
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    _persist_aliases(
        db,
        "mcp:prism-code:download_project_source",
        ("远程下载代码",),
    )
    monkeypatch.setattr(
        capability_catalog_service.embedding_service,
        "embed_texts",
        lambda _db, values: ([[0.0] * 8 for _ in values], "test"),
    )

    rows = capability_catalog_service.search_capabilities(db, admin_user, "远程下载代码", 20)

    assert rows[0]["code"] == "mcp:prism-code:download_project_source"
    assert "远程下载代码" in rows[0]["aliases"]
    assert not any(row["code"] == "mcp:docs:read_file" for row in rows)
    assert capability_catalog_service.rbac_service.check_permission(
        db,
        admin_user.id,
        "server_ops:execute",
    ) is False


def test_persistent_alias_exposes_existing_sandbox_extend_capability(
    db,
    admin_user,
    monkeypatch,
) -> None:
    db.add(
        AgentCapabilityAlias(
            capability_code="sandbox:extend",
            alias="测试环境续期",
            normalized_alias="测试环境续期",
            locale="zh-CN",
            weight=1.0,
            enabled=1,
        )
    )
    db.commit()
    monkeypatch.setattr(
        capability_catalog_service.embedding_service,
        "embed_texts",
        lambda _db, values: ([[0.0] * 8 for _ in values], "test"),
    )

    rows = capability_catalog_service.search_capabilities(db, admin_user, "测试环境续期", 5)

    assert rows[0]["code"] == "sandbox:extend"
    assert rows[0]["source"] == "sandbox"
    assert rows[0]["score"] == 1.0
    assert rows[0]["requires_approval"] is True


def test_managed_health_reports_real_internal_executor_but_keeps_playwright_unavailable(db, super_admin_user) -> None:
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    row = db.query(McpServer).filter(McpServer.code == "prism-code").one()

    result = mcp_governance_service.check_health(db, row.id, super_admin_user)

    assert result["status"] == "healthy"
    assert result["enabled"] is True
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "mcp_health_check", AuditLog.target_id == str(row.id))
        .one()
    )
    assert audit.actor_id == super_admin_user.id
    assert audit.status == "success"
    assert "healthy" in audit.detail

    playwright = db.query(McpServer).filter(McpServer.code == "playwright").one()
    unavailable = mcp_governance_service.check_health(db, playwright.id, super_admin_user)
    assert unavailable["status"] == "unavailable"
    assert unavailable["enabled"] is False


def test_playwright_requires_real_worker_readiness_and_forces_approval(db, super_admin_user) -> None:
    mcp_governance_service.seed_recommended_servers(db, super_admin_user)
    server = db.query(McpServer).filter(McpServer.code == "playwright").one()
    worker = SandboxWorker(
        code="browser-ready-worker",
        name="Browser ready worker",
        worker_type="production_fallback",
        transport="unix",
        endpoint="/run/prism-sandbox/agent.sock",
        supported_languages_json=json.dumps(["python"]),
        supported_modes_json=json.dumps(["blackbox"]),
        runtime="runsc",
        max_concurrency=1,
        priority=1,
        status="healthy",
        enabled=1,
        fingerprint_json=json.dumps({
            "browser_blackbox": {
                "ready": True,
                "image_digest": "sha256:" + "a" * 64,
                "egress_policy_fingerprint": "b" * 64,
                "resource_policy": {
                    "network": "private_browser_to_fixed_target_proxy",
                    "target": "single_https_origin_and_pinned_public_ip",
                },
            }
        }),
    )
    db.add(worker)
    db.commit()

    enabled = mcp_governance_service.upsert_server(db, super_admin_user, {
        "code": "playwright",
        "name": "Playwright 黑盒测试",
        "description": "isolated browser",
        "transport": "managed",
        "managed_kind": "playwright",
        "auth_type": "none",
        "credential_required": False,
        "enabled": True,
    }, server.id)
    assert enabled.status == "healthy" and enabled.enabled == 1

    tools = mcp_governance_service.sync_tools(db, server.id, super_admin_user)
    tool = tools[0]
    mcp_governance_service.update_tool(db, super_admin_user, tool.id, {"enabled": True})
    binding = mcp_governance_service.upsert_binding(db, super_admin_user, {
        "agent_code": "manager",
        "tool_id": tool.id,
        "permission": "allow",
        "requires_approval": False,
        "enabled": True,
    })
    assert binding.requires_approval == 1
    assert db.query(AgentMcpBinding).filter(AgentMcpBinding.tool_id == tool.id).count() == 1


def test_sync_tools_records_actor_without_secret_material(db, super_admin_user, monkeypatch) -> None:
    server, _tool, _binding = _remote_tool(db)
    monkeypatch.setattr(
        mcp_governance_service,
        "_run_remote_discovery",
        lambda _db, _server: [
            {
                "name": "read_file",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
    )

    mcp_governance_service.sync_tools(db, server.id, super_admin_user)

    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "mcp_tool_sync", AuditLog.target_id == str(server.id))
        .one()
    )
    assert audit.actor_id == super_admin_user.id
    assert audit.status == "success"
    assert audit.detail == "code=docs; status=healthy; tools=1"
