"""管理员与成员 surface 共用唯一小菱主控身份、权限仍按账号隔离。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.agents.event_bus as event_bus_module
from app.core.database import Base
from app.models.user import User
from app.services import agent_responses_service as service_module
from app.services.agent_responses_service import (
    PrismToolExecutor,
    _instructions,
    surface_agent_identity,
)
from app.services.deepseek_responses_runtime import InvalidRunStateError, ToolCall
from tests.unit.services.test_change_password_tool import EmptyMcp, _bare_orchestrator


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def admin(db):
    row = User(username="persona-admin", password="x", role="super_admin", status=1)
    db.add(row)
    db.commit()
    return row


def test_surface_agent_identity_mapping():
    """两个会话 surface 只能指向同一个主控身份。"""
    assert surface_agent_identity("admin") == ("chat_assistant", "小菱")
    assert surface_agent_identity("user") == ("chat_assistant", "小菱")


def test_admin_instructions_use_xiaoling_for_review_and_admin_work(db, admin):
    """管理端由小菱同时处理项目审查与治理运维，不再转交第二主 Agent。"""
    instructions = _instructions("admin", admin, is_super_admin=True)
    assert "唯一主控 Agent「棱镜小助·小菱」" in instructions
    assert "audit_security_for_project" in instructions
    assert "代码审查、安全审计、渗透测试、审批和运维都由你作为唯一主控处理" in instructions
    assert "由成员侧的小菱负责" not in instructions
    # 运维职责清单在场
    for keyword in ("态势巡查", "审批", "服务器运维"):
        assert keyword in instructions


def test_user_instructions_stay_xiaoling(db):
    """成员端仍使用同一个小菱主控身份。"""
    from app.models.user import User as _U

    row = _U(username="persona-user", password="x", role="user", status=1)
    db.add(row)
    db.commit()
    instructions = _instructions("user", row)
    assert "小菱" in instructions
    assert "贾维斯" not in instructions
    assert "audit_security_for_project" in instructions


@pytest.mark.asyncio
async def test_admin_surface_uses_one_root_with_business_and_admin_tools(db, admin, monkeypatch):
    """管理端小菱能看到权限过滤后的项目审计、团队与管理能力工具。"""
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())
    executor = PrismToolExecutor(
        db,
        admin,
        surface="admin",
        run_id="run-xiaoling-unified-admin",
        session_key="session-admin-unified",
        mcp_provider=EmptyMcp(),
    )

    schemas = await executor.tool_schemas()
    names = {str(item.get("name") or "") for item in schemas}

    assert "start_review" in names
    assert "audit_security_for_project" in names
    assert "create_agent_team" in names
    assert "admin_execute_capability" in names
    assert "user_execute_capability" in names


def test_xiaoling_must_ask_review_mode_before_dispatch_when_unspecified(db):
    """审查方式未提供时必须先询问，且只呈现对应执行路径支持的模式。"""
    row = User(username="review-mode-user", password="x", role="user", status=1)
    db.add(row)
    db.commit()

    instructions = _instructions("user", row)

    assert "审查方式未明确" in instructions
    assert "ask_user" in instructions
    assert "allow_free_text=false" in instructions
    assert "option value 必须与对应工具参数值完全一致" in instructions
    assert "quick|standard|security|performance|full" in instructions
    assert "quick|standard|deep" in instructions
    assert "triage|full|static_full" in instructions
    assert "不得把有界语义检查称为完整语义覆盖" in instructions
    assert "正式 AgentTeam 的 review_orchestrator 仅用 full|security" in instructions
    assert "若用户尚未选择扫描方式，必须先用 ask_user" in instructions
    assert "代码解释、报告查询" in instructions


def test_xiaoling_source_repair_requires_review_approval_and_same_revision_retest(db):
    """源码修复由只读子 Agent 提案，小菱走审批写入并复测同一源码快照。"""
    row = User(username="source-repair-user", password="x", role="user", status=1)
    db.add(row)
    db.commit()

    instructions = _instructions("user", row)

    for requirement in (
        "临时修复子 Agent",
        "子 Agent 只能提交补丁建议",
        "code_files.update",
        "等待用户审批",
        "重新读取文件确认新版本",
        "run_full_project_validation",
        "不可变源码 SHA-256",
        "同一项目的同一副本 ID",
        "重新执行对应安全审计",
        "原始问题要用同一修订重新复测",
        "test_mode:'combined'",
        "同一不可变源码快照执行白盒与黑盒阶段",
        "验证结果须回报真实 source_sha256",
        "create_pentest_engagement",
        "用户签署授权",
    ):
        assert requirement in instructions


@pytest.mark.asyncio
async def test_admin_surface_tool_events_attributed_to_xiaoling(db, admin, monkeypatch):
    """管理端工具事件归唯一主控小菱，surface 字段仍区分会话权限域。"""
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())
    executor = PrismToolExecutor(
        db,
        admin,
        surface="admin",
        run_id="run-persona-admin",
        mcp_provider=EmptyMcp(),
    )
    emitted = []
    monkeypatch.setattr(event_bus_module, "emit_event", lambda *args, **kwargs: emitted.append((args, kwargs)))

    call = ToolCall(
        call_id="call-persona-1",
        name="admin_describe_capabilities",
        arguments={"page": "dashboard"},
        raw_arguments=json.dumps({"page": "dashboard"}),
    )
    await executor._emit_tool_event("response.tool.started", call)

    assert len(emitted) == 1
    (args, kwargs) = emitted[0]
    assert args[1] == "chat_assistant"
    assert "小菱" in kwargs.get("message", "")
    assert kwargs.get("user_id") == int(admin.id)


@pytest.mark.asyncio
async def test_user_surface_tool_events_stay_xiaoling(db, monkeypatch):
    """成员端回归: 事件仍归小菱。"""
    row = User(username="persona-user2", password="x", role="user", status=1)
    db.add(row)
    db.commit()
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())
    executor = PrismToolExecutor(
        db,
        row,
        surface="user",
        run_id="run-persona-user",
        mcp_provider=EmptyMcp(),
    )
    emitted = []
    monkeypatch.setattr(event_bus_module, "emit_event", lambda *args, **kwargs: emitted.append((args, kwargs)))
    call = ToolCall(
        call_id="call-persona-2",
        name="list_projects",
        arguments={},
        raw_arguments="{}",
    )
    await executor._emit_tool_event("response.tool.started", call)
    (args, kwargs) = emitted[0]
    assert args[1] == "chat_assistant"
    assert "小菱" in kwargs.get("message", "")


@pytest.mark.asyncio
async def test_admin_surface_can_route_pentest_to_business_service(db, admin, monkeypatch):
    """管理员 surface 使用同一小菱主控，固定渗透工具进入真实业务校验。"""
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())
    executor = PrismToolExecutor(
        db,
        admin,
        surface="admin",
        run_id="run-persona-block",
        mcp_provider=EmptyMcp(),
    )
    call = ToolCall(
        call_id="call-persona-block",
        name="create_pentest_engagement",
        arguments={"project_id": 1, "target_type": "web"},
        raw_arguments=json.dumps({"project_id": 1, "target_type": "web"}),
    )
    result = await executor.execute(call, approved=True)
    assert result.status == "error"
    assert result.error == "项目不存在"


@pytest.mark.asyncio
async def test_remote_blackbox_approval_is_bound_to_exact_target(db, admin, monkeypatch):
    """改换 URL 的同 call_id 不能复用用户对另一个目标的批准。"""
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())
    executor = PrismToolExecutor(
        db,
        admin,
        surface="admin",
        run_id="run-remote-target-binding",
        mcp_provider=EmptyMcp(),
    )
    executor._has_permission = lambda _code: True
    original = ToolCall(
        call_id="call-remote-binding",
        name="run_project_tests",
        arguments={
            "project_id": 1,
            "language": "python",
            "remote_target_url": "https://authorized.example.test/health",
            "remote_target_authorized": False,
        },
        raw_arguments="{}",
    )

    pending = await executor.execute(original)
    assert pending.status == "approval_required"
    assert pending.preview == {
        "remote_target_url": "https://authorized.example.test/health",
        "method": "GET",
    }

    altered = ToolCall(
        call_id=original.call_id,
        name=original.name,
        arguments={**original.arguments, "remote_target_url": "https://different.example.test/health"},
        raw_arguments="{}",
    )
    with pytest.raises(InvalidRunStateError, match="审批参数与当前工具调用不匹配"):
        await executor.execute(altered, approved=True)


@pytest.mark.asyncio
async def test_remote_blackbox_runs_only_after_click_approval_and_forces_server_authorization(
    db,
    admin,
    monkeypatch,
):
    """远程黑盒必须先生成当前账号审批，批准后由服务端设置授权标志。"""
    captured: list[dict] = []
    orchestrator = _bare_orchestrator()

    def fake_invoke_tool(_name, arguments, _ctx):
        captured.append(dict(arguments))
        return SimpleNamespace(success=True, data={"status": "ok"}, error="")

    orchestrator.invoke_tool = fake_invoke_tool
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: orchestrator)
    executor = PrismToolExecutor(
        db,
        admin,
        surface="admin",
        run_id="run-remote-target-approval",
        mcp_provider=EmptyMcp(),
    )
    executor._has_permission = lambda _code: True
    call = ToolCall(
        call_id="call-remote-approval",
        name="run_project_tests",
        arguments={
            "project_id": 1,
            "language": "python",
            "remote_target_url": "https://authorized.example.test/health",
            "remote_target_authorized": False,
        },
        raw_arguments="{}",
    )

    pending = await executor.execute(call)
    assert pending.status == "approval_required"
    assert captured == []

    completed = await executor.execute(call, approved=True)
    assert completed.status == "success"
    assert len(captured) == 1
    assert captured[0]["remote_target_url"] == "https://authorized.example.test/health"
    assert captured[0]["remote_target_authorized"] is True


@pytest.mark.asyncio
async def test_remote_target_in_agent_team_requires_click_approval_and_is_bound(db, admin, monkeypatch):
    """含远程目标的子 Agent 团队须展示目标待批准，获批后才授予本次探测。"""
    captured: list[dict] = []
    orchestrator = _bare_orchestrator()

    def fake_invoke_tool(_name, arguments, _ctx):
        captured.append(arguments)
        return SimpleNamespace(success=True, data={"team_id": 42, "status": "queued"}, error="")

    orchestrator.invoke_tool = fake_invoke_tool
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: orchestrator)
    executor = PrismToolExecutor(
        db,
        admin,
        surface="admin",
        run_id="run-team-remote-target",
        mcp_provider=EmptyMcp(),
    )
    args = {
        "title": "授权黑盒验证",
        "objective": "仅对用户授权的测试目标执行只读探测",
        "members": [{"member_key": "tester", "display_name": "测试员", "address": "agent:test_verifier"}],
        "tasks": [{
            "task_key": "probe",
            "member_key": "tester",
            "title": "探测健康页",
            "instructions": "对精确授权目标发出一次 GET 并回报响应状态",
            "input": {
                "operation": "run_project_tests",
                "project_id": 1,
                "language": "python",
                "remote_target_url": "https://authorized.example.test/health",
                "remote_target_authorized": False,
            },
        }],
    }
    call = ToolCall("call-team-remote", "create_agent_team", args, json.dumps(args))

    pending = await executor.execute(call)
    assert pending.status == "approval_required"
    assert pending.preview["remote_targets"] == ["https://authorized.example.test/health"]
    assert pending.preview["method"] == "GET"
    assert pending.preview["risk_level"] == "high"
    assert pending.preview["plan_sha256"]
    assert captured == []

    completed = await executor.execute(call, approved=True)
    assert completed.status == "success"
    assert len(captured) == 1
    task_input = captured[0]["tasks"][0]["input"]
    assert task_input["remote_target_authorized"] is True
