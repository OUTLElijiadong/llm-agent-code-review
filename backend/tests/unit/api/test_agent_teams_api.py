"""Agent Teams HTTP 认证、账户隔离和响应契约集成测试。"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.main import app
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User


def _team_payload(*, surface: str = "user", session_id: str = "session-owner-1") -> dict[str, Any]:
    return {
        "surface": surface,
        "session_id": session_id,
        "title": "HTTP 团队验收",
        "objective": "验证账户隔离和统一响应契约",
        "members": [
            {
                "member_key": "analyzer",
                "display_name": "分析 Agent",
                "address": "agent:project_analyzer",
                "role": "worker",
            },
            {
                "member_key": "verifier",
                "display_name": "验证 Agent",
                "address": "agent:code_reviewer",
                "role": "verifier",
            },
        ],
        "tasks": [
            {
                "task_key": "analyze",
                "member_key": "analyzer",
                "title": "分析项目",
                "instructions": "分析结构化输入",
                "depends_on": [],
            },
            {
                "task_key": "verify",
                "member_key": "verifier",
                "title": "独立验证",
                "instructions": "核对前置结果和证据",
                "depends_on": ["analyze"],
            },
        ],
        "max_active_children": 3,
        "max_attempts": 3,
    }


def _data(response, *, status_code: int = 200) -> Any:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert "data" in body
    return body["data"]


@pytest.fixture()
def team_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    owner = User(username="team-owner", password="x", email="owner@example.test", role="user", status=1)
    other = User(username="team-other", password="x", email="other@example.test", role="user", status=1)
    admin = User(username="team-admin", password="x", email="admin@example.test", role="admin", status=1)
    user_role = Role(name="普通用户", code="user", status="active", sort=100, is_builtin=1)
    chat_permission = Permission(
        code=PermissionCode.AGENT_CHAT,
        name="Agent 对话",
        module="agent",
        type="api",
    )
    db.add_all([owner, other, admin, user_role, chat_permission])
    db.flush()
    db.add(RolePermission(role_id=user_role.id, permission_id=chat_permission.id))
    db.add_all(
        [
            UserRole(user_id=owner.id, role_id=user_role.id),
            UserRole(user_id=other.id, role_id=user_role.id),
        ]
    )
    db.commit()

    def override_db():
        yield db

    def request(
        user: Optional[User],
        method: str,
        path: str,
        **kwargs: Any,
    ):
        app.dependency_overrides[get_db] = override_db
        if user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app).request(method, path, **kwargs)

    yield {"owner": owner, "other": other, "admin": admin, "request": request}

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    db.close()
    engine.dispose()


def test_agent_teams_requires_authentication(team_api):
    response = team_api["request"](None, "GET", "/api/agent-teams")

    assert response.status_code == 401
    assert response.json()["code"] == 40100


def test_agent_teams_plain_user_is_limited_to_own_account(team_api):
    request = team_api["request"]
    owner = team_api["owner"]
    other = team_api["other"]

    created = _data(request(owner, "POST", "/api/agent-teams", json=_team_payload()))
    team_id = created["team_id"]
    assert created["user_id"] == owner.id
    assert created["surface"] == "user"
    assert created["status"] == "queued"
    assert created["max_active_children"] == 3
    assert created["counts"] == {
        "total": 2,
        "completed": 0,
        "running": 0,
        "queued": 2,
        "failed": 0,
        "blocked": 0,
    }
    assert {item["role"] for item in created["members"]} == {"worker", "verifier"}
    assert [item["task_key"] for item in created["tasks"]] == ["analyze", "verify"]
    assert created["events"][0]["event_type"] == "team.created"
    assert created["messages"] == []
    assert created["message_page"] == {
        "total": 0,
        "has_more": False,
        "next_before_id": None,
        "page_size": 500,
    }

    message_page = _data(
        request(
            owner,
            "GET",
            f"/api/agent-teams/{team_id}/messages",
            params={"limit": 1},
        )
    )
    assert message_page == {
        "items": [],
        "total": 0,
        "has_more": False,
        "next_before_id": None,
        "page_size": 1,
    }

    listed = _data(
        request(
            owner,
            "GET",
            "/api/agent-teams",
            params={"surface": "user", "session_id": "session-owner-1"},
        )
    )
    assert listed["total"] == 1
    assert [item["team_id"] for item in listed["items"]] == [team_id]

    hidden_list = _data(request(other, "GET", "/api/agent-teams"))
    assert hidden_list == {"items": [], "total": 0}
    hidden_detail = request(other, "GET", f"/api/agent-teams/{team_id}")
    assert hidden_detail.status_code == 404
    assert hidden_detail.json()["code"] == 40431
    hidden_messages = request(other, "GET", f"/api/agent-teams/{team_id}/messages")
    assert hidden_messages.status_code == 404
    assert hidden_messages.json()["code"] == 40431
    hidden_events = request(other, "GET", f"/api/agent-teams/{team_id}/events")
    assert hidden_events.status_code == 404
    assert hidden_events.json()["code"] == 40431


def test_agent_teams_events_incremental_feed(team_api):
    """增量事件流 HTTP 契约:首次全量,after_id 只取新事件,含 team_status。"""
    request = team_api["request"]
    owner = team_api["owner"]

    created = _data(request(owner, "POST", "/api/agent-teams", json=_team_payload()))
    team_id = created["team_id"]

    first = _data(request(owner, "GET", f"/api/agent-teams/{team_id}/events"))
    assert first["items"], "建队后应有 team.created 等初始事件"
    assert first["team_status"] == "queued"
    assert first["has_more"] is False
    first_ids = [item["event_id"] for item in first["items"]]
    assert first_ids == sorted(first_ids)
    assert first["next_after_id"] == first_ids[-1]
    assert first["items"][0]["event_type"] == "team.created"

    # after_id 取到末尾后,没有新事件时应返回空列表但保持游标
    drained = _data(
        request(
            owner,
            "GET",
            f"/api/agent-teams/{team_id}/events",
            params={"after_id": first["next_after_id"]},
        )
    )
    assert drained["items"] == []
    assert drained["next_after_id"] == first["next_after_id"]
    assert drained["team_status"] == "queued"

    # 分页:limit=1 时 has_more=True,游标可续读
    page_one = _data(
        request(owner, "GET", f"/api/agent-teams/{team_id}/events", params={"limit": 1})
    )
    assert len(page_one["items"]) == 1
    if page_one["has_more"]:
        page_two = _data(
            request(
                owner,
                "GET",
                f"/api/agent-teams/{team_id}/events",
                params={"after_id": page_one["next_after_id"], "limit": 500},
            )
        )
        assert page_two["items"][0]["event_id"] > page_one["items"][0]["event_id"]


def test_plain_user_cannot_create_admin_surface(team_api):
    response = team_api["request"](
        team_api["owner"],
        "POST",
        "/api/agent-teams",
        json=_team_payload(surface="admin", session_id="session-admin-denied"),
    )

    assert response.status_code == 403
    assert response.json()["code"] == 40331


def test_admin_can_read_and_operate_team_across_accounts(team_api):
    request = team_api["request"]
    owner = team_api["owner"]
    admin = team_api["admin"]
    created = _data(request(owner, "POST", "/api/agent-teams", json=_team_payload()))
    team_id = created["team_id"]

    detail = _data(request(admin, "GET", f"/api/agent-teams/{team_id}"))
    assert detail["team_id"] == team_id
    assert detail["user_id"] == owner.id
    # 任务输出带 member_key:前端子Agent工作卡片按它把任务匹配回成员
    task_by_key = {item["task_key"]: item for item in detail["tasks"]}
    assert task_by_key["analyze"]["member_key"] == "analyzer"
    assert task_by_key["verify"]["member_key"] == "verifier"

    cancelled = _data(
        request(
            admin,
            "POST",
            f"/api/agent-teams/{team_id}/cancel",
            json={"reason": "管理员终止验收任务"},
        )
    )
    assert cancelled["status"] == "cancelled"
    assert {item["status"] for item in cancelled["tasks"]} == {"cancelled"}
    assert {item["status"] for item in cancelled["members"]} == {"reclaimed"}

    admin_team = _data(
        request(
            admin,
            "POST",
            "/api/agent-teams",
            json=_team_payload(surface="admin", session_id="session-admin-owned"),
        )
    )
    assert admin_team["surface"] == "admin"
    assert admin_team["user_id"] == admin.id
