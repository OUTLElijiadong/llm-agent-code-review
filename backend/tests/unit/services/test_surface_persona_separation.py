"""跨页面统一小菱身份与控制面权限隔离回归测试。

复现并锁定两类历史 bug:
- 管理端和成员端必须显示同一小菱身份，但运行能力与审计权限仍由 surface 隔离;
- 管理端提示词不得要求切换到另一个助手或声称拥有未注册的成员能力。
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
    """surface 变化不应改变用户可见的助手身份。"""
    assert surface_agent_identity("admin") == ("chat_assistant", "小菱")
    assert surface_agent_identity("user") == ("chat_assistant", "小菱")


def test_admin_instructions_are_jarvis_ops_not_xiaoling_audit(db, admin):
    """管理端仍叫小菱；管理员职责和能力范围由权限工具限制。"""
    instructions = _instructions("admin", admin, is_super_admin=True)
    assert "小菱" in instructions
    assert "贾维斯" not in instructions
    assert "全局运维" in instructions
    assert "不得要求切换账号" in instructions
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
async def test_admin_surface_tool_events_keep_xiaoling_identity(db, admin, monkeypatch):
    """管理端工具事件也归属统一的小菱身份。"""
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
