"""小菱工作状态事件广播回归测试。

覆盖两处发射点:
- PrismToolExecutor._emit_tool_event → AgentEventBus PROGRESS(agent=chat_assistant)
- pentest_service._emit_pentest_event → 阶段进度/终态事件

这些事件是 Agent 中心工位卡显示「小菱正在工作」的唯一数据源。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.agents.event_bus as event_bus_module
from app.core.database import Base
from app.models.pentest import PentestEngagement
from app.models.project import Project
from app.models.user import User
from app.services import agent_responses_service as service_module
from app.services import pentest_service
from app.services.agent_responses_service import PrismToolExecutor
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
def user(db):
    row = User(username="status-event-user", password="x", role="user", status=1)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def project(db, user):
    row = Project(user_id=int(user.id), project_name="状态事件项目", language="php", status="active")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def executor(db, user, monkeypatch):
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_args, **_kwargs: _bare_orchestrator())
    return PrismToolExecutor(
        db,
        user,
        surface="user",
        run_id="run-status-event",
        mcp_provider=EmptyMcp(),
    )


@pytest.mark.asyncio
async def test_tool_started_broadcasts_progress_event(executor, user, monkeypatch):
    emitted = []
    monkeypatch.setattr(event_bus_module, "emit_event", lambda *args, **kwargs: emitted.append((args, kwargs)))

    arguments = {"project_id": 1, "target_type": "web"}
    call = ToolCall(
        call_id="call-status-1",
        name="create_pentest_engagement",
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )
    await executor._emit_tool_event("response.tool.started", call)

    assert len(emitted) == 1
    (args, kwargs) = emitted[0]
    assert args[0].value == "progress"
    assert args[1] == "chat_assistant"
    assert args[2] == "run-status-event"
    assert "create_pentest_engagement" in kwargs.get("message", "")
    assert kwargs.get("user_id") == int(user.id)
    # 工具结束事件不再重复广播(避免工位卡状态抖动)
    await executor._emit_tool_event("response.tool.completed", call)
    assert len(emitted) == 1


def test_pentest_phase_event_broadcast(monkeypatch, user, project):
    emitted = []
    monkeypatch.setattr(event_bus_module, "emit_event", lambda *args, **kwargs: emitted.append((args, kwargs)))

    engagement = PentestEngagement(
        public_id="pt-evt000000000001",
        user_id=int(user.id),
        project_id=int(project.id),
        target_type="web",
        scope_json="{}",
        status="running",
    )
    pentest_service._emit_pentest_event("progress", engagement, "渗透测试·情报收集进行中")
    pentest_service._emit_pentest_event("complete", engagement, "渗透测试完成, 报告已生成")

    assert len(emitted) == 2
    (progress_args, progress_kwargs) = emitted[0]
    assert progress_args[0].value == "progress"
    assert progress_args[1] == "chat_assistant"
    assert progress_args[2] == "pt-evt000000000001"
    assert "情报收集" in progress_kwargs.get("message", "")
    assert progress_kwargs.get("user_id") == int(user.id)
    (complete_args, _kwargs) = emitted[1]
    assert complete_args[0].value == "complete"


def test_pentest_event_failure_never_breaks_pipeline(monkeypatch, user, project):
    """广播失败必须被吞掉: Agent 状态展示不能影响流水线本身。"""
    monkeypatch.setattr(
        event_bus_module,
        "emit_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("redis down")),
    )
    engagement = PentestEngagement(
        public_id="pt-evt000000000002",
        user_id=int(user.id),
        project_id=int(project.id),
        target_type="api",
        scope_json="{}",
        status="running",
    )
    pentest_service._emit_pentest_event("progress", engagement, "不应抛出")
