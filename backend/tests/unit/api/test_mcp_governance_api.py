"""MCP 治理 API 必须使用真实 JWT 且仅唯一 admin 可访问。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import create_access_token
from app.main import app
from app.models.rbac import Role, UserRole
from app.models.user import User


def test_mcp_governance_requires_unique_superadmin() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    manager = User(username="manager", password="x", role="admin", status=1, token_version=0)
    admin = User(username="admin", password="x", role="super_admin", status=1, token_version=0)
    role = Role(name="超级管理员", code="super_admin", status="active", is_builtin=1)
    session.add_all([manager, admin, role])
    session.flush()
    session.add(UserRole(user_id=admin.id, role_id=role.id))
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        manager_token = create_access_token(manager.id, manager.role, manager.token_version)
        admin_token = create_access_token(admin.id, admin.role, admin.token_version)

        denied = client.get(
            "/api/admin/mcp/servers",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        allowed = client.post(
            "/api/admin/mcp/servers/recommended",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert denied.status_code == 403
        assert denied.json()["code"] == 40322
        assert allowed.status_code == 200
        by_code = {row["code"]: row for row in allowed.json()["data"]}
        assert by_code["github"]["status"] == "credential_required"
        # prism-code 已由本轮本地适配器接入，不再仅是登记占位。
        assert by_code["prism-code"]["status"] == "healthy"
        assert "encrypted_headers" not in allowed.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        engine.dispose()
