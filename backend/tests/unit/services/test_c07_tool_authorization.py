"""C07：模拟受诱导的工具调用，核对真实账号、路由、资源与审批边界。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.base import AgentContext
from app.agents.chat_planner import ToolCall as PlannerToolCall
from app.agents.clarify_store import ClarifyStore
from app.api.v1 import agents, code_files, projects, reports, review, rules
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.models.agent_governance import ApprovalItem
from app.models.agent_response_run import AgentToolExecution
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.agent_responses_service import PrismToolExecutor
from app.services.deepseek_responses_runtime import ToolCall


class EmptyMcp:
    async def discover(self):
        return []

    def has_tool(self, _name):
        return False


@pytest.fixture
def tool_scope(request):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    actor_role = getattr(request, "param", "user")
    user = User(username="c07-owner", password='unit-test-password', role=actor_role, status=1)
    other = User(username="c07-other", password='unit-test-password', role="user", status=1)
    role = Role(code=actor_role, name=actor_role, status="active", is_builtin=1)
    db.add_all([user, other, role])
    db.flush()
    links = {}
    for code in ("project:view", "project:create", "project:update", "project:delete", "file:view",
                 "report:view", "review:view", "issue:view", "rule:view", "review:start", "agent:chat"):
        permission = Permission(code=code, name=code, module=code.split(":")[0], type="api")
        db.add(permission)
        db.flush()
        links[code] = RolePermission(role_id=role.id, permission_id=permission.id)
        db.add(links[code])
    db.add_all([UserRole(user_id=user.id, role_id=role.id), UserRole(user_id=other.id, role_id=role.id)])
    own = Project(user_id=user.id, project_name="C07 自有项目", status="active")
    foreign = Project(user_id=other.id, project_name="PRIVATE_FOREIGN_PROJECT", status="active")
    db.add_all([own, foreign])
    db.flush()
    db.add(CodeFile(project_id=own.id, file_name="source.py", language="python", content="pass", status="active"))
    db.add(ReviewTask(user_id=user.id, project_id=own.id, task_name="C07 报告", review_type="full",
                      status="success", total_files=1, processed_files=1))
    db.commit()
    app = FastAPI()
    register_handlers(app)
    for router, prefix in ((projects.router, "/projects"), (code_files.router, "/code-files"),
                            (reports.router, "/reports"), (review.router, "/review"),
                            (rules.router, "/rules"), (agents.router, "/agents")):
        app.include_router(router, prefix=prefix)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    executor = PrismToolExecutor(db, user, surface="user", run_id="c07-run", session_key="c07-session",
                                 mcp_provider=EmptyMcp())
    try:
        with TestClient(app) as client:
            yield db, user, own, foreign, links, executor, client
    finally:
        db.close()
        engine.dispose()


READ_TOOLS = (
    ("list_projects", "project:view", "/projects"),
    ("list_code_files", "file:view", "/code-files"),
    ("list_reports", "report:view", "/reports"),
    ("list_review_tasks", "review:view", "/review/tasks"),
    ("list_review_issues", "issue:view", "/review/tasks/1/issues"),
    ("list_rules", "rule:view", "/rules"),
)


def read_arguments(tool, own):
    if tool == "list_review_issues":
        return {"task_id": 1}
    return {} if tool in {"list_projects", "list_rules"} else {"project_id": own.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,permission,path", READ_TOOLS)
async def test_revoked_permission_is_denied_by_route_and_same_account_tool(tool_scope, tool, permission, path):
    db, _user, own, _foreign, links, executor, client = tool_scope
    db.delete(links[permission])
    db.commit()
    args = read_arguments(tool, own)
    assert client.get(path, params=args).status_code == 403
    result = await executor.execute(ToolCall("denied", tool, args, json.dumps(args)))
    assert result.status == "error", result.output
    assert permission in result.error
    assert tool not in {item["name"] for item in await executor.tool_schemas()}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,permission,path", READ_TOOLS)
async def test_authorized_fixed_read_tools_keep_same_account_positive_path(tool_scope, tool, permission, path):
    _db, _user, own, _foreign, _links, executor, client = tool_scope
    args = read_arguments(tool, own)
    assert client.get(path, params=args).status_code == 200
    result = await executor.execute(ToolCall("allowed", tool, args, json.dumps(args)))
    assert result.status == "success", result.error
    assert "PRIVATE_FOREIGN_PROJECT" not in json.dumps(result.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,permission", (("create_project", "project:create"),
                                           ("delete_project", "project:delete"),
                                           ("update_project", "project:update"),
                                           ("start_review", "review:start")))
async def test_write_tool_missing_permission_is_denied_before_approval(tool_scope, tool, permission):
    db, _user, own, _foreign, links, executor, client = tool_scope
    db.delete(links[permission])
    db.commit()
    args = {"project_name": "C07 禁止创建"} if tool == "create_project" else {"project_id": own.id}
    if tool == "create_project":
        response = client.post("/projects", json=args)
    elif tool == "delete_project":
        response = client.delete(f"/projects/{own.id}")
    elif tool == "update_project":
        args["project_name"] = "C07 不应更新"
        response = client.put(f"/projects/{own.id}", json={"project_name": args["project_name"]})
    else:
        response = client.post("/review/start", json={**args, "file_ids": [1]})
    assert response.status_code == 403
    result = await executor.execute(ToolCall("write-denied", tool, args, json.dumps(args)))
    assert result.status == "error"
    assert permission in result.error
    assert db.query(ApprovalItem).count() == 0
    assert db.get(Project, own.id).status == "active"
    assert db.query(Project).count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,permission", (("delete_project", "project:delete"),))
async def test_permission_revocation_after_approval_request_blocks_write(tool_scope, tool, permission):
    db, _user, own, _foreign, links, executor, _client = tool_scope
    args = {"project_name": "C07 不应创建"} if tool == "create_project" else {"project_id": own.id}
    if tool == "update_project":
        args["project_name"] = "C07 不应更新"
    call = ToolCall("revoked-before-resume", tool, args, json.dumps(args))
    assert (await executor.execute(call)).status == "approval_required"
    db.delete(links[permission])
    db.commit()
    result = await executor.execute(call, approved=True)
    assert result.status == "error"
    assert permission in result.error
    assert db.get(Project, own.id).status == "active"
    assert db.get(Project, own.id).project_name == "C07 自有项目"
    assert db.query(Project).count() == 2
    assert db.query(ApprovalItem).one().status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ("create_project", "update_project"))
async def test_medium_project_write_auto_passes_and_executes_once(tool_scope, tool):
    db, _user, own, _foreign, _links, executor, _client = tool_scope
    args = {"project_name": "C07 自动创建"} if tool == "create_project" else {"project_id": own.id}
    if tool == "update_project":
        args["project_name"] = "C07 自动更新"
    call = ToolCall("medium-write", tool, args, json.dumps(args))
    result = await executor.execute(call)
    assert result.status == "success", result.error
    assert db.query(ApprovalItem).count() == 0
    if tool == "create_project":
        assert db.query(Project).count() == 3
    else:
        assert db.get(Project, own.id).project_name == "C07 自动更新"
    assert (await executor.execute(call)).status == "success"
    assert db.query(AgentToolExecution).count() == 1


@pytest.mark.asyncio
async def test_high_risk_project_delete_still_requires_approval(tool_scope):
    db, _user, own, _foreign, _links, executor, _client = tool_scope
    call = ToolCall("high-risk-delete", "delete_project", {"project_id": own.id}, "{}")
    assert (await executor.execute(call)).status == "approval_required"
    assert db.get(Project, own.id).status == "active"
    result = await executor.execute(call, approved=True)
    assert result.status == "success", result.error
    assert db.get(Project, own.id).status == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", ("ctx", "user", "user_id", "approved", "role"))
async def test_model_cannot_add_identity_or_approval_fields(tool_scope, forged):
    db, _user, _own, foreign, _links, executor, _client = tool_scope
    args = {"project_id": foreign.id, forged: {"user_id": 999, "role": "admin", "approved": True}}
    result = await executor.execute(ToolCall("forged", "get_project_detail", args, json.dumps(args)))
    assert result.status == "error"
    assert "参数校验失败" in result.error
    assert "PRIVATE_FOREIGN_PROJECT" not in json.dumps(result.output)
    assert db.query(AgentToolExecution).one().user_id == executor._user.id


@pytest.mark.asyncio
async def test_plain_account_forged_admin_release_call_never_creates_approval(tool_scope):
    db, _user, _own, _foreign, _links, executor, _client = tool_scope
    result = await executor.execute(ToolCall("admin-forged", "admin_decide_agent_release",
                                            {"approval_id": 239, "decision": "approve"}, ""))
    assert result.status == "error"
    assert "没有管理员工具权限" in result.error
    assert db.query(ApprovalItem).count() == 0


@pytest.mark.asyncio
async def test_foreign_project_stays_unreadable_even_with_injected_admin_claim(tool_scope):
    _db, _user, _own, foreign, _links, executor, _client = tool_scope
    # 模拟模型已经受诱导，直接猜测他人资源 ID；服务端仍使用真实 actor。
    result = await executor.execute(ToolCall("foreign", "get_project_detail", {"project_id": foreign.id}, ""))
    assert result.status == "error"
    assert "PRIVATE_FOREIGN_PROJECT" not in json.dumps(result.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_scope", ["reviewer", "admin"], indirect=True)
@pytest.mark.parametrize("surface", ["user", "admin"])
async def test_common_tools_use_real_actor_permissions_on_both_surfaces(tool_scope, surface):
    db, user, own, _foreign, links, _executor, client = tool_scope
    executor = PrismToolExecutor(db, user, surface=surface, run_id="role-surface",
                                 mcp_provider=EmptyMcp())
    for tool, permission, path in READ_TOOLS:
        args = read_arguments(tool, own)
        assert client.get(path, params=args).status_code == 200
        result = await executor.execute(ToolCall("role-" + tool, tool, args, ""))
        assert result.status == "success", result.error
    for link in links.values():
        db.delete(link)
    db.commit()
    schemas = {item["name"] for item in await executor.tool_schemas()}
    for tool, permission, path in READ_TOOLS:
        args = read_arguments(tool, own)
        route = client.get(path, params=args)
        result = await executor.execute(ToolCall("revoked-" + tool, tool, args, ""))
        if user.role == "admin":
            # 与 REST 相同：程序管理员拥有程序内权限，服务器权限另行校验。
            assert route.status_code == 200
            assert result.status == "success", result.error
            assert tool in schemas
        else:
            assert route.status_code == 403
            assert result.status == "error"
            assert permission in result.error
            assert tool not in schemas


@pytest.mark.asyncio
async def test_direct_capability_and_fixed_tool_both_enforce_same_project_permission(tool_scope):
    db, _user, _own, _foreign, links, executor, _client = tool_scope
    db.delete(links["project:view"])
    db.commit()
    for name, args in (("list_projects", {}),
                       ("user_execute_capability", {"capability": "projects.list", "params": {}})):
        result = await executor.execute(ToolCall(name, name, args, ""))
        assert result.status == "error"
        assert "project:view" in result.error
    assert db.query(ApprovalItem).count() == 0


@pytest.mark.asyncio
async def test_approval_identity_must_come_from_server_not_extra_model_argument(tool_scope):
    db, _user, _own, _foreign, _links, executor, _client = tool_scope
    args = {"project_name": "C07 非授权创建", "approved": True}
    result = await executor.execute(ToolCall("forge-create", "create_project", args, ""))
    assert result.status == "error"
    assert "参数校验失败" in result.error
    assert db.query(Project).count() == 2
    assert db.query(ApprovalItem).count() == 0


@pytest.mark.parametrize("tool,permission,path", READ_TOOLS)
@pytest.mark.parametrize("revoked", [True, False])
def test_legacy_planner_checks_same_account_action_permission(tool_scope, tool, permission, path, revoked):
    db, user, own, _foreign, links, executor, client = tool_scope
    args = read_arguments(tool, own)
    if revoked:
        db.delete(links[permission])
        db.commit()
    assert client.get(path, params=args).status_code == (403 if revoked else 200)
    result = executor._orch.chat_agent._execute_plan(
        [PlannerToolCall(tool_name=tool, arguments=args)],
        [{"role": "user", "content": "查询本人可见资源"}],
        AgentContext(user_id=user.id),
    )
    assert result.success is not revoked, result.error or result.data
    if revoked:
        assert permission in result.error


@pytest.mark.parametrize("intent,permission", (("list_code_files", "file:view"),
                                             ("create_project", "project:create")))
@pytest.mark.parametrize("revoked", [True, False])
def test_legacy_clarify_rechecks_permission_before_dispatch(tool_scope, monkeypatch, intent, permission, revoked):
    db, user, own, _foreign, links, executor, client = tool_scope
    monkeypatch.setattr(ClarifyStore, "_instance", ClarifyStore())
    payload = {} if intent == "list_code_files" else {"project_name": "C07 不应经追问创建"}
    pending = executor._orch.chat_agent._maybe_clarify(intent, payload, AgentContext(user_id=user.id))
    assert pending.success is True
    clarification = pending.data["clarify"]
    if revoked:
        db.delete(links[permission])
        db.commit()
    if intent == "list_code_files":
        answers = {"project_id": own.id}
        assert client.get("/code-files", params=answers).status_code == (403 if revoked else 200)
    else:
        answers = {executor._orch.chat_agent.WRITE_CONFIRMATION_KEY: "确认执行"}
        if revoked:
            assert client.post("/projects", json=payload).status_code == 403
    response = client.post("/agents/clarify", json={"clarify_id": clarification["clarify_id"],
                                                   "answers": answers})
    if revoked:
        assert response.status_code == 403, response.json()
        assert permission in response.json()["message"]
        assert db.query(Project).count() == 2
        assert ClarifyStore.instance().peek(clarification["clarify_id"]) is not None
        retried = client.post("/agents/clarify", json={"clarify_id": clarification["clarify_id"],
                                                       "answers": answers})
        assert retried.status_code == 403
        assert db.query(Project).count() == 2
    else:
        assert response.status_code == 200, response.json()
        assert "source.py" in response.text if intent == "list_code_files" else "项目已创建" in response.text
        assert db.query(Project).count() == (2 if intent == "list_code_files" else 3)
        assert ClarifyStore.instance().peek(clarification["clarify_id"]) is None


@pytest.mark.parametrize("tool,permission,path", READ_TOOLS)
def test_direct_orchestrator_reads_recheck_permission(tool_scope, tool, permission, path):
    db, _user, own, _foreign, links, executor, _client = tool_scope
    args = read_arguments(tool, own)
    method = getattr(executor._orch, tool)
    assert method(**args).success is True
    db.delete(links[permission])
    db.commit()
    denied = method(**args)
    assert denied.success is False
    assert permission in denied.error


@pytest.mark.parametrize("tool,permission", (("create_project", "project:create"),
                                           ("delete_project", "project:delete"),
                                           ("update_project", "project:update"),
                                           ("start_review", "review:start"),
                                           ("get_project_detail", "project:view")))
def test_direct_orchestrator_cannot_bypass_revocation(tool_scope, tool, permission):
    db, _user, own, _foreign, links, executor, _client = tool_scope
    db.delete(links[permission])
    db.commit()
    args = {"project_name": "C07 禁止直调创建"} if tool == "create_project" else {"project_id": own.id}
    if tool == "update_project":
        args["project_name"] = "C07 禁止直调修改"
    result = getattr(executor._orch, tool)(**args)
    assert result.success is False
    assert permission in result.error
    assert db.query(Project).count() == 2
    assert own.project_name == "C07 自有项目"
    assert own.status == "active"
    assert db.query(ReviewTask).count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_project_detail", "update_project", "delete_project"])
async def test_direct_and_responses_keep_foreign_project_object_boundary(tool_scope, tool):
    db, _user, _own, foreign, _links, executor, _client = tool_scope
    args = {"project_id": foreign.id}
    if tool == "update_project":
        args["project_name"] = "C07 不得修改他人项目"
    direct = getattr(executor._orch, tool)(**args)
    assert direct.success is False
    call = ToolCall("foreign-object-" + tool, tool, args, "")
    responses = await executor.execute(call)
    if tool == "delete_project":
        assert responses.status == "approval_required"
        responses = await executor.execute(call, approved=True)
    assert responses.status == "error"
    assert "PRIVATE_FOREIGN_PROJECT" not in json.dumps(responses.output)
    assert foreign.status == "active"
    assert foreign.project_name == "PRIVATE_FOREIGN_PROJECT"
    assert db.query(Project).count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,permission,path", READ_TOOLS)
async def test_same_call_id_cannot_replay_success_after_permission_revocation(tool_scope, tool, permission, path):
    db, _user, own, _foreign, links, executor, _client = tool_scope
    args = read_arguments(tool, own)
    call = ToolCall("same-call", tool, args, json.dumps(args))
    assert (await executor.execute(call)).status == "success"
    db.delete(links[permission])
    db.commit()
    events = []

    async def sink(event):
        events.append(event)

    executor._event_sink = sink
    replay = await executor.execute(call)
    assert replay.status == "error", replay.output
    assert permission in replay.error
    # 既有成功账本保留为历史，但本轮不得重新发出成功内容。
    assert db.query(AgentToolExecution).one().status == "success"
    assert all(event["type"] != "response.tool.completed" for event in events)
