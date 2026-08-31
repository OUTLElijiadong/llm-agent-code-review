"""多任务子Agent团队 + 贾维斯批量内测码 权限链路回归。

覆盖用户点名的两个场景:
- 小菱(user/admin 任一面)多任务/并行场景能新建并调用子 Agent 团队;
- 贾维斯(admin 面)批量生成内测码: 权限门→审批门→一次性明文码回连接。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.user import User
from app.services import agent_responses_service as service_module
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
    row = User(username="team-user", password="x", role="user", status=1)
    db.add(row)
    db.commit()
    return row


_ORCH_HOLDER: dict = {}


def _orch_with_team_stub():
    return _ORCH_HOLDER["orch"]


def _executor(db, user_row, surface: str, run_id: str) -> PrismToolExecutor:
    return PrismToolExecutor(
        db,
        user_row,
        surface=surface,
        run_id=run_id,
        session_key="sess-team-test",
        mcp_provider=EmptyMcp(),
    )


def _team_call(call_id: str, title: str) -> ToolCall:
    arguments: dict[str, Any] = {
        "title": title,
        "objective": "并行验证两个独立子任务并汇总",
        "members": [
            {"member_key": "researcher", "display_name": "调研员", "address": "agent:chat_assistant"},
            {"member_key": "verifier", "display_name": "核验员", "address": "agent:chat_assistant"},
        ],
        "tasks": [
            {"task_key": "t1", "member_key": "researcher", "title": "调研资料",
                "instructions": "并行调研并输出要点", "depends_on": []},
            {"task_key": "t2", "member_key": "verifier", "title": "核验结论",
                "instructions": "核验调研结论", "depends_on": ["t1"]},
        ],
    }
    return ToolCall(
        call_id=call_id,
        name="create_agent_team",
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )


@pytest.mark.asyncio
async def test_user_surface_creates_parallel_teams_via_invoke_tool(db, user, monkeypatch):
    """成员面小菱: 多任务场景下可连续新建多个并行子 Agent 团队(真实 service)。"""
    from app.agents.base import AgentContext, AgentResult

    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _orch_with_team_stub())

    captured_ctx: list[AgentContext] = []

    def fake_invoke_tool(tool_name, arguments, ctx):
        captured_ctx.append(ctx)
        assert tool_name == "create_agent_team"
        assert ctx.extra.get("surface") == "user"
        assert ctx.extra.get("session_key") == "sess-team-test"
        return AgentResult(success=True, data={"team_id": 101, "status": "queued"})

    orch = _bare_orchestrator()
    monkeypatch.setattr(orch, "invoke_tool", fake_invoke_tool, raising=False)
    _ORCH_HOLDER["orch"] = orch

    executor = _executor(db, user, "user", "run-team-user")
    first = await executor.execute(_team_call("call-team-1", "团队一"), approved=True)
    second = await executor.execute(_team_call("call-team-2", "团队二"), approved=True)

    assert first.status == "success" and second.status == "success", (first.error, second.error)
    assert len(captured_ctx) == 2, "两个并行团队都必须真实触达团队服务"


@pytest.mark.asyncio
async def test_admin_surface_batch_beta_codes_full_chain(db, monkeypatch):
    """贾维斯批量内测码: 权限门→能力校验→审批门(CRITICAL)→一次性明文码回连接。"""
    from app.models.user import User as _U

    admin = _U(username="beta-admin", password="x", role="super_admin", status=1)
    db.add(admin)
    db.commit()
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())

    executor = _executor(db, admin, "admin", "run-beta-batch")

    call = ToolCall(
        call_id="call-beta-batch",
        name="admin_execute_capability",
        arguments={
            "capability": "beta_codes.generate",
            "params": {"count": 5, "expiry_days": 7, "label": "贾维斯批量"},
        },
        raw_arguments=json.dumps({"capability": "beta_codes.generate", "params": {"count": 5}}),
    )

    # 第一轮: CRITICAL 能力必须先出审批卡(超管也不能免确认)
    pending = await executor.execute(call, approved=False)
    assert pending.status == "approval_required", f"高危能力应先审批: {pending.status} {getattr(pending, 'error', '')}"

    executed: dict[str, Any] = {}

    async def fake_execute_api(user, spec, params, request_id=""):
        executed["spec"] = spec.code
        executed["params"] = params
        return {"code": 0, "data": {"codes": ["PRISM-A1", "PRISM-A2", "PRISM-A3", "PRISM-A4", "PRISM-A5"], "items": []}}

    from app.services import admin_capability_service

    monkeypatch.setattr(admin_capability_service, "execute_api", fake_execute_api)

    approved_result = await executor.execute(call, approved=True)
    assert approved_result.status == "success", approved_result.error
    assert executed["spec"] == "beta_codes.generate"
    assert executed["params"]["count"] == 5
    codes = (approved_result.output or {}).get("data", {}).get("one_time_codes")
    assert codes == ["PRISM-A1", "PRISM-A2", "PRISM-A3", "PRISM-A4", "PRISM-A5"], "批量明文码必须一次性回连接"


@pytest.mark.asyncio
async def test_user_surface_cannot_execute_admin_beta_capability(db, user, monkeypatch):
    """成员面小菱调管理能力: 网关直接拒绝(能力隔离)。"""
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())
    executor = _executor(db, user, "user", "run-beta-deny")
    call = ToolCall(
        call_id="call-beta-deny",
        name="admin_execute_capability",
        arguments={"capability": "beta_codes.generate", "params": {"count": 1}},
        raw_arguments="{}",
    )
    result = await executor.execute(call, approved=True)
    assert result.status == "error"
    assert "管理员" in (result.error or "")


@pytest.mark.asyncio
async def test_member_business_tools_blocked_and_unlisted_on_admin_surface(db, user, monkeypatch):
    """审计/沙箱/下载/圆桌/审查等成员业务工具: 管理面 schema 不广告 + 网关硬拒。"""
    from app.models.user import User as _U

    admin = _U(username="biz-admin", password="x", role="super_admin", status=1)
    db.add(admin)
    db.commit()
    monkeypatch.setattr(service_module, "get_request_orchestrator", lambda *_a, **_k: _bare_orchestrator())
    executor = _executor(db, admin, "admin", "run-biz-block")

    # 网关: 逐个成员业务工具在管理面直接拒绝
    for tool in ("audit_security_for_project", "deploy_project_sandbox", "download_project_source",
                 "start_roundtable_discussion", "start_review"):
        call = ToolCall(call_id=f"call-{tool}", name=tool, arguments={}, raw_arguments="{}")
        result = await executor.execute(call, approved=True)
        assert result.status == "error", tool
        assert "成员侧" in (result.error or ""), tool

    # schema: 成员业务工具不出现在管理面工具清单
    schemas = await executor.tool_schemas()
    names = {item["name"] for item in schemas}
    for tool in ("audit_security_for_project", "deploy_project_sandbox", "download_project_source",
                 "start_roundtable_discussion", "start_review", "create_pentest_engagement"):
        assert tool not in names, f"{tool} 不应出现在管理面工具清单"
    # 管理面应有工具与团队工具仍在
    assert "admin_execute_capability" in names
    assert "create_agent_team" in names

    # 成员面回归: 这些工具仍可进入清单(不受影响)
    executor_user = _executor(db, user, "user", "run-biz-user")
    user_names = {item["name"] for item in await executor_user.tool_schemas()}
    # 该测试用户无项目权限种子, download_project_source 会被正常过滤; 用圆桌工具断言成员业务工具仍在
    assert "start_roundtable_discussion" in user_names


def test_list_agents_hides_governance_contracts_for_user_surface(db, user):
    """成员面 list_agents 隐藏治理契约(manager/operations/monitor 等), 管理面全量。"""
    from app.services import agent_mesh_service

    user_items = agent_mesh_service.list_agents(db, user, surface="user")["agents"] \
        if isinstance(agent_mesh_service.list_agents(db, user, surface="user"), dict) \
        and "agents" in agent_mesh_service.list_agents(db, user, surface="user") else None
    if user_items is None:
        payload = agent_mesh_service.list_agents(db, user, surface="user")
        user_items = payload.get("items") or payload.get("agents") or []
    codes = {str(item.get("address", "")).split(":")[-1] for item in user_items}
    for hidden in ("manager", "operations", "monitor", "orchestrator", "evolution"):
        assert hidden not in codes, f"成员面不应看到 {hidden}"

    admin_payload = agent_mesh_service.list_agents(db, user, surface="admin")
    admin_items = admin_payload.get("items") or admin_payload.get("agents") or []
    admin_codes = {str(item.get("address", "")).split(":")[-1] for item in admin_items}
    assert {"manager", "operations", "monitor"} <= admin_codes
