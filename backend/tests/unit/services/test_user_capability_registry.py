"""普通用户页面、OpenAPI 与 Responses 能力白名单契约测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.discussion_bus import DiscussionBus
from app.agents.events import DiscussionTurn
from app.core.permission_codes import ALL_PERMISSION_CODES
from app.main import app
from app.models.agent_governance import ApprovalItem
from app.models.agent_response_run import AgentToolExecution
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import agent_responses_service as service_module
from app.services.admin_capability_registry import operation_contract
from app.services.admin_capability_service import AdminCapabilityError, prepare_request
from app.services.agent_responses_service import PrismToolExecutor
from app.services.deepseek_responses_runtime import ToolCall
from app.services.user_capability_registry import (
    CAPABILITY_BY_CODE,
    CRITICAL,
    READ,
    USER_CAPABILITIES,
    describe_capabilities,
    discovery_tool_schema,
    execution_tool_schema,
)


class EmptyMcp:
    async def discover(self) -> list[dict[str, Any]]:
        return []

    def has_tool(self, _: str) -> bool:
        return False


def _user(db) -> User:
    value = User(username="capability_user", password="x", role="user", status=1)
    db.add(value)
    db.commit()
    return value


def _executor(db, user: User, run_id: str = "run_user_capability") -> PrismToolExecutor:
    return PrismToolExecutor(
        db,
        user,
        surface="user",
        run_id=run_id,
        mcp_provider=EmptyMcp(),
    )


def test_every_user_capability_is_a_real_json_openapi_operation() -> None:
    openapi = app.openapi()

    assert len(USER_CAPABILITIES) == 113
    assert len(CAPABILITY_BY_CODE) == len(USER_CAPABILITIES)
    for spec in USER_CAPABILITIES:
        assert not spec.path.startswith(("/api/admin", "/api/auth", "/api/rbac", "/api/users"))
        operation = openapi["paths"][spec.path][spec.method.lower()]
        content = (operation.get("requestBody") or {}).get("content", {})
        assert not content or set(content) == {"application/json"}, spec.code
        response_content = {
            media_type
            for response in operation.get("responses", {}).values()
            for media_type in (response.get("content") or {})
        }
        assert not response_content or response_content <= {"application/json"}, spec.code
        contract = operation_contract(spec, openapi)  # type: ignore[arg-type]
        assert contract.schema["type"] == "object"
        assert contract.schema["additionalProperties"] is False
        if spec.method == "GET":
            assert spec.risk == READ

    security_codes = {spec.code for spec in USER_CAPABILITIES if spec.page == "/security"}
    assert security_codes == {
        "security.checklist",
        "security.dashboard",
        "security.findings",
        "security.scan_file",
        "security.scan_task",
        "security.scan_project",
        "security.scan_all_projects",
    }

    sandbox_codes = {spec.code for spec in USER_CAPABILITIES if spec.page == "/sandboxes"}
    assert sandbox_codes == {"sandboxes.list", "sandboxes.get"}
    # 沙箱创建/停止/续期属于副作用操作,只能走专用固定工具,不开放通用执行器
    assert not any(
        spec.page == "/sandboxes" and spec.method != "GET" for spec in USER_CAPABILITIES
    )


def test_registry_excludes_binary_stream_multipart_admin_and_secret_routes() -> None:
    paths = {spec.path for spec in USER_CAPABILITIES}

    assert "/api/code-files/upload" not in paths
    assert "/api/code-files/upload-folder" not in paths
    assert "/api/code-files/{file_id}/download" not in paths
    assert "/api/projects/{project_id}/source-archive" not in paths
    assert "/api/reports/tasks/{task_id}/export" not in paths
    assert "/api/agent-responses/stream" not in paths
    assert "/api/agents/events" not in paths
    assert "/api/api-config" in paths
    assert not any(spec.path == "/api/api-config" and spec.method == "PUT" for spec in USER_CAPABILITIES)
    assert "/api/api-config/test" not in paths
    assert not any(spec.path.startswith("/api/admin") for spec in USER_CAPABILITIES)


def test_explicit_permissions_match_rbac_catalog_except_migrated_report_template_permission() -> None:
    exceptions = {"report:template_manage"}

    for spec in USER_CAPABILITIES:
        if spec.permission:
            assert spec.permission in ALL_PERMISSION_CODES or spec.permission in exceptions, spec.code


def test_user_discovery_returns_exact_openapi_contract() -> None:
    rows = describe_capabilities(app.openapi(), page="/issues")

    assert {row["capability"] for row in rows} == {
        "issues.list",
        "issues.get",
        "issues.update_status",
        "issues.batch_update_status",
    }
    update = next(row for row in rows if row["capability"] == "issues.update_status")
    assert update["risk"] == "write"
    assert set(update["parameters"]["required"]) == {"issue_id", "status"}


def test_prepare_request_cannot_override_registered_method_or_path() -> None:
    spec = CAPABILITY_BY_CODE["issues.update_status"]
    path, query, body = prepare_request(
        spec,  # type: ignore[arg-type]
        {"issue_id": 17, "status": "fixed"},
        app.openapi(),
    )

    assert path == "/api/issues/17/status"
    assert query == {}
    assert body == {"status": "fixed"}
    with pytest.raises(AdminCapabilityError, match="不接受参数"):
        prepare_request(
            spec,  # type: ignore[arg-type]
            {"issue_id": 17, "status": "fixed", "path": "/api/admin", "method": "DELETE"},
            app.openapi(),
        )


def test_tool_schema_exposes_only_capability_code_and_params() -> None:
    discovery = discovery_tool_schema()
    execution = execution_tool_schema()

    assert discovery["name"] == "user_describe_capabilities"
    assert execution["name"] == "user_execute_capability"
    properties = execution["parameters"]["properties"]
    assert set(properties) == {"capability", "params"}
    assert set(properties["capability"]["enum"]) == set(CAPABILITY_BY_CODE)


@pytest.mark.asyncio
async def test_user_surface_exposes_registry_tools_but_admin_surface_does_not(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    user_tools = {item["name"] for item in await _executor(db, user).tool_schemas()}
    admin_executor = PrismToolExecutor(
        db,
        SimpleNamespace(id=99, role="admin", token_version=0),
        surface="admin",
        run_id="run_admin_surface",
        mcp_provider=EmptyMcp(),
    )
    admin_tools = {item["name"] for item in await admin_executor.tool_schemas()}

    assert {"user_describe_capabilities", "user_execute_capability"} <= user_tools
    assert "user_describe_capabilities" not in admin_tools
    assert "user_execute_capability" not in admin_tools


@pytest.mark.asyncio
async def test_user_discovery_filters_agent_studio_by_actual_rbac_permission(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        service_module.rbac_service,
        "check_permission",
        lambda _db, user_id, permission: user_id == user.id and permission == "agent_asset:create",
    )
    result = await _executor(db, user, "run_filtered_studio_discovery").execute(
        ToolCall(
            "call_filtered_studio_discovery",
            "user_describe_capabilities",
            {"page": "/agent-studio"},
            "{}",
        )
    )

    assert result.status == "success"
    assert result.output["count"] == 1
    assert [item["capability"] for item in result.output["items"]] == ["agent_studio.agents.create"]
    assert result.output["items"][0]["permission"] == "agent_asset:create"
    schemas = {item["name"]: item for item in await _executor(db, user).tool_schemas()}
    available = set(schemas["user_execute_capability"]["parameters"]["properties"]["capability"]["enum"])
    assert "agent_studio.agents.create" in available
    assert "agent_studio.agents.list" not in available
    assert "search_published_agents" not in schemas
    assert "invoke_published_agent" not in schemas
    assert "download_project_source" not in schemas


@pytest.mark.asyncio
async def test_fixed_download_tools_return_only_authorized_same_origin_urls(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    checked_permissions: list[str] = []

    def allow_permission(_db, user_id, permission):
        assert user_id == user.id
        checked_permissions.append(permission)
        return True

    report_checks: list[tuple[int, int]] = []
    file_checks: list[tuple[int, int]] = []
    monkeypatch.setattr(service_module.rbac_service, "check_permission", allow_permission)
    monkeypatch.setattr(
        service_module.report_service,
        "get_report_detail",
        lambda _db, actor, task_id: report_checks.append((actor.id, task_id)) or {"task": {"id": task_id}},
    )
    monkeypatch.setattr(
        service_module.code_file_service,
        "get_file",
        lambda _db, actor, file_id: file_checks.append((actor.id, file_id))
        or SimpleNamespace(id=file_id, file_name="evidence.bin", is_binary=1),
    )
    executor = _executor(db, user, "run_fixed_downloads")
    schemas = {item["name"]: item for item in await executor.tool_schemas()}

    assert set(schemas["download_report"]["parameters"]["properties"]["format"]["enum"]) == {
        "json",
        "html",
        "pdf",
        "word",
    }
    assert schemas["download_report"]["parameters"]["additionalProperties"] is False
    assert schemas["download_code_file"]["parameters"]["additionalProperties"] is False

    report = await executor.execute(
        ToolCall(
            "call_html_download",
            "download_report",
            {"task_id": 23, "format": "html", "template_type": "compliance"},
            "{}",
        )
    )
    code_file = await executor.execute(
        ToolCall("call_binary_download", "download_code_file", {"file_id": 41}, "{}")
    )

    assert report.status == "success"
    assert report.output["download_url"] == (
        "/api/reports/tasks/23/export?format=html&template_type=compliance"
    )
    assert report.output["file_name"] == "review_report_23.html"
    assert code_file.status == "success"
    assert code_file.output["download_url"] == "/api/code-files/41/download"
    assert code_file.output["file_name"] == "evidence.bin"
    assert report_checks == [(user.id, 23)]
    assert file_checks == [(user.id, 41)]
    assert checked_permissions[-2:] == ["report:export:html", "file:download"]


@pytest.mark.asyncio
async def test_fixed_download_tools_reject_path_injection_and_non_binary_file(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(service_module.rbac_service, "check_permission", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        service_module.code_file_service,
        "get_file",
        lambda *_args, **_kwargs: SimpleNamespace(file_name="main.py", is_binary=0),
    )
    executor = _executor(db, user, "run_rejected_downloads")

    injected = await executor.execute(
        ToolCall(
            "call_injected_download",
            "download_report",
            {"task_id": 23, "format": "html", "path": "/api/admin/users"},
            "{}",
        )
    )
    invalid_format = await executor.execute(
        ToolCall(
            "call_invalid_format",
            "download_report",
            {"task_id": 23, "format": "exe"},
            "{}",
        )
    )
    text_file = await executor.execute(
        ToolCall("call_text_download", "download_code_file", {"file_id": 9}, "{}")
    )

    assert injected.status == "error"
    assert "extra_forbidden" in injected.error
    assert invalid_format.status == "error"
    assert "literal_error" in invalid_format.error
    assert text_file.status == "error"
    assert "仅支持二进制文件" in text_file.error


@pytest.mark.asyncio
async def test_roundtable_tools_read_and_control_only_owned_session_after_approval(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    DiscussionBus._instance = None
    bus = DiscussionBus.instance()
    bus.create_session(
        session_id="disc_agent_owned",
        task_id=0,
        file_name="main.py",
        owner_user_id=user.id,
        max_rounds=2,
    )
    controls: list[tuple[str, dict[str, Any]]] = []
    bus.set_controller("disc_agent_owned", lambda action, payload: controls.append((action, payload)))
    executor = _executor(db, user, "run_roundtable_tools")
    schemas = {item["name"]: item for item in await executor.tool_schemas()}

    assert {
        "start_roundtable_discussion",
        "get_roundtable_discussion",
        "control_roundtable_discussion",
    } <= set(schemas)
    status = await executor.execute(
        ToolCall("call_discussion_get", "get_roundtable_discussion", {"session_id": "disc_agent_owned"}, "{}")
    )
    control_call = ToolCall(
        "call_discussion_pause",
        "control_roundtable_discussion",
        {"session_id": "disc_agent_owned", "action": "pause"},
        "{}",
    )
    paused = await executor.execute(control_call)

    assert status.status == "success"
    assert status.output["session_id"] == "disc_agent_owned"
    assert paused.status == "approval_required"
    assert controls == []

    completed = await executor.execute(control_call, approved=True)
    assert completed.status == "success"
    assert completed.output["accepted"] is True
    assert controls == [("pause", {"session_id": "disc_agent_owned"})]

    user_input_call = ToolCall(
        "call_discussion_user_input",
        "control_roundtable_discussion",
        {
            "session_id": "disc_agent_owned",
            "action": "user_input",
            "content": "请重点复核权限边界",
        },
        "{}",
    )
    assert (await executor.execute(user_input_call)).status == "approval_required"
    user_input = await executor.execute(user_input_call, approved=True)
    assert user_input.status == "success"
    assert user_input.output["session_id"] == "disc_agent_owned"
    assert bus.get_session("disc_agent_owned").status == "active"
    assert bus.get_session("disc_agent_owned").turns[-1].content == "请重点复核权限边界"
    assert controls[-1] == ("user_input", {"content": "请重点复核权限边界"})

    outsider = User(username="roundtable_outsider", password="x", role="user", status=1)
    db.add(outsider)
    db.commit()
    denied = await _executor(db, outsider, "run_roundtable_outsider").execute(
        ToolCall("call_discussion_denied", "get_roundtable_discussion", {"session_id": "disc_agent_owned"}, "{}")
    )
    assert denied.status == "error"
    assert "无权访问" in denied.error
    DiscussionBus._instance = None


@pytest.mark.asyncio
async def test_concluded_roundtable_user_input_starts_owned_continuation(
    db,
    monkeypatch,
) -> None:
    from app.api.v1 import discussion as discussion_api
    from app.api.v1 import ws_discussion

    user = _user(db)
    project = Project(
        user_id=user.id,
        project_name="continuation-project",
        language="python",
        status="active",
    )
    db.add(project)
    db.commit()
    code_file = CodeFile(
        project_id=project.id,
        file_name="main.py",
        file_path="main.py",
        language="python",
        content="def guarded():\n    return True\n",
        size_bytes=31,
        line_count=2,
        status="active",
    )
    db.add(code_file)
    db.commit()

    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(discussion_api.rule_service, "get_enabled_rules", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(discussion_api, "_gen_session_id", lambda: "disc_continued")
    launched = []
    monkeypatch.setattr(ws_discussion, "launch_pending_discussion", lambda pending: launched.append(pending))

    DiscussionBus._instance = None
    bus = DiscussionBus.instance()
    original = bus.create_session(
        session_id="disc_concluded",
        task_id=0,
        file_name=code_file.file_name,
        owner_user_id=user.id,
        max_rounds=2,
    )
    original.project_id = project.id
    original.file_id = code_file.id
    original.review_type = "security"
    original.origin_surface = "user"
    original.origin_session_key = "chat-original"
    bus.publish_turn(original.session_id, DiscussionTurn(
        turn_id=9,
        agent_code="orchestrator",
        agent_name="主持人",
        role="agent",
        content="📋 **讨论共识小结**\n\n上一轮认为权限边界安全。",
    ))
    original.report_task_id = 731
    bus.close_session(original.session_id)

    executor = PrismToolExecutor(
        db,
        user,
        surface="user",
        run_id="run-roundtable-continuation",
        session_key="chat-current",
        mcp_provider=EmptyMcp(),
    )
    call = ToolCall(
        "call-roundtable-continuation",
        "control_roundtable_discussion",
        {
            "session_id": original.session_id,
            "action": "user_input",
            "content": "重新核对管理员越权路径，并修正结论。",
        },
        "{}",
    )

    assert (await executor.execute(call)).status == "approval_required"
    completed = await executor.execute(call, approved=True)

    assert completed.status == "success"
    assert completed.output["session_id"] == "disc_continued"
    assert completed.output["continued_from_session_id"] == original.session_id
    assert completed.output["previous_report_task_id"] == 731
    assert completed.output["accepted"] is True
    assert original.status == "concluded"
    assert original.report_task_id == 731

    continuation = bus.get_session("disc_continued")
    assert continuation is not None
    assert continuation.status == "active"
    assert continuation.report_task_id == 0
    assert continuation.owner_user_id == user.id
    assert continuation.project_id == project.id
    assert continuation.file_id == code_file.id
    assert continuation.review_type == "security"
    assert continuation.origin_surface == "user"
    assert continuation.origin_session_key == "chat-original"
    assert continuation.continued_from_session_id == original.session_id

    assert len(launched) == 1
    pending = launched[0]
    assert pending.session_id == "disc_continued"
    assert pending.kwargs["project_id"] == project.id
    assert pending.kwargs["file_id"] == code_file.id
    assert pending.kwargs["review_type"] == "security"
    assert pending.kwargs["origin_surface"] == "user"
    assert pending.kwargs["origin_session_key"] == "chat-original"
    assert "上一轮认为权限边界安全" in pending.kwargs["continuation_context"]
    assert "重新核对管理员越权路径" in pending.kwargs["continuation_context"]

    ws_discussion._session_owners.pop("disc_continued", None)
    ws_discussion._owner_registered_at.pop("disc_continued", None)
    DiscussionBus._instance = None


@pytest.mark.asyncio
async def test_concluded_roundtable_continuation_rejects_non_owner(
    db,
    monkeypatch,
) -> None:
    owner = _user(db)
    outsider = User(username="roundtable_continuation_outsider", password="x", role="user", status=1)
    db.add(outsider)
    db.commit()
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())

    DiscussionBus._instance = None
    bus = DiscussionBus.instance()
    original = bus.create_session(
        session_id="disc_private_concluded",
        task_id=0,
        file_name="private.py",
        owner_user_id=owner.id,
    )
    original.project_id = 91
    original.file_id = 92
    original.review_type = "full"
    bus.close_session(original.session_id)

    executor = _executor(db, outsider, "run-roundtable-continuation-denied")
    call = ToolCall(
        "call-roundtable-continuation-denied",
        "control_roundtable_discussion",
        {
            "session_id": original.session_id,
            "action": "user_input",
            "content": "把结论改成安全",
        },
        "{}",
    )
    assert (await executor.execute(call)).status == "approval_required"
    denied = await executor.execute(call, approved=True)

    assert denied.status == "error"
    assert "无权控制" in denied.error
    assert list(bus._sessions) == [original.session_id]
    assert original.status == "concluded"
    DiscussionBus._instance = None


@pytest.mark.asyncio
async def test_unique_super_admin_can_read_and_control_other_users_roundtable(
    db,
    super_admin_user,
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    owner = _user(db)
    DiscussionBus._instance = None
    bus = DiscussionBus.instance()
    bus.create_session(
        session_id="disc_super_admin_control",
        task_id=0,
        file_name="main.py",
        owner_user_id=owner.id,
    )
    controls: list[str] = []
    bus.set_controller("disc_super_admin_control", lambda action, _payload: controls.append(action))
    executor = PrismToolExecutor(
        db,
        super_admin_user,
        surface="admin",
        run_id="run-super-roundtable",
        mcp_provider=EmptyMcp(),
    )

    status = await executor.execute(
        ToolCall("call-super-read", "get_roundtable_discussion", {"session_id": "disc_super_admin_control"}, "{}")
    )
    control = ToolCall(
        "call-super-stop",
        "control_roundtable_discussion",
        {"session_id": "disc_super_admin_control", "action": "stop"},
        "{}",
    )
    assert status.status == "success"
    assert (await executor.execute(control)).status == "approval_required"
    assert (await executor.execute(control, approved=True)).status == "success"
    assert controls == ["stop"]
    DiscussionBus._instance = None


@pytest.mark.asyncio
async def test_user_capability_reuses_current_identity_and_requires_write_approval(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        service_module.tool_gateway,
        "authorize",
        lambda *_args, **_kwargs: SimpleNamespace(decision=service_module.policy_engine.ALLOW, reason="allow"),
    )
    calls: list[tuple[int, str, dict[str, Any]]] = []

    async def fake_execute(actor, spec, params, *, request_id):
        calls.append((actor.id, spec.code, dict(params)))
        return {"capability": spec.code, "request_id": request_id, "data": {"updated": True}}

    monkeypatch.setattr(service_module.admin_capability_service, "execute_api", fake_execute)
    executor = _executor(db, user, "run_profile_update")
    call = ToolCall(
        "call_profile_update",
        "user_execute_capability",
        {"capability": "profile.update", "params": {"goals": "全量白盒审计"}},
        "{}",
    )

    paused = await executor.execute(call)
    assert paused.status == "approval_required"
    assert paused.danger is False
    assert calls == []
    approval = db.get(ApprovalItem, paused.approval_id)
    assert approval.action == "responses.user_execute_capability"

    completed = await executor.execute(call, approved=True)
    assert completed.status == "success"
    assert calls == [(user.id, "profile.update", {"goals": "全量白盒审计"})]
    ledger = db.query(AgentToolExecution).filter_by(run_id="run_profile_update").one()
    assert ledger.status == "success"
    assert db.get(ApprovalItem, paused.approval_id).status == "approved"


@pytest.mark.asyncio
async def test_critical_user_capability_requires_danger_approval_before_api_call(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(service_module.rbac_service, "check_permission", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        service_module.tool_gateway,
        "authorize",
        lambda *_args, **_kwargs: SimpleNamespace(decision=service_module.policy_engine.ALLOW, reason="allow"),
    )
    called = False

    async def fake_execute(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(service_module.admin_capability_service, "execute_api", fake_execute)
    result = await _executor(db, user, "run_project_delete").execute(
        ToolCall(
            "call_project_delete",
            "user_execute_capability",
            {"capability": "projects.delete", "params": {"project_id": 7}},
            "{}",
        )
    )

    assert CAPABILITY_BY_CODE["projects.delete"].risk == CRITICAL
    assert result.status == "approval_required"
    assert result.danger is True
    assert called is False
    approval = db.get(ApprovalItem, result.approval_id)
    assert approval is not None
    assert approval.risk_level == "critical"


@pytest.mark.asyncio
async def test_user_capability_permission_is_checked_before_real_api_call(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(service_module.rbac_service, "check_permission", lambda *_args, **_kwargs: False)
    called = False

    async def fake_execute(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(service_module.admin_capability_service, "execute_api", fake_execute)
    result = await _executor(db, user, "run_permission_denied").execute(
        ToolCall(
            "call_permission_denied",
            "user_execute_capability",
            {"capability": "projects.list", "params": {}},
            "{}",
        )
    )

    assert result.status == "error"
    assert "project:view" in result.error
    assert called is False


@pytest.mark.asyncio
async def test_unregistered_or_injected_user_capability_is_rejected(db, monkeypatch) -> None:
    user = _user(db)
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace())
    executor = _executor(db, user, "run_user_injection")

    unknown = await executor.execute(
        ToolCall(
            "call_unknown",
            "user_execute_capability",
            {"capability": "admin.users.delete", "params": {"user_id": 1}},
            "{}",
        )
    )
    injected = await executor.execute(
        ToolCall(
            "call_injected",
            "user_execute_capability",
            {
                "capability": "profile.get",
                "params": {},
                "path": "/api/admin/overview/system",
            },
            "{}",
        )
    )

    assert unknown.status == "error"
    assert "未注册" in unknown.error
    assert injected.status == "error"
    assert "不接受参数" in injected.error
