"""管理员副驾驶 API 权限与结构化协议测试。"""
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.main import app


def test_admin_copilot_requires_authentication_header():
    response = TestClient(app).post(
        "/api/admin/copilot/chat",
        json={"message": "生成日报", "session_id": "unauth-session-001"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == 40100


def test_legacy_template_chat_is_gone(db, admin_user):
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        response = TestClient(app).post(
            "/api/admin/copilot/chat",
            json={"message": "生成日报", "session_id": "legacy-session-001"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)

    assert response.status_code == 410
    assert response.json()["detail"] == "旧管理副驾驶协议已停用，请使用 /api/agent-responses/stream"
