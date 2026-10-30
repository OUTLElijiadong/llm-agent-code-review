"""本地黑盒精确 HTTP 白名单(http_scope)在各调用方链路上的贯通回归。

4.1.15 起，本地黑盒(test_mode=blackbox 且无 remote_target_url)必须携带 1–20 条精确 GET 路径白名单，
服务端 ``sandbox_service.create_environment`` 会二次校验并拒绝缺失白名单的请求。
小菱 ``run_project_tests`` 工具、团队 ``test_verifier`` 成员以及授权渗透 detect 阶段的沙箱探测
原先都不传白名单，因此一律被服务端拒绝。

本文件不桩 ``create_environment``：参数校验、worker 选择与落库逻辑真实运行，
只桩掉源码打包与后台执行线程(即真正起容器/调 worker 的最底层)。
本机无 Docker/runsc，真实容器内的执行不在本文件验证范围内。
"""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from app.agents.base import AgentContext, AgentResult
from app.agents.orchestrator import get_request_orchestrator
from app.agents.tool_contracts import (
    FixedToolArgumentError,
    get_fixed_tool_description,
    get_fixed_tool_schema,
    validate_fixed_tool_arguments,
)
from app.core.permission_codes import PermissionCode
from app.models.agent_capability import SandboxEnvironment, SandboxWorker
from app.models.pentest import PentestEngagement
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import agent_mesh_dispatcher, pentest_service, sandbox_service

# 客户端只写必填的 path，其余字段由服务端按 SandboxHttpRequestScope 默认值补齐。
SCOPE = [
    {"path": "/"},
    {"path": "/about.php", "query": {"page": "1"}, "expected_statuses": [200]},
]
NORMALIZED_SCOPE = [
    {"method": "GET", "path": "/", "query": {}, "expected_statuses": []},
    {"method": "GET", "path": "/about.php", "query": {"page": "1"}, "expected_statuses": [200]},
]
ROOT_ONLY_SCOPE = [{"method": "GET", "path": "/", "query": {}, "expected_statuses": []}]


def _archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("index.php", "<?php echo 'ok';\n")
    return output.getvalue()


@pytest.fixture
def local_sandbox(db, monkeypatch):
    """普通用户 + PHP 项目 + 健康 worker；只桩掉源码打包与后台执行线程。"""
    owner = User(username="scope-plumbing-owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="scope-plumbing", language="php", status="active")
    role = Role(code="user", name="黑盒白名单夹具", status="active")
    db.add_all([project, role])
    db.flush()
    db.add(UserRole(user_id=owner.id, role_id=role.id))
    for code in (PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW):
        permission = Permission(code=code, name=code, module="local", type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.add(
        SandboxWorker(
            code="scope-plumbing-worker",
            name="本地桩执行器",
            worker_type="local",
            transport="unix",
            endpoint="/run/prism-sandbox/never-used.sock",
            supported_languages_json='["php","python"]',
            supported_modes_json='["whitebox","blackbox","combined","deploy"]',
            runtime="runsc",
            status="healthy",
            enabled=1,
            max_concurrency=1,
        )
    )
    db.commit()
    started: list[tuple] = []
    monkeypatch.setattr(
        sandbox_service.project_source_service,
        "build_source_archive",
        lambda *_args, **_kwargs: (_archive(), "source.zip"),
    )
    # create_environment 会起后台线程跑 _execute_environment；这里替换为只记录的空操作。
    monkeypatch.setattr(sandbox_service, "_execute_environment", lambda *args, **_kwargs: started.append(args))
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)
    return SimpleNamespace(owner=owner, project=project, started=started)


def _run_tool(db, local_sandbox, **arguments) -> AgentResult:
    """走与小菱固定工具相同的入口：契约校验 → Orchestrator → TestVerifierAgent → create_environment。"""
    orchestrator = get_request_orchestrator(db, user=local_sandbox.owner)
    payload = {"project_id": local_sandbox.project.id, "language": "php", **arguments}
    return orchestrator.invoke_tool(
        "run_project_tests",
        payload,
        AgentContext(user_id=int(local_sandbox.owner.id), project_id=int(local_sandbox.project.id)),
    )


# ── 工具契约与模型可见的 JSON schema ──


def test_run_project_tests_contract_accepts_http_scope_for_local_blackbox():
    arguments = validate_fixed_tool_arguments(
        "run_project_tests",
        {"project_id": 1, "language": "php", "test_mode": "blackbox", "http_scope": SCOPE},
    )

    assert arguments["test_mode"] == "blackbox"
    assert arguments["http_scope"] == SCOPE


@pytest.mark.parametrize(
    ("label", "http_scope", "field", "error_type"),
    [
        ("通配符路径", [{"path": "/admin/*"}], "http_scope.0.path", "value_error"),
        ("非 GET 方法", [{"method": "POST", "path": "/"}], "http_scope.0.method", "literal_error"),
        ("多余字段", [{"path": "/", "headers": {"X": "1"}}], "http_scope.0.headers", "extra_forbidden"),
        ("超过 20 条", [{"path": f"/p{index}"} for index in range(21)], "http_scope", "too_long"),
    ],
)
def test_run_project_tests_contract_rejects_invalid_http_scope_entries(label, http_scope, field, error_type):
    """复用 SandboxHttpRequestScope 的同一套校验：模型写错白名单时在工具契约层就被精确指出。"""
    with pytest.raises(FixedToolArgumentError) as raised:
        validate_fixed_tool_arguments(
            "run_project_tests",
            {"project_id": 1, "language": "php", "test_mode": "blackbox", "http_scope": http_scope},
        )

    issues = raised.value.issues
    assert {"field": field, "type": error_type} in [
        {"field": item["field"], "type": item["type"]} for item in issues
    ], f"{label}应在字段 {field} 上以 {error_type} 被契约拒绝: {issues}"


def test_run_project_tests_schema_tells_the_model_how_to_run_blackbox():
    schema = get_fixed_tool_schema("run_project_tests")
    description = get_fixed_tool_description("run_project_tests")
    scope_schema = schema["properties"]["http_scope"]

    assert any(item.get("maxItems") == 20 for item in scope_schema["anyOf"])
    assert "SandboxHttpRequestScope" in schema["$defs"]
    assert "本地黑盒" in scope_schema["description"]
    assert "combined" in description and "run_full_project_validation" in description
    assert "http_scope" in description


# ── 小菱 run_project_tests 工具链路(真实 create_environment) ──


def test_service_still_requires_scope_for_local_blackbox(db, local_sandbox):
    """前提锚点：服务端对缺失白名单的本地黑盒失败关闭；贯通修复只能补传白名单，不能放宽这条红线。"""
    with pytest.raises(sandbox_service.ValidationError) as raised:
        sandbox_service.create_environment(
            db,
            local_sandbox.owner,
            {
                "project_id": local_sandbox.project.id,
                "purpose": "test",
                "language": "php",
                "test_mode": "blackbox",
            },
        )

    assert raised.value.code == 40001
    assert "精确 HTTP 路径白名单" in raised.value.message
    assert db.query(SandboxEnvironment).count() == 0
    assert local_sandbox.started == []


def test_xiaoling_local_blackbox_with_scope_creates_enforced_environment(db, local_sandbox):
    result = _run_tool(db, local_sandbox, test_mode="blackbox", http_scope=SCOPE)

    assert result.success, result.error
    environment = db.query(SandboxEnvironment).one()
    config = json.loads(environment.agent_config_json)
    assert (environment.purpose, environment.test_mode) == ("test", "blackbox")
    assert config["http_scope_enforced"] is True
    assert config["http_scope"] == NORMALIZED_SCOPE
    assert len(local_sandbox.started) == 1


def test_xiaoling_local_blackbox_without_scope_gets_self_healing_error(db, local_sandbox):
    result = _run_tool(db, local_sandbox, test_mode="blackbox")

    assert result.success is False
    assert db.query(SandboxEnvironment).count() == 0
    error = result.error or ""
    for needle in ("http_scope", "combined", "run_full_project_validation"):
        assert needle in error, f"错误信息缺少自愈指引 {needle!r}: {error}"


@pytest.mark.parametrize("test_mode", ["whitebox", "combined"])
def test_http_scope_is_rejected_with_hint_outside_local_blackbox(db, local_sandbox, test_mode):
    result = _run_tool(db, local_sandbox, test_mode=test_mode, http_scope=SCOPE)

    assert result.success is False
    assert db.query(SandboxEnvironment).count() == 0
    assert "仅适用于本地黑盒" in (result.error or ""), result.error


def test_empty_scope_is_rejected_for_every_mode_at_the_contract_layer():
    """空列表既不能让本地黑盒通过，也不能带进其它模式。"""
    for arguments in (
        {"test_mode": "blackbox", "http_scope": []},
        {"test_mode": "combined", "http_scope": []},
        {"test_mode": "blackbox", "remote_target_url": "https://example.test/", "http_scope": SCOPE},
    ):
        with pytest.raises(FixedToolArgumentError):
            validate_fixed_tool_arguments("run_project_tests", {"project_id": 1, "language": "php", **arguments})


def test_remote_blackbox_without_scope_is_still_accepted_by_the_contract():
    """远程目标黑盒走授权探测，不需要也不接受本地白名单(远程授权逻辑本身不在此修改)。"""
    arguments = validate_fixed_tool_arguments(
        "run_project_tests",
        {
            "project_id": 1,
            "language": "php",
            "test_mode": "blackbox",
            "remote_target_url": "https://authorized.example.test/health",
        },
    )

    assert "http_scope" not in arguments


@pytest.mark.parametrize("test_mode", ["whitebox", "combined"])
def test_non_blackbox_modes_keep_working_without_scope(db, local_sandbox, test_mode):
    """回归保护：白盒与组合测试不需要也不得携带白名单。"""
    result = _run_tool(db, local_sandbox, test_mode=test_mode)

    assert result.success, result.error
    config = json.loads(db.query(SandboxEnvironment).one().agent_config_json)
    assert config["http_scope_enforced"] is False
    assert config["http_scope"] is None


# ── 团队 test_verifier 成员(Agent Mesh 分发) ──


def _team_message(**payload):
    return {
        "user_id": 7,
        "trace_id": "team-scope-trace",
        "message_id": "team-scope-1",
        "payload": {"operation": "run_project_tests", "project_id": 31, "language": "php", **payload},
        "context": {"team_id": 9, "task_id": 2, "lease_token": "lease-scope"},
    }


def _fake_orchestrator(monkeypatch, calls):
    class FakeOrchestrator:
        def run_project_tests(self, **kwargs):
            calls.append(kwargs)
            return AgentResult(
                success=True,
                data={"public_id": "sbx_scope", "status": "succeeded", "test_mode": kwargs.get("test_mode")},
            )

    monkeypatch.setattr("app.agents.orchestrator.get_request_orchestrator", lambda *_a, **_k: FakeOrchestrator())
    monkeypatch.setattr(
        "app.services.agent_team_service.handoff_dependency_runtime_resources",
        lambda *_a, **_k: [],
    )


def test_team_test_verifier_forwards_http_scope_to_run_project_tests(monkeypatch):
    calls: list[dict] = []
    _fake_orchestrator(monkeypatch, calls)

    result = agent_mesh_dispatcher._runtime_handler(
        object(),
        SimpleNamespace(id=7, role="super_admin"),
        "test_verifier",
        _team_message(test_mode="blackbox", http_scope=SCOPE),
    )

    assert result["status"] == "completed"
    assert calls[0]["test_mode"] == "blackbox"
    assert calls[0].get("http_scope") == SCOPE


def test_team_test_verifier_local_blackbox_reaches_real_create_environment(db, local_sandbox, monkeypatch):
    """不桩 Orchestrator：Mesh 分发 → 请求级 Orchestrator → TestVerifierAgent → 真实 create_environment。"""
    created: list[AgentResult] = []

    def stop_at_created(_db, _user, result, **_kwargs):
        # 沙箱执行线程已被替换为空操作，不等待终态，直接把入队结果交还给断言。
        created.append(result)
        return {"status": "completed", "summary": "stub", "evidence": [], "artifacts": [], "errors": [],
                "next_action": None}

    monkeypatch.setattr(agent_mesh_dispatcher, "_wait_for_sandbox_terminal", stop_at_created)
    message = _team_message(test_mode="blackbox", http_scope=SCOPE, project_id=int(local_sandbox.project.id))
    message["context"] = {}  # 不带团队租约：本用例只验证白名单贯通，不涉及团队账本

    result = agent_mesh_dispatcher._runtime_handler(db, local_sandbox.owner, "test_verifier", message)

    assert result["status"] == "completed"
    assert created and created[0].success, created[0].error if created else "未调用 create_environment"
    config = json.loads(db.query(SandboxEnvironment).one().agent_config_json)
    assert config["http_scope_enforced"] is True
    assert config["http_scope"] == NORMALIZED_SCOPE


def test_team_test_verifier_without_scope_keeps_previous_call_shape(monkeypatch):
    """回归保护：未声明白名单的组合测试沿用原参数集合，不额外传 http_scope。"""
    calls: list[dict] = []
    _fake_orchestrator(monkeypatch, calls)

    result = agent_mesh_dispatcher._runtime_handler(
        object(),
        SimpleNamespace(id=7, role="super_admin"),
        "test_verifier",
        _team_message(test_mode="combined"),
    )

    assert result["status"] == "completed"
    assert "http_scope" not in calls[0]


# ── 授权渗透 detect 阶段的沙箱探测(真实 create_environment) ──


def test_pentest_detect_probe_sends_minimal_root_scope_and_creates_environment(db, local_sandbox, monkeypatch):
    engagement = PentestEngagement(
        public_id="pt-scope-plumbing01",
        user_id=int(local_sandbox.owner.id),
        project_id=int(local_sandbox.project.id),
        target_type="web",
        scope_json="{}",
        status="running",
    )
    db.add(engagement)
    db.commit()
    # 不等待沙箱执行(本文件不跑容器)；创建成功后立即进入收尾，回收动作桩掉以免触碰 worker。
    monkeypatch.setattr(pentest_service, "_PROBE_WAIT_SECONDS", 0)
    monkeypatch.setattr(pentest_service, "_best_effort_stop_env", lambda *_args, **_kwargs: None)

    probe = pentest_service._run_sandbox_probe(db, engagement)

    assert probe["attempted"] is True, probe.get("reason")
    environment = db.query(SandboxEnvironment).one()
    config = json.loads(environment.agent_config_json)
    assert (environment.purpose, environment.test_mode) == ("test", "blackbox")
    assert config["http_scope_enforced"] is True
    # 委托范围里没有任何路径声明，探测只允许沙箱回环上的 GET /，不得凭空扩展别的路径。
    assert config["http_scope"] == ROOT_ONLY_SCOPE
    assert probe["http_scope"] == ROOT_ONLY_SCOPE


def test_pentest_probe_success_is_reported_as_bounded_not_complete_dynamic_coverage(db, local_sandbox):
    """只探测了精确白名单里的 GET / 时，渗透覆盖状态不得被标成完整的动态覆盖。"""
    engagement = PentestEngagement(
        public_id="pt-scope-coverage01",
        user_id=int(local_sandbox.owner.id),
        project_id=int(local_sandbox.project.id),
        target_type="web",
        scope_json="{}",
        status="completed",
    )
    db.add(engagement)
    db.flush()
    for phase_key in pentest_service.PHASE_KEYS:
        phase = pentest_service._ensure_phase_run(db, engagement.id, phase_key)
        phase.status = "success"
        if phase_key == "detect":
            phase.output_json = pentest_service._dump({
                "probe": {
                    "attempted": True,
                    "env_status": "succeeded",
                    "http_scope": ROOT_ONLY_SCOPE,
                },
            })
    db.commit()

    coverage = pentest_service._pentest_coverage(db, engagement.id)

    assert coverage["coverage_status"] == "partial"
    assert any("白名单" in gap and "GET /" in gap for gap in coverage["coverage_gaps"]), coverage["coverage_gaps"]


# ── 其余入口：MCP create_test / 服务端报错对所有调用方可自愈 ──


def test_service_error_for_missing_scope_tells_every_caller_how_to_recover(db, local_sandbox):
    """团队成员、MCP 等不经过工具契约的入口只能看到服务端报错，必须告诉它们怎么补或怎么换。"""
    with pytest.raises(sandbox_service.ValidationError) as raised:
        sandbox_service.create_environment(
            db,
            local_sandbox.owner,
            {"project_id": local_sandbox.project.id, "purpose": "test", "language": "php", "test_mode": "blackbox"},
        )

    message = raised.value.message
    assert "精确 HTTP 路径白名单" in message
    assert "1–20" in message and "combined" in message, message


def test_managed_mcp_create_test_blackbox_is_rejected_with_a_way_out(db, local_sandbox, monkeypatch):
    """prism-sandbox.create_test 的 schema 没有 http_scope，blackbox 在服务端必被拒；报错要指向可行替代。"""
    from app.services import managed_mcp_adapter

    monkeypatch.setattr(managed_mcp_adapter, "managed_kind_ready", lambda *_args: True)

    with pytest.raises(sandbox_service.ValidationError) as raised:
        managed_mcp_adapter.call_managed_tool(
            db,
            local_sandbox.owner,
            "prism-sandbox",
            "create_test",
            {"project_id": local_sandbox.project.id, "language": "php", "test_mode": "blackbox"},
        )

    assert db.query(SandboxEnvironment).count() == 0
    assert "combined" in raised.value.message, raised.value.message
