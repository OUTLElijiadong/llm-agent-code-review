"""Agent 治理管理端 API 集成测试。"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import require_admin
from app.main import app
from app.models.agent_governance import AgentAlert
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
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)
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
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/admin"):
            continue
        normalized_path = re.sub(r"\{[^}]+\}", "{param}", path[4:])
        for method in methods:
            if method in {"GET", "POST", "PUT", "DELETE"}:
                backend_routes.add((method, normalized_path))

    missing = sorted(frontend_calls - backend_routes)
    assert not missing


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
    job = _ok(client, "put", f"/api/admin/jobs/{jobs[0]['id']}", json={"schedule": "daily@02:10", "status": "enabled"})
    assert job["schedule"] == "daily@02:10"
    run = _ok(client, "post", f"/api/admin/jobs/{jobs[0]['id']}/run")
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
