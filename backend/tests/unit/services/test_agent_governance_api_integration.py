"""Agent 治理管理端 API 集成测试。"""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user, require_admin, require_super_admin
from app.core.security import create_access_token
from app.main import app
from app.models.agent_governance import AgentAlert, AgentJob, AgentJobRun, ApprovalItem
from app.models.agent_mesh import AgentMeshConversation
from app.models.agent_response_run import AgentResponseRun
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services import agent_responses_service
from app.services.agent_responses_service import DatabaseCheckpointStore, PrismToolExecutor
from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime, RunCheckpoint, ToolCall


@pytest.fixture
def admin_api_client(monkeypatch):
    """创建共享内存 SQLite 的管理端 API 测试客户端。

    Yields:
        tuple[TestClient, Session]: 测试客户端和数据库会话。
    """
    # 权限/业务测试不启动应用 lifespan；调度不可用的拒绝行为另有专项覆盖。
    monkeypatch.setattr(settings, "agent_governance_scheduler_enabled", False)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    admin = User(
        id=1,
        username="admin",
        password="x",
        email="admin@example.com",
        nickname="管理员",
        role="admin",
        status=1,
    )
    session.add(admin)
    session.commit()

    def override_db():
        """覆盖 FastAPI 数据库依赖。

        Yields:
            Session: 测试数据库会话。
        """
        yield session

    def override_admin():
        """覆盖管理员权限依赖。

        Returns:
            User: 管理员用户。
        """
        return admin

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_admin] = override_admin
    app.dependency_overrides[require_super_admin] = override_admin
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(require_super_admin, None)
        session.close()
        engine.dispose()


def _ok(client: TestClient, method: str, url: str, **kwargs):
    """调用管理端 API 并断言统一响应成功。

    Args:
        client: TestClient 实例。
        method: HTTP 方法。
        url: 请求路径。
        kwargs: 请求参数。

    Returns:
        object: 响应 data 字段。
    """
    response = getattr(client, method)(url, **kwargs)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]


def test_response_approval_exposes_only_owned_session_trace_and_redacted_request(admin_api_client, monkeypatch):
    client, db = admin_api_client
    owner = db.get(User, 1)
    other = User(
        id=2,
        username="other-admin",
        password="x",
        email="other-admin@example.com",
        nickname="其他管理员",
        role="admin",
        status=1,
    )
    db.add(other)
    owned_run = AgentResponseRun(
        run_id="approval-source-run",
        user_id=owner.id,
        surface="admin",
        session_key="admin-session-owned",
        status="waiting_approval",
        checkpoint_json="{}",
    )
    db.add(owned_run)
    db.add(AgentResponseRun(
        run_id="foreign-surface-run",
        user_id=owner.id,
        surface="user",
        session_key="user-session-must-not-link",
        status="waiting_approval",
        checkpoint_json="{}",
    ))
    db.add(AgentResponseRun(
        run_id="foreign-owner-run",
        user_id=2,
        surface="admin",
        session_key="other-admin-session-must-not-link",
        status="waiting_approval",
        checkpoint_json="{}",
    ))
    call_id = "call-1"
    request_id = hashlib.sha256(f"responses:approval-source-run:{call_id}".encode()).hexdigest()
    item = ApprovalItem(
        title="全局模型配置请求",
        agent_code="manager",
        action="responses.admin_execute_capability",
        resource="response_run:approval-source-run",
        risk_level="critical",
        status="pending",
        request_json=(
            f'{{"owner_user_id":1,"run_id":"approval-source-run","call_id":"{call_id}",'
            '"tool":"admin_execute_capability","arguments":{"capability":"governance.agents.list"},'
            '"preview":{"api_key":"should-never-be-returned"}}'
        ),
        copilot_request_id=request_id,
    )
    db.add(item)
    db.flush()
    owned_run.checkpoint_json = json.dumps({
        "run_id": "approval-source-run",
        "status": "waiting_approval",
        "pending": {
            "kind": "approval",
            "approval_id": item.id,
            "call": {
                "call_id": call_id,
                "name": "admin_execute_capability",
                "arguments": {"capability": "governance.agents.list"},
            },
        },
    })

    def response_approval(*, title, run_id, call_id, tool="admin_execute_capability", action=None,
                          resource=None, owner_id=1, request_id_override="computed", arguments=None):
        """Create a persisted Responses approval fixture with the runtime correlation hash."""
        return ApprovalItem(
            title=title,
            action=action or f"responses.{tool}",
            resource=resource or f"response_run:{run_id}",
            risk_level="high",
            status="pending",
            request_json=json.dumps({
                "owner_user_id": owner_id,
                "run_id": run_id,
                "call_id": call_id,
                "tool": tool,
                "arguments": arguments or {"capability": "governance.agents.list"},
            }),
            copilot_request_id=(
                hashlib.sha256(f"responses:{run_id}:{call_id}".encode()).hexdigest()
                if request_id_override == "computed"
                else request_id_override
            ),
        )

    def bind_pending_approval(
        *,
        title,
        run_id,
        call_id,
        request_arguments=None,
        checkpoint_run_id=None,
        checkpoint_approval_id=None,
        checkpoint_arguments=None,
    ):
        """Persist an approval and its runtime-shaped waiting checkpoint."""
        run = AgentResponseRun(
            run_id=run_id,
            user_id=owner.id,
            surface="admin",
            session_key=f"session-{run_id}",
            status="waiting_approval",
            checkpoint_json="{}",
        )
        db.add(run)
        approval = response_approval(
            title=title,
            run_id=run_id,
            call_id=call_id,
            arguments=request_arguments or {"capability": "governance.agents.list"},
        )
        db.add(approval)
        db.flush()
        run.checkpoint_json = json.dumps({
            "run_id": checkpoint_run_id or run_id,
            "status": "waiting_approval",
            "pending": {
                "kind": "approval",
                "approval_id": (
                    approval.id if checkpoint_approval_id is None else checkpoint_approval_id
                ),
                "call": {
                    "call_id": call_id,
                    "name": "admin_execute_capability",
                    "arguments": checkpoint_arguments or {
                        "capability": "governance.agents.list",
                    },
                },
            },
        })
        return approval

    mismatched_checkpoint_run_id_item = bind_pending_approval(
        title="检查点运行 ID 不匹配",
        run_id="checkpoint-run-id-mismatch",
        call_id="call-run-id-mismatch",
        checkpoint_run_id="different-checkpoint-run",
    )
    mismatched_approval_id_item = bind_pending_approval(
        title="检查点审批 ID 不匹配",
        run_id="checkpoint-approval-id-mismatch",
        call_id="call-approval-id-mismatch",
        checkpoint_approval_id=-1,
    )
    mismatched_arguments_item = bind_pending_approval(
        title="检查点参数不匹配",
        run_id="checkpoint-arguments-mismatch",
        call_id="call-arguments-mismatch",
        request_arguments={"capability": "governance.agents.list", "page": 2},
        checkpoint_arguments={"capability": "governance.agents.list", "page": 1},
    )
    different_arguments_item = bind_pending_approval(
        title="独立审批参数应独立核验",
        run_id="different-approval-arguments",
        call_id="call-different-arguments",
        request_arguments={"capability": "governance.agents.list", "page": 2},
        checkpoint_arguments={"capability": "governance.agents.list", "page": 2},
    )
    mismatched_json_scalar_types_item = bind_pending_approval(
        title="JSON 布尔值与整数不可互换",
        run_id="checkpoint-json-type-mismatch",
        call_id="call-json-type-mismatch",
        request_arguments={"capability": "governance.agents.list", "enabled": True},
        checkpoint_arguments={"capability": "governance.agents.list", "enabled": 1},
    )

    mismatched_trace_item = ApprovalItem(
        title="资源和运行标识不一致",
        action="responses.admin_execute_capability",
        resource="response_run:different-run",
        risk_level="high",
        status="pending",
        request_json=(
            '{"owner_user_id":1,"run_id":"approval-source-run","call_id":"call-2",'
            '"tool":"admin_execute_capability","arguments":{"capability":"governance.agents.list"}}'
        ),
    )
    db.add(mismatched_trace_item)
    unbound_trace_item = ApprovalItem(
        title="缺少唯一调用绑定",
        action="responses.admin_execute_capability",
        resource="response_run:approval-source-run",
        risk_level="high",
        status="pending",
        request_json=(
            '{"owner_user_id":1,"run_id":"approval-source-run","call_id":"call-3",'
            '"tool":"admin_execute_capability","arguments":{"capability":"governance.agents.list"}}'
        ),
    )
    db.add(unbound_trace_item)
    mismatched_call_item = response_approval(
        title="检查点调用 ID 不匹配", run_id="approval-source-run", call_id="call-4"
    )
    db.add(mismatched_call_item)
    mismatched_tool_item = response_approval(
        title="检查点工具名不匹配", run_id="approval-source-run",
        call_id="call-5", tool="admin_list_agents"
    )
    db.add(mismatched_tool_item)
    mismatched_action_item = ApprovalItem(
        title="审批动作与工具名不一致",
        action="responses.user_execute_capability",
        resource="response_run:approval-source-run",
        risk_level="high",
        status="pending",
        request_json=(
            '{"owner_user_id":1,"run_id":"approval-source-run","call_id":"call-5",'
            '"tool":"admin_execute_capability","arguments":{"capability":"governance.agents.list"}}'
        ),
    )
    db.add(mismatched_action_item)
    malformed_checkpoint_run = AgentResponseRun(
        run_id="malformed-checkpoint-run",
        user_id=owner.id,
        surface="admin",
        session_key="malformed-checkpoint-session",
        status="waiting_approval",
        checkpoint_json="[]",
    )
    db.add(malformed_checkpoint_run)
    malformed_checkpoint_item = response_approval(
        title="检查点格式损坏", run_id="malformed-checkpoint-run", call_id="call-7"
    )
    db.add(malformed_checkpoint_item)
    for run_id in ("foreign-surface-run", "foreign-owner-run"):
        db.add(response_approval(
            title=f"不得跨账号或跨入口跳转：{run_id}",
            run_id=run_id,
            call_id=f"call-{run_id}",
        ))

    monkeypatch.setattr(
        agent_responses_service,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: object(),
    )
    runtime_run_id = "approval-created-through-runtime"
    runtime_session_key = "runtime-created-approval-session"
    db.add(AgentMeshConversation(
        user_id=owner.id,
        surface="admin",
        session_key=runtime_session_key,
        title="待审批来源集成样本",
        status="active",
        last_seen_at=datetime.now(timezone.utc),
    ))
    db.commit()
    checkpoint_store = DatabaseCheckpointStore(
        db,
        user_id=owner.id,
        surface="admin",
        session_key=runtime_session_key,
    )
    initial_checkpoint = RunCheckpoint(
        run_id=runtime_run_id,
        model="test-model",
        transcript=[],
        tools=[],
    )
    assert asyncio.run(checkpoint_store.create(initial_checkpoint)) is True
    runtime_executor = PrismToolExecutor(
        db,
        owner,
        surface="admin",
        run_id=runtime_run_id,
        session_key=runtime_session_key,
        mcp_provider=object(),
    )
    secret_argument = "runtime-only-secret-should-not-persist"
    runtime_call = ToolCall(
        "runtime-call-id",
        "admin_execute_capability",
        {
            "capability": "governance.agents.list",
            "params": {"api_key": secret_argument},
        },
        json.dumps({
            "capability": "governance.agents.list",
            "params": {"api_key": secret_argument},
        }),
    )

    class RuntimeApprovalExecutor:
        async def execute(self, call, *, approved=False):
            assert approved is False
            return runtime_executor._approval(
                call,
                danger=True,
                operation="只读 Agent 列表",
                impact="等待管理员审批",
                preview={"message": "等待管理员审批"},
            )

    runtime = DeepSeekResponsesRuntime(
        transport=object(),
        tool_executor=RuntimeApprovalExecutor(),
        checkpoint_store=checkpoint_store,
        stream=False,
    )
    runtime_result = asyncio.run(runtime._process_calls(
        RunCheckpoint(
            run_id=runtime_run_id,
            model="test-model",
            transcript=[],
            tools=[],
        ),
        [runtime_call],
    ))
    assert runtime_result.status == "waiting_approval"
    runtime_approval_id = int(runtime_result.pending["approval_id"])
    db.commit()

    listed = _ok(client, "get", "/api/admin/approvals", params={"status": "pending"})
    for verified_item, expected_run_id in (
        (item, "approval-source-run"),
        (different_arguments_item, "different-approval-arguments"),
    ):
        result = next(row for row in listed if row["id"] == verified_item.id)
        assert result["source_trace_status"] == "verified"
        assert result["source_run_id"] == expected_run_id
        assert result["source_session_id"]
        assert result["source_tool_name"] == "admin_execute_capability"
        assert result["requires_session_resume"] is True
    runtime_result_item = next(row for row in listed if row["id"] == runtime_approval_id)
    assert runtime_result_item["source_trace_status"] == "verified"
    assert runtime_result_item["source_run_id"] == runtime_run_id
    assert runtime_result_item["source_session_id"] == runtime_session_key
    assert secret_argument not in str(runtime_result_item["request_json"])
    assert "should-never-be-returned" not in str(
        next(row for row in listed if row["id"] == item.id)["request_json"]
    )
    mismatch = next(row for row in listed if row["id"] == mismatched_trace_item.id)
    assert mismatch["source_trace_status"] == "unavailable"
    assert mismatch["source_session_id"] is None
    for unbound in (
        unbound_trace_item,
        mismatched_call_item,
        mismatched_tool_item,
        mismatched_action_item,
        malformed_checkpoint_item,
        mismatched_checkpoint_run_id_item,
        mismatched_approval_id_item,
        mismatched_arguments_item,
        mismatched_json_scalar_types_item,
    ):
        result = next(row for row in listed if row["id"] == unbound.id)
        assert result["source_trace_status"] == "unavailable"
        assert result["source_session_id"] is None
    for run_id in ("foreign-surface-run", "foreign-owner-run"):
        foreign = next(row for row in listed if row["resource"] == f"response_run:{run_id}")
        assert foreign["source_trace_status"] == "unavailable"
        assert foreign["source_session_id"] is None

    app.dependency_overrides[require_admin] = lambda: other
    try:
        other_items = _ok(client, "get", "/api/admin/approvals", params={"status": "pending"})
    finally:
        app.dependency_overrides[require_admin] = lambda: owner
    assert item.id not in {row["id"] for row in other_items}


def test_frontend_admin_governance_api_paths_match_backend_routes():
    """验证前端管理端 API 封装均接入后端真实路由。"""
    frontend_api = Path(__file__).resolve().parents[4] / "frontend/src/api/adminGovernance.ts"
    source = frontend_api.read_text(encoding="utf-8")
    frontend_calls: set[tuple[str, str]] = set()
    for match in re.finditer(r"(get|post|put)<[^>]+>\((`[^`]+`|'[^']+'|\"[^\"]+\")", source):
        method = match.group(1).upper()
        path = match.group(2)[1:-1]
        path = re.sub(r"\$\{[^}]+\}", "{param}", path)
        frontend_calls.add((method, path))

    backend_routes: set[tuple[str, str]] = set()
    for path, path_item in app.openapi().get("paths", {}).items():
        if not path.startswith("/api/admin"):
            continue
        normalized_path = re.sub(r"\{[^}]+\}", "{param}", path[4:])
        for method in path_item:
            normalized_method = method.upper()
            if normalized_method in {"GET", "POST", "PUT", "DELETE"}:
                backend_routes.add((normalized_method, normalized_path))

    missing = sorted(frontend_calls - backend_routes)
    assert not missing


def test_external_knowledge_source_api_requires_unique_super_admin(monkeypatch):
    """普通管理员可读来源，但不能保存或抓取；唯一 admin 超管可执行。"""
    monkeypatch.setattr(settings, "agent_governance_scheduler_enabled", False)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    ordinary = User(
        username="manager",
        password="x",
        email="manager@example.com",
        nickname="普通管理员",
        role="admin",
        status=1,
        token_version=0,
    )
    super_user = User(
        username="admin",
        password="x",
        email="admin@example.com",
        nickname="超级管理员",
        role="super_admin",
        status=1,
        token_version=0,
    )
    super_role = Role(name="超级管理员", code="super_admin", status="active", is_builtin=1)
    session.add_all([ordinary, super_user, super_role])
    session.flush()
    session.add(UserRole(user_id=super_user.id, role_id=super_role.id))
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    payload = {
        "agent_code": "manager",
        "source_type": "inline",
        "source_uri": "安全知识",
        "whitelist": 1,
        "enabled": 1,
        "config_json": {"content": "只有超管可修改来源"},
    }
    try:
        client = TestClient(app)
        ordinary_token = create_access_token(ordinary.id, ordinary.role, ordinary.token_version)
        super_token = create_access_token(super_user.id, super_user.role, super_user.token_version)

        readable = client.get(
            "/api/admin/governance/knowledge/sources",
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        denied_upsert = client.post(
            "/api/admin/governance/knowledge/sources",
            json=payload,
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        denied_crawl = client.post(
            "/api/admin/governance/knowledge/crawl",
            params={"agent_code": "manager"},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        allowed = client.post(
            "/api/admin/governance/knowledge/sources",
            json=payload,
            headers={"Authorization": f"Bearer {super_token}"},
        )

        ordinary_jobs = client.get(
            "/api/admin/jobs",
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        super_jobs = client.get(
            "/api/admin/jobs",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        crawl_job = next(item for item in super_jobs.json()["data"] if item["job_type"] == "crawl")
        crawl_row = session.get(AgentJob, crawl_job["id"])
        runs_before = session.query(AgentJobRun).filter(AgentJobRun.job_id == crawl_job["id"]).count()
        denied_job_update = client.put(
            f"/api/admin/jobs/{crawl_job['id']}",
            json={"status": "disabled"},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        denied_job_run = client.post(
            f"/api/admin/jobs/{crawl_job['id']}/run",
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        session.refresh(crawl_row)
        status_after_denied_update = crawl_row.status
        runs_after_denied_run = session.query(AgentJobRun).filter(AgentJobRun.job_id == crawl_job["id"]).count()
        monkeypatch.setattr(
            "app.services.scheduler_service._execute_job",
            lambda _db, _job: {"doc_count": 1},
        )
        allowed_job_update = client.put(
            f"/api/admin/jobs/{crawl_job['id']}",
            json={"status": "disabled"},
            headers={"Authorization": f"Bearer {super_token}"},
        )
        allowed_job_run = client.post(
            f"/api/admin/jobs/{crawl_job['id']}/run",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        ordinary_job_runs = client.get(
            "/api/admin/jobs/runs",
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        super_job_runs = client.get(
            "/api/admin/jobs/runs",
            headers={"Authorization": f"Bearer {super_token}"},
        )

        sensitive_approval = ApprovalItem(
            title="服务器操作审批",
            action="operations.restart_service",
            resource="production",
            risk_level="critical",
            status="pending",
            decision="escalate",
        )
        program_approval = ApprovalItem(
            title="程序内容审批",
            action="project.update",
            resource="project:1",
            risk_level="high",
            status="pending",
            decision="escalate",
        )
        session.add_all([sensitive_approval, program_approval])
        session.commit()
        ordinary_approvals = client.get(
            "/api/admin/approvals",
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        super_approvals = client.get(
            "/api/admin/approvals",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        ordinary_overview = client.get(
            "/api/admin/governance/overview",
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        super_overview = client.get(
            "/api/admin/governance/overview",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        denied_sensitive_approval = client.post(
            f"/api/admin/approvals/{sensitive_approval.id}/approve",
            json={"note": "越权尝试"},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        denied_sensitive_rejection = client.post(
            f"/api/admin/approvals/{sensitive_approval.id}/reject",
            json={"note": "越权尝试"},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )

        assert readable.status_code == 200
        assert denied_upsert.status_code == 403
        assert denied_upsert.json()["code"] == 40322
        assert denied_crawl.status_code == 403
        assert denied_crawl.json()["code"] == 40322
        assert allowed.status_code == 200
        assert allowed.json()["data"]["source_type"] == "inline"
        assert ordinary_jobs.status_code == 200
        assert super_jobs.status_code == 200
        assert {item["job_type"] for item in ordinary_jobs.json()["data"]}.isdisjoint({"crawl", "ops_health_check"})
        assert {"crawl", "ops_health_check"}.issubset({item["job_type"] for item in super_jobs.json()["data"]})
        assert denied_job_update.status_code == 403
        assert denied_job_update.json()["code"] == 40322
        assert denied_job_run.status_code == 403
        assert denied_job_run.json()["code"] == 40322
        assert status_after_denied_update == "enabled"
        assert runs_after_denied_run == runs_before
        assert allowed_job_update.status_code == 200
        assert allowed_job_run.status_code == 200
        assert allowed_job_run.json()["data"]["job_id"] == crawl_job["id"]
        assert all(item["job_id"] != crawl_job["id"] for item in ordinary_job_runs.json()["data"])
        assert any(item["job_id"] == crawl_job["id"] for item in super_job_runs.json()["data"])
        assert ordinary_approvals.status_code == 200
        assert super_approvals.status_code == 200
        assert ordinary_overview.status_code == 200
        assert super_overview.status_code == 200
        assert ordinary_overview.json()["data"]["approvals_pending"] == 1
        assert super_overview.json()["data"]["approvals_pending"] == 2
        assert sensitive_approval.id not in {item["id"] for item in ordinary_approvals.json()["data"]}
        assert program_approval.id in {item["id"] for item in ordinary_approvals.json()["data"]}
        assert sensitive_approval.id in {item["id"] for item in super_approvals.json()["data"]}
        assert denied_sensitive_approval.status_code == 403
        assert denied_sensitive_approval.json()["code"] == 40322
        assert denied_sensitive_rejection.status_code == 403
        assert denied_sensitive_rejection.json()["code"] == 40322
        session.refresh(sensitive_approval)
        assert sensitive_approval.status == "pending"
        assert sensitive_approval.decided_by is None
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()


def test_admin_governance_api_business_loop(admin_api_client):
    """验证管理端治理 API 已真实接入端点并形成业务闭环。"""
    client, db = admin_api_client

    agents = _ok(client, "get", "/api/admin/governance/agents")
    assert any(agent["code"] == "manager" for agent in agents)
    overview = _ok(client, "get", "/api/admin/governance/overview")
    assert overview["agents_total"] >= 1

    manager = _ok(
        client,
        "put",
        "/api/admin/governance/agents/manager",
        json={"priority": 77, "auto_approval_threshold": 0.8},
    )
    assert manager["priority"] == 77

    memory = _ok(
        client,
        "post",
        "/api/admin/governance/agents/manager/memory",
        json={"title": "验证记忆", "content": "运行时集成验证", "memory_type": "reflection", "weight": 1},
    )
    assert memory["agent_code"] == "manager"

    source = _ok(
        client,
        "post",
        "/api/admin/governance/knowledge/sources",
        json={
            "agent_code": "manager",
            "source_type": "inline",
            "source_uri": "闭环知识",
            "whitelist": 1,
            "enabled": 1,
            "config_json": {"content": "治理知识抓取验证"},
        },
    )
    assert source["agent_code"] == "manager"
    crawl = _ok(client, "post", "/api/admin/governance/knowledge/crawl", params={"agent_code": "manager"})
    assert crawl["doc_count"] >= 1

    risk_doc = _ok(
        client,
        "post",
        "/api/admin/governance/knowledge/docs",
        json={
            "agent_code": "manager",
            "title": "高风险验证知识",
            "content": "需要审批后生效",
            "source_type": "manual",
            "risk_level": "high",
            "confidence": 0.3,
        },
    )
    assert risk_doc["status"] == "pending_approval"
    approvals = _ok(client, "get", "/api/admin/approvals")
    pending = next(item for item in approvals if item["resource"] == f"agent_knowledge_doc:{risk_doc['id']}")
    approved = _ok(client, "post", f"/api/admin/approvals/{pending['id']}/approve", json={"note": "通过"})
    assert approved["status"] == "approved"

    policy = _ok(
        client,
        "post",
        "/api/admin/policies",
        json={
            "rule_code": "integration_allow",
            "name": "集成验证策略",
            "subject": "agent:*",
            "action": "knowledge.read",
            "resource": "*",
            "effect": "allow",
            "risk_level": "low",
            "condition_json": {},
            "priority": 1,
            "enabled": 1,
        },
    )
    assert policy["rule_code"] == "integration_allow"
    decision = _ok(
        client,
        "post",
        "/api/admin/policies/evaluate",
        json={"subject": "agent:manager", "action": "knowledge.read", "resource": "agent:manager", "context": {}},
    )
    assert decision["decision"] == "allow"

    permission = _ok(
        client,
        "post",
        "/api/admin/tools/permissions",
        json={
            "agent_code": "manager",
            "tool_code": "shell",
            "permission": "deny",
            "risk_level": "critical",
            "enabled": 1,
            "note": "集成验证",
        },
    )
    assert permission["permission"] == "deny"
    assert _ok(client, "get", "/api/admin/tools/permissions")

    jobs = _ok(client, "get", "/api/admin/jobs")
    assert len(jobs) >= 3
    reflection_job = next(item for item in jobs if item["job_type"] == "reflection")
    job = _ok(
        client,
        "put",
        f"/api/admin/jobs/{reflection_job['id']}",
        json={"schedule": "daily@02:10", "status": "enabled"},
    )
    assert job["schedule"] == "daily@02:10"
    run = _ok(client, "post", f"/api/admin/jobs/{reflection_job['id']}/run")
    assert run["status"] in {"success", "failed"}

    alert = AgentAlert(alert_type="integration", severity="warning", status="open", title="集成告警")
    db.add(alert)
    db.commit()
    alerts = _ok(client, "get", "/api/admin/observability/alerts")
    assert any(item["title"] == "集成告警" for item in alerts)
    resolved = _ok(client, "post", f"/api/admin/observability/alerts/{alert.id}/resolve", json={"note": "已处理"})
    assert resolved["status"] == "resolved"

    reward = _ok(
        client,
        "post",
        "/api/admin/rewards/events",
        json={"agent_code": "manager", "event_type": "reward", "score": 2, "reason": "集成验证奖励"},
    )
    assert reward["score"] == 2
    version = _ok(
        client,
        "post",
        "/api/admin/rollback/versions",
        json={
            "agent_code": "manager",
            "artifact_type": "prompt",
            "version": "it-v1",
            "content": "prompt",
            "snapshot": "prompt",
            "status": "stable",
        },
    )
    rolled = _ok(client, "post", f"/api/admin/rollback/versions/{version['id']}/rollback")
    assert rolled["status"] == "rolled_back"


def test_alert_pagination_reports_full_total_and_preserves_legacy_list(admin_api_client):
    """旧列表继续兼容，新分页 API 应覆盖超过 100 条的完整告警集合。"""
    client, db = admin_api_client
    db.add_all(
        [
            AgentAlert(alert_type="pagination", severity="warning", status="open", title=f"告警 {index}")
            for index in range(105)
        ]
    )
    db.add_all(
        [
            AgentAlert(alert_type="pagination", severity="info", status="resolved", title=f"已关闭告警 {index}")
            for index in range(4)
        ]
    )
    db.commit()

    legacy_rows = _ok(client, "get", "/api/admin/observability/alerts")
    first_page = _ok(
        client,
        "get",
        "/api/admin/observability/alerts/page",
        params={"status": "open", "page": 1, "page_size": 50},
    )
    second_page = _ok(
        client,
        "get",
        "/api/admin/observability/alerts/page",
        params={"status": "open", "page": 2, "page_size": 50},
    )
    third_page = _ok(
        client,
        "get",
        "/api/admin/observability/alerts/page",
        params={"status": "open", "page": 3, "page_size": 50},
    )
    resolved_page = _ok(
        client,
        "get",
        "/api/admin/observability/alerts/page",
        params={"status": "resolved", "page": 1, "page_size": 20},
    )

    assert db.query(AgentAlert).filter(AgentAlert.status == "open").count() == 105
    assert len(legacy_rows) == 100
    assert first_page["total"] == 105
    assert first_page["page"] == 1
    assert first_page["page_size"] == 50
    assert first_page["pages"] == 3
    assert len(first_page["items"]) == 50
    assert first_page["items"][0]["id"] > first_page["items"][-1]["id"]
    assert second_page["total"] == 105
    assert second_page["page"] == 2
    assert len(second_page["items"]) == 50
    assert third_page["total"] == 105
    assert third_page["page"] == 3
    assert len(third_page["items"]) == 5
    paged_ids = [row["id"] for page in (first_page, second_page, third_page) for row in page["items"]]
    db_rows = db.query(AgentAlert).filter(AgentAlert.status == "open").order_by(AgentAlert.id.desc()).all()
    db_ids = [row.id for row in db_rows]
    assert len(paged_ids) == 105
    assert len(set(paged_ids)) == 105
    assert paged_ids == db_ids
    assert resolved_page["total"] == 4
    assert len(resolved_page["items"]) == 4


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
def test_alert_pagination_rejects_invalid_bounds(admin_api_client, params):
    client, _ = admin_api_client
    response = client.get("/api/admin/observability/alerts/page", params=params)
    # 应用的统一参数校验处理器把 FastAPI 422 映射为业务 HTTP 400。
    assert response.status_code == 400


def test_alert_pagination_keeps_admin_authorization(admin_api_client):
    client, db = admin_api_client
    reviewer = User(
        username="alert-page-reviewer",
        password="x",
        email="alert-page-reviewer@example.com",
        nickname="审查员",
        role="reviewer",
        status=1,
    )
    db.add(reviewer)
    db.commit()

    admin_override = app.dependency_overrides.pop(require_admin)
    app.dependency_overrides[get_current_user] = lambda: reviewer
    try:
        response = client.get("/api/admin/observability/alerts/page")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides[require_admin] = admin_override

    assert response.status_code == 403


def test_generic_approval_api_can_filter_agent_release_items(admin_api_client):
    """审批中心的通用页签可按动作过滤专用 Agent 发布单。"""
    client, db = admin_api_client
    generic = ApprovalItem(
        title="通用知识审批",
        action="project.update",
        resource="project:1",
        risk_level="high",
        status="pending",
        decision="escalate",
    )
    release = ApprovalItem(
        title="专用发布审批",
        action="agent_package.publish",
        resource="custom_agent_version:1",
        risk_level="high",
        status="pending",
        decision="escalate",
        request_json="{}",
    )
    db.add_all([generic, release])
    db.commit()

    listed = _ok(
        client,
        "get",
        "/api/admin/approvals",
        params={"exclude_action": "agent_package.publish"},
    )
    assert [item["id"] for item in listed] == [generic.id]


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize(
    "payload_run_id,resource_run_id",
    [
        ("run_waiting_for_global_config_approval", "run_waiting_for_global_config_approval"),
        ("", "run_waiting_for_global_config_approval"),
        ("missing_run_in_payload", "run_waiting_for_global_config_approval"),
        ("run_waiting_for_global_config_approval", "missing_run_in_resource"),
    ],
)
def test_generic_approval_api_does_not_detach_response_run_from_its_approval(
    admin_api_client,
    decision,
    payload_run_id,
    resource_run_id,
):
    """Responses 写操作必须通过原会话恢复，通用审批不能只改审批行状态。"""
    client, session = admin_api_client
    admin = session.get(User, 1)
    admin.role = "super_admin"
    super_admin_role = Role(name="超级管理员", code="super_admin", status="active")
    session.add(super_admin_role)
    session.commit()
    session.add(UserRole(user_id=admin.id, role_id=super_admin_role.id))
    session.commit()

    run = AgentResponseRun(
        run_id="run_waiting_for_global_config_approval",
        user_id=admin.id,
        surface="admin",
        session_key="admin-session-approval-1",
        status="waiting_approval",
        checkpoint_json=json.dumps(
            {
                "status": "waiting_approval",
                "pending": {
                    "call": {
                        "call_id": "call_global_config_update",
                        "name": "admin_execute_capability",
                        "arguments": {"capability": "llm.config.update", "params": {"model": "deepseek-flash"}},
                    },
                },
            }
        ),
        version=1,
    )
    approval = ApprovalItem(
        title="更新全局 LLM 配置",
        action="responses.admin_execute_capability",
        resource=f"response_run:{resource_run_id}",
        risk_level="critical",
        status="pending",
        decision="escalate",
        request_json=json.dumps(
            {
                "owner_user_id": admin.id,
                "run_id": payload_run_id,
                "call_id": "call_global_config_update",
                "tool": "admin_execute_capability",
                "arguments": {"capability": "llm.config.update", "params": {"model": "deepseek-flash"}},
            }
        ),
    )
    session.add_all([run, approval])
    session.commit()

    listed = _ok(client, "get", "/api/admin/approvals", params={"status": "pending"})
    listed_item = next(item for item in listed if item["id"] == approval.id)
    assert listed_item["requires_session_resume"] is True

    response = client.post(f"/api/admin/approvals/{approval.id}/{decision}", json={"note": "确认"})

    session.refresh(approval)
    session.refresh(run)
    assert approval.status == "pending"
    assert run.status == "waiting_approval"
    assert response.status_code == 400


def test_generic_approval_api_keeps_legacy_response_items_without_runs_available(admin_api_client):
    """旧式/无会话 Responses 审批仍由通用审批流处理。"""
    client, session = admin_api_client
    admin = session.get(User, 1)
    approval = ApprovalItem(
        title="保存知识条目",
        action="responses.save_knowledge_note",
        resource="knowledge_note:legacy-item",
        risk_level="medium",
        status="pending",
        decision="escalate",
        request_json=json.dumps({"owner_user_id": admin.id, "note": "legacy request"}),
    )
    session.add(approval)
    session.commit()

    listed = _ok(client, "get", "/api/admin/approvals", params={"status": "pending"})
    listed_item = next(item for item in listed if item["id"] == approval.id)
    assert listed_item["requires_session_resume"] is False

    response = client.post(f"/api/admin/approvals/{approval.id}/approve", json={"note": "兼容旧事项"})

    session.refresh(approval)
    assert response.status_code == 200
    assert approval.status == "approved"
