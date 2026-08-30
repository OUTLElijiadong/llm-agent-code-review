"""控制面/体验面身份分离回归测试。

复现并锁定两类历史 bug:
- bug①: 管理端(贾维斯)运行的工具事件被记到 chat_assistant(小菱)名下,
  Agent 中心工位卡显示错身份;
- bug②: 管理端人设复用小菱并携带成员侧审计指令(审计叙事混入运维面)。
"""

from __future__ import annotations

import json

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
from app.services.deepseek_responses_runtime import ToolCall
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
    """surface → (agent code, 称谓) 单一事实源。"""
    assert surface_agent_identity("admin") == ("manager", "贾维斯")
    assert surface_agent_identity("user") == ("chat_assistant", "小菱")


def test_admin_instructions_are_jarvis_ops_not_xiaoling_audit(db, admin):
    """管理端人设: 贾维斯全局运维定位, 不叫小菱, 不携带成员侧审计指令。"""
    instructions = _instructions("admin", admin, is_super_admin=True)
    assert "贾维斯" in instructions
    assert "全局运维" in instructions
    assert "小菱" in instructions  # 仅作为"成员侧找小菱"的引导出现
    # 成员侧审计指令不得进入管理面人设
    assert "audit_security_for_project" not in instructions
    # 运维职责清单在场
    for keyword in ("态势巡查", "审批", "服务器运维"):
        assert keyword in instructions


def test_user_instructions_stay_xiaoling(db):
    """成员端人设保持小菱, 审计指令保留(审计是成员业务)。"""
    from app.models.user import User as _U

    row = _U(username="persona-user", password="x", role="user", status=1)
    db.add(row)
    db.commit()
    instructions = _instructions("user", row)
    assert "小菱" in instructions
    assert "贾维斯" not in instructions
    assert "audit_security_for_project" in instructions


@pytest.mark.asyncio
async def test_admin_surface_tool_events_attributed_to_manager(db, admin, monkeypatch):
    """bug①复现: 管理端工具事件必须归 manager(贾维斯), 不再冒充小菱。"""
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
    assert args[1] == "manager", "管理端事件被误记到小菱(chat_assistant)名下"
    assert "贾维斯" in kwargs.get("message", "")
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
async def test_member_pentest_tools_blocked_on_admin_surface(db, admin, monkeypatch):
    """bug③复现: 成员侧渗透工具在管理面必须被网关层拒绝(不只靠人设约束)。"""
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
    assert "成员侧" in (result.error or "")
