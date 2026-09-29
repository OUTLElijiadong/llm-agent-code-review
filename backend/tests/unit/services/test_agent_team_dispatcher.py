"""动态子 Agent 团队调度消息契约测试。"""

import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agents.base import AgentResult
from app.models.agent_team import AgentTeam, AgentTeamEvent, AgentTeamMember, AgentTeamTask
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_mesh_dispatcher, agent_team_dispatcher, agent_team_service, published_agent_tools


@pytest.mark.parametrize("change", [
    "permission_before", "permission_during", "disabled_before", "disabled_during",
])
def test_team_worker_discards_task_after_owner_loses_access(
    monkeypatch, db, change,
):
    user = User(username=f"revoke-{change}", password="x", role="user", status=1)
    db.add(user)
    db.flush()
    team = AgentTeam(
        user_id=user.id, surface="user", session_key="revoke-session", title="撤权边界",
        objective="只在当前权限内审查", status="running", trace_id="revoke-trace",
    )
    db.add(team)
    db.flush()
    member = AgentTeamMember(
        team_id=team.id, member_key="reviewer", display_name="审查 Agent",
        address="agent:code_reviewer", kind="builtin", role="verifier", status="running",
    )
    db.add(member)
    db.flush()
    task = AgentTeamTask(
        team_id=team.id, member_id=member.id, task_key="review", title="审查代码",
        instructions="审查给定片段", status="running", attempt_count=1, max_attempts=2,
        lease_token="active-lease", lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        input_json='{"code":"print(1)"}',
    )
    db.add(task)
    db.commit()
    claimed = {
        "task_id": task.id, "member_id": member.id, "address": member.address,
        "lease_token": "active-lease", "attempt_count": 1, "input": {"code": "print(1)"},
    }
    permission = {"allowed": change != "permission_before"}
    if change == "disabled_before":
        user.status = 0
        db.commit()
    calls = []
    monkeypatch.setattr(agent_team_dispatcher, "SessionLocal", lambda: db)
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_args: permission["allowed"])

    def handler(*_args, **_kwargs):
        calls.append("model invoked")
        if change == "permission_during":
            permission["allowed"] = False
        elif change == "disabled_during":
            db.query(User).filter(User.id == user.id).update({User.status: 0})
            db.commit()
        return "审查 Agent", {"status": "completed", "summary": "旧权限下的结果"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handler)
    outcome = agent_team_dispatcher._execute_claimed(team.id, claimed)

    assert outcome == {"success": False}
    assert calls == (["model invoked"] if change.endswith("during") else [])
    db.expire_all()
    saved = db.get(AgentTeamTask, task.id)
    assert saved.status == "failed"
    assert "旧权限下的结果" not in saved.result_json
    assert db.query(AgentTeamEvent).filter_by(team_id=team.id, event_type="task.failed").count() == 1


def test_task_message_preserves_input_and_adds_team_handoff_context():
    team = SimpleNamespace(
        id=9,
        user_id=7,
        surface="user",
        session_key="session-a1",
        trace_id="team-trace",
    )
    claimed = {
        "task_id": 12,
        "task_key": "verify",
        "member_id": 3,
        "address": "agent:code_reviewer",
        "attempt_count": 2,
        "lease_token": "lease-12-2",
        "request_message_id": "request-12-2",
        "title": "独立复核",
        "instructions": "复核前置结果",
        "input": {"code": "print('ok')", "source_revision_id": 81},
        "dependency_context": {"read": {"status": "completed", "result": {"summary": "读取完成"}}},
        "member_snapshot": {
            "template_id": 21,
            "version_id": 22,
            "release_id": 23,
            "package_checksum": "p" * 64,
            "template_checksum": "v" * 64,
        },
    }

    message = agent_team_dispatcher._task_message(team, claimed)

    assert message["message_id"] == "request-12-2"
    assert message["sent_from"] == "session:user:session-a1"
    assert message["send_to"] == "agent:code_reviewer"
    assert message["payload"]["code"] == "print('ok')"
    assert message["payload"]["dependency_context"]["read"]["result"]["summary"] == "读取完成"
    assert message["payload"]["_agent_team"]["member_snapshot"]["release_id"] == 23
    assert message["context"] == {
        "team_id": 9,
        "agent_team_task_id": 12,
        "member_id": 3,
        "source_revision_id": 81,
        "run_id": "team-trace",
        "attempt": 2,
        "lease_token": "lease-12-2",
    }


def test_team_context_does_not_turn_internal_id_into_review_task(monkeypatch):
    calls = []
    sentinel = SimpleNamespace(scan_task=lambda task_id, **_kwargs: calls.append(task_id))
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(security_sentinel=sentinel),
    )

    result = agent_mesh_dispatcher._runtime_handler(
        SimpleNamespace(),
        SimpleNamespace(id=7),
        "security_sentinel",
        {
            "user_id": 7,
            "payload": {"instructions": "只读检查服务器安全"},
            "context": {"team_id": 9, "agent_team_task_id": 31},
        },
    )

    assert calls == []
    assert result["status"] == "needs_clarification"
    assert result["next_action"]["provide_fields"] == ["file_id|task_id|project_id"]


def test_operations_team_executes_readonly_action_with_audited_request_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.ops_service.execute",
        lambda _db, _user, **kwargs: calls.append(kwargs)
        or {"status": "success", "request_id": kwargs["request_id"], "result": {"ok": True}},
    )

    result = agent_mesh_dispatcher._runtime_handler(
        SimpleNamespace(),
        SimpleNamespace(id=1),
        "operations",
        {
            "user_id": 1,
            "payload": {"action": "status", "params": {}},
            "context": {"team_id": 13, "agent_team_task_id": 41, "attempt": 1},
        },
    )

    assert result["status"] == "completed"
    assert calls[0]["request_id"] == "team-13-task-41-attempt-1"
    assert calls[0]["source"] == "agent_team"


def test_operations_team_rejects_write_action_without_executing(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.ops_service.execute", lambda *_args, **kwargs: calls.append(kwargs))

    result = agent_mesh_dispatcher._runtime_handler(
        SimpleNamespace(),
        SimpleNamespace(id=1),
        "operations",
        {
            "user_id": 1,
            "payload": {"action": "restart_service", "params": {"service": "backend"}},
            "context": {"team_id": 13, "agent_team_task_id": 41, "attempt": 1},
        },
    )

    assert calls == []
    assert result["status"] == "approval_required"
    assert result["next_action"]["use_main_xiaoling"] is True


def test_project_analyzer_can_inspect_project_without_source_paths(monkeypatch):
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_args: True)
    detail = AgentResult(success=True, data={"id": 153, "source_mode": "audit_archive"})
    files = AgentResult(success=True, data={"total": 0, "items": []})
    orch = SimpleNamespace(
        get_project_detail=lambda *args, **kwargs: detail,
        file_mgr=SimpleNamespace(list_files=lambda *args, **kwargs: files),
    )
    monkeypatch.setattr("app.agents.orchestrator.get_request_orchestrator", lambda *args, **kwargs: orch)

    result = agent_mesh_dispatcher._runtime_handler(
        SimpleNamespace(),
        SimpleNamespace(id=7),
        "project_analyzer",
        {
            "user_id": 7,
            "payload": {"operation": "inspect_project", "project_id": 153},
            "context": {},
        },
    )

    assert result["status"] == "completed"
    assert [item["source"] for item in result["evidence"]] == ["project_detail", "project_files"]


_TEAM_READ_PERMISSION_CASES = [
    pytest.param(
        "project_manager", {"operation": "list"},
        ("project:view",), "list_projects", id="project-list",
    ),
    pytest.param(
        "project_manager", {"operation": "get", "project_id": 153},
        ("project:view",), "get_project_detail", id="project-detail",
    ),
    pytest.param(
        "code_file_manager", {"operation": "list", "project_id": 153},
        ("file:view",), "list_files", id="file-list",
    ),
    pytest.param(
        "code_file_manager", {"operation": "get", "file_id": 91},
        ("file:view",), "get_file", id="file-content",
    ),
    pytest.param(
        "project_analyzer", {"operation": "inspect_project", "project_id": 153},
        ("project:view", "file:view"), "list_files", id="project-inspection",
    ),
    pytest.param(
        "review_orchestrator", {"operation": "list", "project_id": 153},
        ("review:view",), "list_tasks", id="review-list",
    ),
    pytest.param(
        "review_orchestrator", {"operation": "get", "task_id": 29},
        ("review:view",), "get_task_detail", id="review-detail",
    ),
    pytest.param(
        "review_orchestrator", {"operation": "issues", "task_id": 29},
        ("issue:view",), "list_issues", id="review-issues",
    ),
    pytest.param(
        "test_verifier", {"operation": "inspect_existing_results", "project_id": 153},
        ("review:view",), "list_tasks", id="existing-review-results",
    ),
    pytest.param(
        "reporter", {"operation": "list", "project_id": 153},
        ("report:view",), "list_reports", id="report-list",
    ),
    pytest.param(
        "reporter", {"operation": "get", "task_id": 29},
        ("report:view",), "get_report_detail", id="report-detail",
    ),
    pytest.param(
        "rule_manager", {"operation": "list"},
        ("rule:view",), "list_rules", id="rule-list",
    ),
]


def _mesh_permission_user(db, permission_codes):
    user = User(username="mesh-reader", password="x", role="reviewer", status=1)
    role = Role(name="审查员", code="reviewer", status="active", is_builtin=1)
    db.add_all([user, role])
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    for code in permission_codes:
        permission = Permission(code=code, name=code, module=code.split(":", 1)[0], type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()
    return user


def _mesh_read_orchestrator():
    from unittest.mock import Mock

    completed = AgentResult(success=True, data={"total": 0, "items": []})
    return SimpleNamespace(
        get_project_detail=Mock(return_value=completed),
        list_projects=Mock(return_value=completed),
        file_mgr=SimpleNamespace(
            list_files=Mock(return_value=completed),
            get_file=Mock(return_value=completed),
        ),
        review_orch=SimpleNamespace(
            list_tasks=Mock(return_value=completed),
            get_task_detail=Mock(return_value=completed),
            list_issues=Mock(return_value=completed),
        ),
        reporter=SimpleNamespace(
            list_reports=Mock(return_value=completed),
            get_report_detail=Mock(return_value=completed),
        ),
        rule_mgr=SimpleNamespace(list_rules=Mock(return_value=completed)),
    )


@pytest.mark.parametrize("code,payload,required,expected_call", _TEAM_READ_PERMISSION_CASES)
def test_team_read_agents_block_when_global_read_permission_is_revoked(
    monkeypatch, db, code, payload, required, expected_call,
):
    granted = set(required) - {required[-1]}
    user = _mesh_permission_user(db, granted)
    orchestrator = _mesh_read_orchestrator()
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: orchestrator,
    )

    result = agent_mesh_dispatcher._runtime_handler(
        db,
        user,
        code,
        {"user_id": user.id, "payload": payload, "context": {"team_id": 8}},
        trusted_team_execution=True,
    )

    assert result["status"] == "blocked"
    assert result["errors"][0]["code"] == "insufficient_permission"
    assert required[-1] in result["errors"][0]["permissions"]
    for target in (orchestrator, orchestrator.file_mgr, orchestrator.review_orch,
                   orchestrator.reporter, orchestrator.rule_mgr):
        call = getattr(target, expected_call, None)
        if call is not None:
            assert not call.called
    if code == "project_analyzer":
        assert not orchestrator.get_project_detail.called


@pytest.mark.parametrize("code,payload,required,expected_call", _TEAM_READ_PERMISSION_CASES)
def test_team_read_agents_keep_authorized_path_when_permissions_exist(
    monkeypatch, db, code, payload, required, expected_call,
):
    user = _mesh_permission_user(db, required)
    orchestrator = _mesh_read_orchestrator()
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: orchestrator,
    )

    result = agent_mesh_dispatcher._runtime_handler(
        db,
        user,
        code,
        {"user_id": user.id, "payload": payload, "context": {"team_id": 8}},
        trusted_team_execution=True,
    )

    assert result["status"] == "completed"
    if expected_call in {"list_files", "get_file"}:
        assert getattr(orchestrator.file_mgr, expected_call).called
    elif expected_call in {"list_tasks", "get_task_detail", "list_issues"}:
        assert getattr(orchestrator.review_orch, expected_call).called
    elif expected_call in {"list_reports", "get_report_detail"}:
        assert getattr(orchestrator.reporter, expected_call).called
    elif expected_call == "list_rules":
        assert orchestrator.rule_mgr.list_rules.called
    else:
        assert getattr(orchestrator, expected_call).called
    if code == "project_analyzer":
        assert orchestrator.get_project_detail.called
        assert orchestrator.file_mgr.list_files.called


@pytest.mark.parametrize("code,payload,required,_expected_call", _TEAM_READ_PERMISSION_CASES)
def test_team_creation_rejects_member_without_the_read_permission(
    db, code, payload, required, _expected_call,
):
    from app.schemas.agent_team import AgentTeamTaskIn

    granted = set(required) - {required[-1]}
    user = _mesh_permission_user(db, granted)
    task_input_data = dict(payload)
    if task_input_data.get("project_id"):
        project = Project(user_id=user.id, project_name="Mesh permission scope", status="active")
        db.add(project)
        db.flush()
        task_input_data["project_id"] = project.id
    task_input = AgentTeamTaskIn.model_validate({
        "task_key": "read-check",
        "member_key": "reader",
        "title": "读取权限复核",
        "instructions": "按请求读取当前任务所需内容",
        "input": task_input_data,
    })

    with pytest.raises(agent_team_service.AgentTeamAccessError, match=required[-1]):
        agent_team_service._validate_task_scope(db, user, task_input, f"agent:{code}")


def test_team_run_review_rechecks_review_start_permission_at_dispatch(monkeypatch, db):
    from unittest.mock import Mock

    user = _mesh_permission_user(db, {"review:view"})
    run_review = Mock()
    monkeypatch.setattr("app.services.agent_team_review.run_team_review", run_review)

    result = agent_mesh_dispatcher._runtime_handler(
        db,
        user,
        "review_orchestrator",
        {
            "user_id": user.id,
            "payload": {"operation": "run_review", "project_id": 153, "review_type": "full"},
            "context": {"team_id": 8, "agent_team_task_id": 12, "lease_token": "valid-lease"},
        },
        trusted_team_execution=True,
    )

    assert result["status"] == "blocked"
    assert result["errors"][0]["permissions"] == ["review:start"]
    run_review.assert_not_called()


def test_test_verifier_can_inspect_existing_results_without_running_tests(monkeypatch):
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_args: True)
    list_tasks = AgentResult(success=True, data={"total": 2, "items": [{"id": 137, "status": "failed"}]})
    orch = SimpleNamespace(review_orch=SimpleNamespace(list_tasks=lambda *args, **kwargs: list_tasks))
    monkeypatch.setattr("app.agents.orchestrator.get_request_orchestrator", lambda *args, **kwargs: orch)

    result = agent_mesh_dispatcher._runtime_handler(
        SimpleNamespace(),
        SimpleNamespace(id=7),
        "test_verifier",
        {
            "user_id": 7,
            "payload": {"operation": "inspect_existing_results", "project_id": 153},
            "context": {},
        },
    )

    assert result["status"] == "completed"
    assert result["evidence"][0]["data"]["items"][0]["id"] == 137


def test_readonly_instructions_fail_closed_when_model_omits_operation(monkeypatch):
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_args: True)
    list_tasks = AgentResult(success=True, data={"total": 5, "items": []})
    orch = SimpleNamespace(review_orch=SimpleNamespace(list_tasks=lambda *args, **kwargs: list_tasks))
    monkeypatch.setattr("app.agents.orchestrator.get_request_orchestrator", lambda *args, **kwargs: orch)

    result = agent_mesh_dispatcher._runtime_handler(
        SimpleNamespace(),
        SimpleNamespace(id=7),
        "test_verifier",
        {
            "user_id": 7,
            "payload": {
                "project_id": 153,
                "language": "python",
                "instructions": "只读核对已有审查任务，严禁运行新测试或创建沙箱",
            },
            "context": {},
        },
    )

    assert result["status"] == "completed"
    assert result["summary"] == "历史测试结果核验已完成"


def test_reporter_prefers_team_dependency_context_over_internal_task_id(monkeypatch):
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    result = agent_mesh_dispatcher._runtime_handler(
        SimpleNamespace(),
        SimpleNamespace(id=7),
        "reporter",
        {
            "user_id": 7,
            "payload": {"dependency_context": {"facts": {
                "status": "completed", "result": {"status": "completed", "summary": "已核对项目事实"},
            }}},
            "context": {"task_id": 999},
        },
    )
    assert result["status"] == "completed"
    assert "已核对" in result["summary"]


def test_dispatch_once_consumes_persistent_queue_and_records_completion(monkeypatch, db):
    user = User(username="dispatch-owner", password="x", email="dispatch@example.test", role="user", status=1)
    db.add(user)
    db.commit()
    payload = AgentTeamCreateIn.model_validate(
        {
            "surface": "user",
            "session_id": "session-dispatch",
            "title": "队列执行",
            "objective": "验证真实 Handler 调度",
            "members": [
                {
                    "member_key": "reader",
                    "display_name": "读取 Agent",
                    "address": "agent:project_analyzer",
                    "role": "worker",
                },
                {
                    "member_key": "reviewer",
                    "display_name": "验证 Agent",
                    "address": "agent:code_reviewer",
                    "role": "verifier",
                },
            ],
            "tasks": [
                {
                    "task_key": "read",
                    "member_key": "reader",
                    "title": "读取项目",
                    "instructions": "读取项目",
                    "input": {"folder_name": "demo", "file_names": ["main.py"]},
                },
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "验证结果",
                    "instructions": "复核读取结果",
                    "depends_on": ["read"],
                },
            ],
        }
    )
    created = agent_team_service.create_team_from_xiaoling(db, user, payload)
    monkeypatch.setattr(agent_team_dispatcher, "SessionLocal", lambda: db)
    monkeypatch.setattr(agent_team_dispatcher.settings, "agent_team_enabled", True)
    monkeypatch.setattr(agent_team_dispatcher.rbac_service, "check_permission", lambda *_args: True)

    class ImmediateExecutor:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args):
            future = Future()
            try:
                future.set_result(fn(*args))
            except Exception as exc:  # pragma: no cover - Future 转发异常
                future.set_exception(exc)
            return future

    monkeypatch.setattr(agent_team_dispatcher, "ThreadPoolExecutor", ImmediateExecutor)

    def fake_handle(_db, _user, address, message, *, trusted_team_execution=False):
        assert trusted_team_execution is True
        assert message["sent_from"] == "session:user:session-dispatch"
        if address == "agent:project_analyzer":
            assert message["payload"]["file_names"] == ["main.py"]
        else:
            assert address == "agent:code_reviewer"
            assert message["payload"]["dependency_context"]["read"]["status"] == "completed"
        return None, {"status": "completed", "summary": "项目读取完成", "evidence": [{"files": 1}]}

    monkeypatch.setattr("app.services.agent_mesh_dispatcher._handle", fake_handle)
    stats = agent_team_dispatcher.dispatch_once(limit=5)

    assert stats["claimed"] == 2
    assert stats["completed"] == 2
    assert agent_team_service.get_team(db, user, created["team_id"])["status"] == "completed"


def test_dispatch_once_runs_three_independent_children_concurrently(monkeypatch, db):
    user = User(username="parallel-owner", password="x", email="parallel@example.test", role="user", status=1)
    db.add(user)
    db.commit()
    payload = AgentTeamCreateIn.model_validate(
        {
            "surface": "user",
            "session_id": "session-parallel",
            "title": "真实并发",
            "objective": "同时执行三个独立子任务",
            "max_active_children": 3,
            "members": [
                {
                    "member_key": "reader",
                    "display_name": "读取 Agent",
                    "address": "agent:project_analyzer",
                    "role": "worker",
                },
                {
                    "member_key": "reviewer",
                    "display_name": "验证 Agent",
                    "address": "agent:code_reviewer",
                    "role": "verifier",
                },
            ],
            "tasks": [
                {
                    "task_key": f"read-{index}",
                    "member_key": "reader",
                    "title": f"读取 {index}",
                    "instructions": "并发读取",
                    "input": {"folder_name": "demo", "file_names": [f"{index}.py"]},
                }
                for index in range(3)
            ]
            + [
                {
                    "task_key": "verify",
                    "member_key": "reviewer",
                    "title": "验证",
                    "instructions": "验证所有并发结果",
                    "depends_on": [f"read-{index}" for index in range(3)],
                }
            ],
        }
    )
    agent_team_service.create_team_from_xiaoling(db, user, payload)
    monkeypatch.setattr(agent_team_dispatcher, "SessionLocal", lambda: db)
    monkeypatch.setattr(agent_team_dispatcher.settings, "agent_team_enabled", True)
    monkeypatch.setattr(agent_team_dispatcher.settings, "agent_team_max_active_children", 3)
    observed_leases = []
    real_claim_next_task = agent_team_service.claim_next_task

    def observed_claim_next_task(*args, **kwargs):
        observed_leases.append(int(kwargs["lease_seconds"]))
        return real_claim_next_task(*args, **kwargs)

    monkeypatch.setattr(agent_team_service, "claim_next_task", observed_claim_next_task)

    gate = threading.Barrier(3, timeout=2)
    guard = threading.Lock()
    active = 0
    max_active = 0

    def fake_execute(_team_id, claimed):
        nonlocal active, max_active
        assert claimed["task_key"].startswith("read-")
        with guard:
            active += 1
            max_active = max(max_active, active)
        gate.wait()
        time.sleep(0.02)
        with guard:
            active -= 1
        return {"success": True}

    monkeypatch.setattr(agent_team_dispatcher, "_execute_claimed", fake_execute)
    stats = agent_team_dispatcher.dispatch_once(limit=1)

    assert stats["claimed"] == 3
    assert max_active == 3
    assert observed_leases
    assert min(observed_leases) >= int(agent_team_dispatcher.settings.agent_full_validation_wait_seconds) + 60


def test_custom_handler_forwards_frozen_release_snapshot(monkeypatch):
    captured = {}

    def fake_invoke(_db, _user, **kwargs):
        captured.update(kwargs)
        return {"summary": "旧版本已执行", "release_id": kwargs["release_id"], "version_id": kwargs["version_id"]}

    monkeypatch.setattr(published_agent_tools, "invoke_published_agent", fake_invoke)
    result = agent_mesh_dispatcher._custom_handler(
        object(),
        SimpleNamespace(id=7),
        "published_reviewer",
        {
            "user_id": 7,
            "payload": {
                "code": "print('v1')",
                "_agent_team": {
                    "member_snapshot": {
                        "release_id": 11,
                        "version_id": 12,
                        "package_checksum": "p" * 64,
                        "template_checksum": "v" * 64,
                    }
                },
            },
        },
    )

    assert result["status"] == "completed"
    assert captured["release_id"] == 11
    assert captured["version_id"] == 12
    assert captured["package_checksum"] == "p" * 64
    assert captured["template_checksum"] == "v" * 64


def test_custom_team_reads_authorized_file_at_execution(db, super_admin_user, monkeypatch):
    project = Project(user_id=super_admin_user.id, project_name="团队源码", language="python", status="active")
    db.add(project)
    db.flush()
    source = CodeFile(
        project_id=project.id, file_name="auth.py", language="python", content="def allowed():\n    return True\n",
        size_bytes=31, raw_size=31, line_count=2, version_no=1, is_binary=0, status="active",
    )
    db.add(source)
    db.commit()
    captured = {}
    monkeypatch.setattr(
        agent_team_service, "require_active_task_lease",
        lambda *_args, **_kwargs: (SimpleNamespace(), SimpleNamespace(),
                                  SimpleNamespace(id=3, address="custom:published_reviewer")),
    )

    def fake_invoke(_db, _user, **kwargs):
        captured.update(kwargs)
        return {"summary": "已完成", "release_id": 11, "version_id": 12}

    monkeypatch.setattr(published_agent_tools, "invoke_published_agent", fake_invoke)
    message = {
        "context": {"team_id": 9, "agent_team_task_id": 12, "member_id": 3, "lease_token": "lease"},
        "payload": {"project_id": project.id, "file_id": source.id,
                    "_agent_team": {"member_snapshot": {"release_id": 11, "version_id": 12}}},
    }
    result = agent_mesh_dispatcher._custom_handler(
        db, super_admin_user, "published_reviewer", message, trusted_team_execution=True,
    )
    assert result["status"] == "completed"
    assert captured["code"] == source.content
    assert captured["file_name"] == "auth.py"
    assert captured["language"] == "python"
    assert captured["release_id"] == 11
    assert result["evidence"][0]["file_id"] == source.id
    assert "code" not in result["evidence"][0]

    untrusted = agent_mesh_dispatcher._custom_handler(db, super_admin_user, "published_reviewer", message)
    assert untrusted["status"] == "blocked"

    outsider = User(username="other_team_user", password="x", role="user", status=1)
    db.add(outsider)
    db.commit()
    monkeypatch.setattr(agent_mesh_dispatcher.rbac_service, "check_permission", lambda *_args: True)
    captured.clear()
    foreign = agent_mesh_dispatcher._custom_handler(
        db, outsider, "published_reviewer", message, trusted_team_execution=True,
    )
    assert foreign["status"] == "blocked"
    assert captured == {}


def test_custom_team_discards_result_if_source_changes_during_model_call(db, super_admin_user, monkeypatch):
    project = Project(user_id=super_admin_user.id, project_name="审查中变更", language="python", status="active")
    db.add(project)
    db.flush()
    source = CodeFile(
        project_id=project.id, file_name="auth.py", language="python", content="old code",
        size_bytes=8, raw_size=8, line_count=1, version_no=1, is_binary=0, status="active",
    )
    db.add(source)
    db.commit()
    monkeypatch.setattr(
        agent_team_service, "require_active_task_lease",
        lambda *_args, **_kwargs: (SimpleNamespace(), SimpleNamespace(),
                                  SimpleNamespace(id=3, address="custom:published_reviewer")),
    )

    def replace_source(db_session, _user, **_kwargs):
        row = db_session.get(CodeFile, source.id)
        row.content = "new code"
        row.version_no += 1
        db_session.commit()
        return {"summary": "旧源码结论", "release_id": 11, "version_id": 12}

    monkeypatch.setattr(published_agent_tools, "invoke_published_agent", replace_source)
    result = agent_mesh_dispatcher._custom_handler(
        db, super_admin_user, "published_reviewer",
        {"context": {"team_id": 9, "agent_team_task_id": 12, "member_id": 3, "lease_token": "lease"},
         "payload": {"project_id": project.id, "file_id": source.id}},
        trusted_team_execution=True,
    )
    assert result["status"] == "blocked"
    assert "审查结果未回传" in result["summary"]


def test_custom_team_discards_result_if_file_renamed_during_model_call(db, super_admin_user, monkeypatch):
    from app.services import code_file_service

    project = Project(user_id=super_admin_user.id, project_name="审查中重命名", language="python", status="active")
    db.add(project)
    db.flush()
    source = CodeFile(
        project_id=project.id, file_name="auth.py", language="python", content="old code",
        size_bytes=8, raw_size=8, line_count=1, version_no=1, is_binary=0, status="active",
    )
    db.add(source)
    db.commit()
    monkeypatch.setattr(
        agent_team_service, "require_active_task_lease",
        lambda *_args, **_kwargs: (SimpleNamespace(), SimpleNamespace(),
                                  SimpleNamespace(id=3, address="custom:published_reviewer")),
    )

    def rename_source(db_session, user, **_kwargs):
        code_file_service.rename_file(db_session, user, source.id, "renamed.py")
        return {"summary": "旧文件名结论", "release_id": 11, "version_id": 12}

    monkeypatch.setattr(published_agent_tools, "invoke_published_agent", rename_source)
    result = agent_mesh_dispatcher._custom_handler(
        db, super_admin_user, "published_reviewer",
        {"context": {"team_id": 9, "agent_team_task_id": 12, "member_id": 3, "lease_token": "lease"},
         "payload": {"project_id": project.id, "file_id": source.id}},
        trusted_team_execution=True,
    )
    assert source.version_no == 1  # 正常重命名不递增源码版本号
    assert result["status"] == "blocked"
    assert "审查结果未回传" in result["summary"]


def test_governed_sandbox_handler_only_runs_for_trusted_team_lease(monkeypatch):
    calls = []
    reads = []

    class FakeOrchestrator:
        def deploy_project_sandbox(self, **kwargs):
            calls.append(kwargs)
            return AgentResult(success=True, data={"public_id": "sbx_1", "status": "queued"})

    class FakeDb:
        def rollback(self):
            reads.append("rollback")

        def expire_all(self):
            reads.append("expire_all")

        def get(self, model, _row_id):
            if model is AgentTeam:
                return SimpleNamespace(status="running")
            if model is AgentTeamTask:
                return SimpleNamespace(status="running", lease_token="lease-1")
            return None

    monkeypatch.setattr(
        "app.services.agent_governance_service.is_runtime_enabled",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    monkeypatch.setattr(agent_mesh_dispatcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.services.sandbox_service.get_environment",
        lambda *_args: {
            "public_id": "sbx_1",
            "status": "ready",
            "result": {"conclusion": {"passed": True, "summary": "部署就绪"}},
            "artifacts": [{"id": 1, "file_name": "smoke.json"}],
        },
    )
    message = {
        "user_id": 7,
        "trace_id": "team-trace",
        "message_id": "team-task-1",
        "payload": {
            "operation": "deploy",
            "project_id": 31,
            "language": "python",
            "source_revision_id": 41,
        },
        "context": {"team_id": 9, "task_id": 1, "lease_token": "lease-1"},
    }
    user = SimpleNamespace(id=7, role="user")

    db = FakeDb()
    _, denied = agent_mesh_dispatcher._handle(db, user, "agent:sandbox_deployer", message)
    _, completed = agent_mesh_dispatcher._handle(
        db,
        user,
        "agent:sandbox_deployer",
        message,
        trusted_team_execution=True,
    )

    assert denied["status"] == "approval_required"
    assert completed["status"] == "completed"
    assert completed["summary"] == "沙箱部署已达到真实终态 ready"
    assert completed["artifacts"][0]["data"]["artifacts"][0]["file_name"] == "smoke.json"
    assert calls[0]["project_id"] == 31
    assert calls[0]["source_revision_id"] == 41
    assert reads == ["rollback", "expire_all"]


def test_full_validation_releases_ready_dependency_deployment_before_start(monkeypatch):
    order = []

    class FakeOrchestrator:
        def run_full_project_validation(self, **_kwargs):
            order.append(("validate", "sbx_validation"))
            return AgentResult(success=True, data={"public_id": "sbx_validation", "status": "succeeded"})

    class FakeDb:
        def rollback(self):
            pass

        def expire_all(self):
            pass

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    monkeypatch.setattr(
        "app.services.agent_team_service.handoff_dependency_runtime_resources",
        lambda *_args, **_kwargs: order.append(("stop", "sbx_ready_deploy"))
        or [{"public_id": "sbx_ready_deploy", "purpose": "deploy", "status": "stopped"}],
    )

    result = agent_mesh_dispatcher._runtime_handler(
        FakeDb(),
        SimpleNamespace(id=7),
        "test_verifier",
        {
            "user_id": 7,
            "trace_id": "team-trace",
            "message_id": "team-test-release-deploy",
            "payload": {
                "operation": "run_full_project_validation",
                "project_id": 31,
                "language": "python",
                "dependency_context": {
                    "forged": {
                        "artifacts": [
                            {
                                "type": "sandbox_environment",
                                "data": {"public_id": "sbx_other_account", "project_id": 999},
                            }
                        ]
                    }
                },
            },
            "context": {"team_id": 9, "task_id": 2, "lease_token": "lease-2"},
        },
    )

    assert order == [("stop", "sbx_ready_deploy"), ("validate", "sbx_validation")]
    assert result["status"] == "completed"
    assert result["evidence"][0] == {
        "source": "dependency_sandbox_release",
        "data": {"public_id": "sbx_ready_deploy", "purpose": "deploy", "status": "stopped"},
    }


def test_full_validation_fails_closed_when_dependency_deployment_cannot_stop(monkeypatch):
    validation_calls = []

    class FakeOrchestrator:
        def run_full_project_validation(self, **kwargs):
            validation_calls.append(kwargs)
            return AgentResult(success=True, data={"public_id": "must-not-start", "status": "queued"})

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    monkeypatch.setattr(
        "app.services.agent_team_service.handoff_dependency_runtime_resources",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("worker stop timeout")),
    )

    result = agent_mesh_dispatcher._runtime_handler(
        object(),
        SimpleNamespace(id=7),
        "test_verifier",
        {
            "user_id": 7,
            "trace_id": "team-trace",
            "message_id": "team-test-stop-failed",
            "payload": {
                "operation": "run_full_project_validation",
                "project_id": 31,
                "language": "python",
                "dependency_context": {
                    "deploy": {
                        "status": "completed",
                        "artifacts": [
                            {
                                "type": "sandbox_environment",
                                "data": {
                                    "public_id": "sbx_ready_deploy",
                                    "project_id": 31,
                                    "purpose": "deploy",
                                    "status": "ready",
                                },
                            }
                        ],
                    }
                },
            },
            "context": {"team_id": 9, "task_id": 2, "lease_token": "lease-2"},
        },
    )

    assert validation_calls == []
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "dependency_sandbox_release_failed"
    assert result["next_action"] == {"reorder": "validate_before_deploy"}


def test_generated_test_compile_errors_change_retry_strategy_instead_of_repeating_them():
    state = {
        "status": "failed",
        "result": {
            "summary": "测试未通过",
            "agent_tests": {
                "protocol_version": 2,
                "generated": 2,
                "passed_count": 0,
                "failed": 2,
                "files": {"test_ai_health_route.py": "fail", "blackbox.py": "fail"},
                "file_results": {
                    "test_ai_health_route.py": {
                        "status": "fail",
                        "phase": "compile",
                        "failure_kind": "compile_error",
                        "exit_code": 1,
                        "output": "SyntaxError: '(' was never closed",
                    },
                    "blackbox.py": {
                        "status": "fail",
                        "phase": "compile",
                        "failure_kind": "compile_error",
                        "exit_code": 1,
                        "output": "IndentationError: unexpected indent",
                    },
                },
                "details": {
                    "test_ai_health_route.py": "SyntaxError: '(' was never closed",
                    "blackbox.py": "IndentationError: unexpected indent",
                },
            },
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert "编译" in result["strategy_change"]
    assert result["retry_after_seconds"] >= 1
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "sandbox_terminal_failure"
    assert result["next_action"]["regenerate_agent_tests"] is True
    assert result["retryable"] is True


def test_non_python_generated_syntax_failure_also_regenerates_instead_of_blame_source():
    state = {
        "status": "failed",
        "result": {
            "summary": "测试未通过",
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "passed_count": 0,
                "failed": 1,
                "files": {"blackbox.js": "fail"},
                "file_results": {
                    "blackbox.js": {
                        "status": "fail",
                        "phase": "compile",
                        "failure_kind": "compile_error",
                        "exit_code": 1,
                        "output": "SyntaxError: Unexpected token ';'",
                    }
                },
                "details": {"blackbox.js": "SyntaxError: Unexpected token ';'"},
            },
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["next_action"]["regenerate_agent_tests"] is True
    assert result["next_action"]["preserve_business_source"] is True


def test_generated_test_compile_error_with_traceback_regenerates_test():
    state = {
        "status": "failed",
        "result": {
            "summary": "测试未通过",
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "passed_count": 0,
                "failed": 1,
                "files": {"blackbox.py": "fail"},
                "file_results": {
                    "blackbox.py": {
                        "status": "fail",
                        "phase": "compile",
                        "failure_kind": "compile_error",
                        "exit_code": 1,
                        "output": "SyntaxError: '(' was never closed",
                    }
                },
                "details": {
                    "blackbox.py": (
                        '  File "/workspace/_agent_tests/blackbox.py", line 7\n'
                        "    broken = (\n"
                        "              ^\n"
                        "SyntaxError: '(' was never closed"
                    )
                },
            },
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["next_action"]["regenerate_agent_tests"] is True
    assert result["next_action"]["preserve_business_source"] is True


def test_dynamic_security_assertion_failure_is_not_regenerated_as_a_weaker_test():
    state = {
        "status": "failed",
        "result": {
            "summary": "测试未通过",
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "passed_count": 0,
                "failed": 1,
                "files": {"blackbox.py": "fail"},
                "file_results": {
                    "blackbox.py": {
                        "status": "fail",
                        "phase": "execute",
                        "failure_kind": "execution_failure",
                        "exit_code": 1,
                        "output": "AssertionError: expected 403, received 200",
                    }
                },
                "details": {"blackbox.py": "AssertionError: expected 403, received 200"},
            },
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is False
    assert result["next_action"] == {"fix_business_source": True, "requires_review": True}
    assert "regenerate_agent_tests" not in result["next_action"]


def test_dynamic_assertion_quoting_syntax_error_is_not_misclassified_as_test_syntax_failure():
    state = {
        "status": "failed",
        "result": {
            "summary": "测试未通过",
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "passed_count": 0,
                "failed": 1,
                "files": {"blackbox.py": "fail"},
                "file_results": {
                    "blackbox.py": {
                        "status": "fail",
                        "phase": "execute",
                        "failure_kind": "execution_failure",
                        "exit_code": 1,
                        "output": "AssertionError: response leaked SyntaxError in main.py",
                    }
                },
                "details": {"blackbox.py": "AssertionError: response leaked SyntaxError in main.py"},
            },
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is False
    assert result["next_action"] == {"fix_business_source": True, "requires_review": True}
    assert "regenerate_agent_tests" not in result["next_action"]


def test_runtime_error_wrapping_response_syntax_text_is_kept_as_execution_failure():
    state = {
        "status": "failed",
        "result": {
            "summary": "测试未通过",
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "passed_count": 0,
                "failed": 1,
                "files": {"blackbox.py": "fail"},
                "file_results": {
                    "blackbox.py": {
                        "status": "fail",
                        "phase": "execute",
                        "failure_kind": "execution_failure",
                        "exit_code": 1,
                        "output": "RuntimeError: response body was:\nSyntaxError: leaked from app response",
                    }
                },
            },
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is False
    assert result["next_action"] == {"fix_business_source": True, "requires_review": True}
    assert "regenerate_agent_tests" not in result["next_action"]


@pytest.mark.parametrize(
    ("file_name", "output"),
    [
        ("blackbox.go", "./_agent_tests/blackbox.go:2:19: syntax error: unexpected name main"),
        ("blackbox.java", "./_agent_tests/blackbox.java:7: error: ';' expected"),
    ],
)
def test_non_python_compile_phase_is_classified_from_protocol_not_diagnostic_text(file_name, output):
    state = {
        "status": "failed",
        "result": {
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "failed": 1,
                "files": {file_name: "fail"},
                "file_results": {
                    file_name: {
                        "status": "fail",
                        "phase": "compile",
                        "failure_kind": "compile_error",
                        "exit_code": 1,
                        "output": output,
                    }
                },
            }
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is True
    assert result["next_action"]["regenerate_agent_tests"] is True


def test_mixed_compile_and_execution_failures_preserve_business_failure():
    state = {
        "status": "failed",
        "result": {
            "agent_tests": {
                "protocol_version": 2,
                "generated": 2,
                "failed": 2,
                "files": {"test_ai.py": "fail", "blackbox.py": "fail"},
                "file_results": {
                    "test_ai.py": {
                        "status": "fail",
                        "phase": "compile",
                        "failure_kind": "compile_error",
                        "exit_code": 1,
                        "output": "SyntaxError: invalid syntax",
                    },
                    "blackbox.py": {
                        "status": "fail",
                        "phase": "execute",
                        "failure_kind": "execution_failure",
                        "exit_code": 1,
                        "output": "AssertionError: expected 403, received 200",
                    },
                },
            }
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is False
    assert result["next_action"]["fix_business_source"] is True
    assert result["next_action"]["invalid_agent_test_files"] == ["test_ai.py"]
    assert "regenerate_agent_tests" not in result["next_action"]


def test_legacy_failed_marker_without_failure_kind_requires_review():
    state = {
        "status": "failed",
        "result": {
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "failed": 1,
                "files": {"blackbox.py": "fail"},
                "details": {"blackbox.py": "SyntaxError: ambiguous legacy output"},
            }
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is False
    assert result["next_action"] == {
        "requires_review": True,
        "reason": "agent_test_failure_kind_unknown",
    }
    assert "regenerate_agent_tests" not in result["next_action"]


def test_agent_test_infrastructure_failure_switches_worker_without_regeneration():
    state = {
        "status": "failed",
        "result": {
            "agent_tests": {
                "protocol_version": 2,
                "generated": 1,
                "failed": 1,
                "files": {"blackbox.py": "fail"},
                "file_results": {
                    "blackbox.py": {
                        "status": "fail",
                        "phase": "compile",
                        "failure_kind": "infrastructure_error",
                        "exit_code": 127,
                        "output": "python: not found",
                    }
                },
            }
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is True
    assert result["next_action"]["change_worker"] is True
    assert result["next_action"]["validate_runner_protocol"] is True
    assert "regenerate_agent_tests" not in result["next_action"]


def test_project_syntax_failure_is_not_misclassified_as_generated_test_failure():
    state = {
        "status": "failed",
        "result": {
            "summary": "项目编译失败: SyntaxError in main.py",
            "agent_tests": {"generated": 0, "passed_count": 0, "failed": 0},
        },
    }

    result = agent_mesh_dispatcher._sandbox_failure_result(
        "项目沙箱验证",
        state,
        status="failed",
        message=agent_mesh_dispatcher._sandbox_state_message(state),
        code="sandbox_terminal_failure",
    )

    assert result["retryable"] is True
    assert "regenerate_agent_tests" not in result["next_action"]


def test_test_verifier_waits_for_terminal_failure_and_returns_reroute_strategy(monkeypatch):
    class FakeOrchestrator:
        def run_project_tests(self, **_kwargs):
            return AgentResult(success=True, data={"public_id": "sbx_failed", "status": "running"})

    class FakeDb:
        def rollback(self):
            pass

        def expire_all(self):
            pass

        def get(self, model, _row_id):
            if model is AgentTeam:
                return SimpleNamespace(status="running")
            if model is AgentTeamTask:
                return SimpleNamespace(status="running", lease_token="lease-2")
            return None

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    monkeypatch.setattr(
        "app.services.agent_team_service.handoff_dependency_runtime_resources",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(agent_mesh_dispatcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.services.sandbox_service.get_environment",
        lambda *_args: {
            "public_id": "sbx_failed",
            "status": "failed",
            "error": "pytest failed",
            "result": {"conclusion": {"passed": False, "summary": "测试未通过"}},
            "events": [{"stage": "test", "message": "1 failed"}],
            "artifacts": [{"id": 2, "file_name": "report.json"}],
        },
    )

    result = agent_mesh_dispatcher._runtime_handler(
        FakeDb(),
        SimpleNamespace(id=7),
        "test_verifier",
        {
            "user_id": 7,
            "trace_id": "team-trace",
            "message_id": "team-test-1",
            "payload": {
                "operation": "run_project_tests",
                "project_id": 31,
                "language": "python",
                "source_revision_id": 41,
            },
            "context": {"team_id": 9, "task_id": 2, "lease_token": "lease-2"},
        },
    )

    assert result["status"] == "failed"
    assert result["summary"] == "项目沙箱验证终态为 failed：pytest failed"
    assert result["evidence"][0]["data"]["events"][0]["message"] == "1 failed"
    assert result["artifacts"][0]["data"]["artifacts"][0]["file_name"] == "report.json"
    assert "全新沙箱" in result["strategy_change"]


def test_test_verifier_team_runs_combined_whitebox_blackbox_on_requested_revision(monkeypatch):
    """小菱团队中的 test_verifier 将组合验证和修复副本交给隔离沙箱执行。"""
    calls = []

    class FakeOrchestrator:
        def run_project_tests(self, **kwargs):
            calls.append(kwargs)
            return AgentResult(
                success=True,
                data={
                    "public_id": "sbx_combined",
                    "status": "succeeded",
                    "source_revision_id": 41,
                    "source_sha256": "a" * 64,
                    "test_mode": "combined",
                },
            )

    class FakeDb:
        pass

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    monkeypatch.setattr(
        "app.services.agent_team_service.handoff_dependency_runtime_resources",
        lambda *_args, **_kwargs: [],
    )

    result = agent_mesh_dispatcher._runtime_handler(
        FakeDb(),
        SimpleNamespace(id=7, role="super_admin"),
        "test_verifier",
        {
            "user_id": 7,
            "trace_id": "team-combined-trace",
            "message_id": "team-combined-1",
            "payload": {
                "operation": "run_project_tests",
                "project_id": 31,
                "language": "python",
                "test_mode": "combined",
                "source_revision_id": 41,
            },
            "context": {"team_id": 9, "task_id": 3, "lease_token": "lease-combined"},
        },
    )

    assert result["status"] == "completed"
    assert calls == [{
        "project_id": 31,
        "language": "python",
        "test_mode": "combined",
        "worker_code": "",
        "source_revision_id": 41,
        "remote_target_url": "",
        "remote_target_authorized": False,
        "ctx": calls[0]["ctx"],
    }]
    environment_evidence = result["evidence"][0]["data"]
    assert environment_evidence["source_revision_id"] == 41
    assert environment_evidence["source_sha256"] == "a" * 64


def test_team_cancellation_stops_waiting_sandbox(monkeypatch):
    stopped = []

    class FakeOrchestrator:
        def deploy_project_sandbox(self, **_kwargs):
            return AgentResult(success=True, data={"public_id": "sbx_cancel", "status": "queued"})

    class FakeDb:
        def rollback(self):
            pass

        def expire_all(self):
            pass

        def get(self, model, _row_id):
            if model is AgentTeam:
                return SimpleNamespace(status="cancelled")
            if model is AgentTeamTask:
                return SimpleNamespace(status="cancelled")
            return None

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    monkeypatch.setattr(agent_mesh_dispatcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.services.sandbox_service.stop_environment",
        lambda _db, _user, public_id: stopped.append(public_id)
        or {
            "public_id": public_id,
            "status": "stopped",
        },
    )
    monkeypatch.setattr(
        "app.services.sandbox_service.get_environment",
        lambda *_args: pytest.fail("团队取消后不应继续轮询沙箱"),
    )

    result = agent_mesh_dispatcher._runtime_handler(
        FakeDb(),
        SimpleNamespace(id=7),
        "sandbox_deployer",
        {
            "user_id": 7,
            "trace_id": "team-trace",
            "message_id": "team-deploy-cancel",
            "payload": {"operation": "deploy", "project_id": 31, "language": "python"},
            "context": {"team_id": 9, "task_id": 2},
        },
    )

    assert stopped == ["sbx_cancel"]
    assert result["status"] == "cancelled"
    assert result["artifacts"][0]["data"]["status"] == "stopped"


def test_replaced_lease_stops_old_waiting_sandbox(monkeypatch):
    stopped = []

    class FakeOrchestrator:
        def deploy_project_sandbox(self, **_kwargs):
            return AgentResult(success=True, data={"public_id": "sbx_old_lease", "status": "queued"})

    class FakeDb:
        def rollback(self):
            pass

        def expire_all(self):
            pass

        def get(self, model, _row_id):
            if model is AgentTeam:
                return SimpleNamespace(status="running")
            if model is AgentTeamTask:
                return SimpleNamespace(status="running", lease_token="lease-new")
            return None

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    monkeypatch.setattr(agent_mesh_dispatcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.services.sandbox_service.stop_environment",
        lambda _db, _user, public_id: stopped.append(public_id)
        or {
            "public_id": public_id,
            "status": "stopped",
        },
    )
    monkeypatch.setattr(
        "app.services.sandbox_service.get_environment",
        lambda *_args: pytest.fail("旧租约被接管后不应继续轮询沙箱"),
    )

    result = agent_mesh_dispatcher._runtime_handler(
        FakeDb(),
        SimpleNamespace(id=7),
        "sandbox_deployer",
        {
            "user_id": 7,
            "trace_id": "team-trace",
            "message_id": "team-deploy-old-lease",
            "payload": {"operation": "deploy", "project_id": 31, "language": "python"},
            "context": {"team_id": 9, "task_id": 2, "lease_token": "lease-old"},
        },
    )

    assert stopped == ["sbx_old_lease"]
    assert result["status"] == "cancelled"


def test_retry_strategy_is_consumed_by_runtime_handler(monkeypatch):
    captured = {}

    class FakeOrchestrator:
        def analyze_project(self, _folder_name, _file_names, **kwargs):
            captured.update(kwargs)
            return AgentResult(success=True, data={"language": "python"})

    monkeypatch.setattr(
        "app.agents.orchestrator.get_request_orchestrator",
        lambda *_args, **_kwargs: FakeOrchestrator(),
    )
    result = agent_mesh_dispatcher._runtime_handler(
        object(),
        SimpleNamespace(id=7),
        "project_analyzer",
        {
            "user_id": 7,
            "trace_id": "team-trace",
            "message_id": "retry-2",
            "payload": {
                "folder_name": "demo",
                "file_names": ["main.py"],
                "_execution_strategy": {
                    "mode": "alternate_reasoning_with_failure_context",
                    "instruction": "改用更小的输入并重新分析扩展名",
                },
            },
            "context": {},
        },
    )

    assert result["status"] == "completed"
    assert captured["strategy_instruction"] == "改用更小的输入并重新分析扩展名"
