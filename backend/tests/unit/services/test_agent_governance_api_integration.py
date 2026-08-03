"""Agent 治理管理端 API 集成测试。"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import require_admin, require_super_admin
from app.core.security import create_access_token
from app.main import app
from app.models.agent_governance import AgentAlert, AgentJob, AgentJobRun, ApprovalItem
from app.models.rbac import Role, UserRole
from app.models.user import User


@pytest.fixture
def admin_api_client():
    """创建共享内存 SQLite 的管理端 API 测试客户端。

    Yields:
        tuple[TestClient, Session]: 测试客户端和数据库会话。
    """
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
