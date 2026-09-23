"""临时团队独立边界探针：内存 SQLite；不连接模型、生产或外部服务。"""
from unittest.mock import Mock

import pytest

from app.agents.base import AgentContext, AgentResult
from app.agents.orchestrator import get_request_orchestrator
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.core.permission_codes import PermissionCode
from app.models.agent_team import AgentTeam
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_mesh_dispatcher, agent_team_service, rbac_service


@pytest.fixture
def isolated_accounts(db):
    owner = User(id=801, username="isolated-owner", password="unused", role="user", status=1)
    other = User(id=802, username="isolated-peer", password="unused", role="user", status=1)
    admin = User(id=803, username="isolated-admin", password="unused", role="admin", status=1)
    role = Role(id=801, code="user", name="普通用户", status="active")
    permission = Permission(id=801, code=PermissionCode.AGENT_CHAT, name="聊天", module="agent")
    db.add_all([owner, other, admin, role, permission,
                UserRole(user_id=801, role_id=801), UserRole(user_id=802, role_id=801),
                RolePermission(role_id=801, permission_id=801)])
    db.commit()
    assert rbac_service.check_permission(db, owner.id, PermissionCode.AGENT_CHAT)
    assert not rbac_service.check_permission(db, owner.id, PermissionCode.SECURITY_SCAN)
    assert rbac_service.is_admin_user(db, admin.id)
    return owner, other, admin


@pytest.fixture
def owned_team(db, isolated_accounts):
    owner, _, _ = isolated_accounts
    row = AgentTeam(user_id=owner.id, surface="user", session_key="private-synthetic-session",
                    title="私有审查团队", objective="仅本人可见的合成任务内容", status="queued",
                    trace_id="private-synthetic-trace", summary_json="{}", error_json="{}")
    db.add(row)
    db.commit()
    return row


def test_admin_list_must_not_disclose_other_accounts_private_teams(db, isolated_accounts, owned_team):
    _, _, admin = isolated_accounts
    rows = agent_team_service.list_teams(db, admin)
    assert rows["total"] == 0
    assert rows["items"] == []


@pytest.mark.parametrize("reader", ["get_team", "list_team_events", "list_team_messages"])
def test_admin_read_must_not_disclose_other_accounts_private_team(db, isolated_accounts, owned_team, reader):
    _, _, admin = isolated_accounts
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        getattr(agent_team_service, reader)(db, admin, owned_team.id)


def test_ordinary_peer_is_already_denied(db, isolated_accounts, owned_team):
    _, other, _ = isolated_accounts
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.get_team(db, other, owned_team.id)


@pytest.mark.parametrize("scope", ["project", "file", "task"])
def test_mesh_security_cannot_bypass_existing_account_permission(db, isolated_accounts, monkeypatch, scope):
    owner, _, _ = isolated_accounts
    # 仅替代昂贵的模型/扫描叶方法；真实账号、RBAC、主工具和mesh分支保持原实现。
    scanner = Mock(return_value=AgentResult(success=True, data={"findings": []}))
    monkeypatch.setattr(SecuritySentinelAgent, f"scan_{scope}", scanner)
    orch = get_request_orchestrator(db, user=owner)
    direct = getattr(orch, f"audit_security_for_{scope}")(901, ctx=AgentContext(user_id=owner.id))
    assert direct.success is False
    assert "security:scan" in direct.error
    scanner.assert_not_called()
    _, dispatched = agent_mesh_dispatcher._handle(
        db, owner, "agent:security_sentinel",
        {"user_id": owner.id, "payload": {f"{scope}_id": 901, "scan_mode": "full"}, "context": {}},
    )
    scanner.assert_not_called()
    assert dispatched["status"] != "completed"


def test_team_creation_cannot_delegate_ungranted_security_scan(db, isolated_accounts):
    owner, _, _ = isolated_accounts
    db.add(Project(id=901, user_id=owner.id, project_name="本账号合成项目", status="active"))
    db.commit()
    payload = AgentTeamCreateIn.model_validate({
        "surface": "user", "session_id": "new-private-session", "title": "合成团队", "objective": "完整安全审计",
        "members": [{"member_key": "security", "display_name": "安全哨兵", "address": "agent:security_sentinel"}],
        "tasks": [{"task_key": "scan", "member_key": "security", "title": "审计", "instructions": "安全审计",
                   "input": {"project_id": 901, "scan_mode": "full"}}],
    })
    with pytest.raises(agent_team_service.AgentTeamAccessError):
        agent_team_service.create_team(db, owner, payload)
