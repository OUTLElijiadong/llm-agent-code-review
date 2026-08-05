"""管理员副驾驶废弃协议路由移除契约测试(旧 chat/history 已删除,应 404)。"""
from fastapi.testclient import TestClient

from app.main import app


def test_legacy_admin_copilot_routes_are_removed():
    client = TestClient(app)
    assert client.post(
        "/api/admin/copilot/chat",
        json={"message": "x", "session_id": "sess-00000001"},
    ).status_code == 404
    assert client.get("/api/admin/copilot/history?session_id=sess-00000001").status_code == 404
