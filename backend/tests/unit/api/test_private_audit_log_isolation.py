"""历史全局操作日志不能成为私人聊天的读取或关键词探测旁路。"""

from types import SimpleNamespace

import pytest

from app.api.v1 import audit
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.audit import AuditLogOut
from app.schemas.common import PageOut


def _ensure_user(db, user_id, role):
    """范围判定读取真实用户行，夹具必须写入对应账号，而不是只传替身对象。"""
    if db.get(User, user_id) is None:
        db.add(User(id=user_id, username=f"{role}-{user_id}", password="x", role=role, status=1))
        db.commit()


def _list(db, *, viewer_id=7, keyword="", actor_id=None, include_system_heartbeat=False, role="admin"):
    _ensure_user(db, viewer_id, role)
    result = audit.list_audit_logs(
        action="", keyword=keyword, actor_id=actor_id, start="", end="", page=1, page_size=20,
        include_system_heartbeat=include_system_heartbeat,
        db=db, viewer=SimpleNamespace(id=viewer_id, role=role),
    ).data
    return PageOut[AuditLogOut].model_validate(result.model_dump())


@pytest.mark.parametrize("action,target", [
    ("agent_team_create", "agent_team"),
    ("agent_team_cancel", "agent_team"),
    ("agent_team_archive", "agent_team"),
    ("legacy_action", "agent_response_run"),
    ("agent_mesh_send", None),
    ("roundtable_control", "discussion"),
    ("agent_approval", "approval"),
    ("governance.knowledge_doc_create", "agent_knowledge_doc"),
])
def test_cross_account_private_audit_detail_is_redacted_without_mutating_history(db, action, target):
    row = AuditLog(actor_id=8, actor_name="other-user", action=action, target_type=target,
                   target_id="private-id", detail="PRIVATE-HISTORY-TEXT", status="success")
    db.add(row)
    db.commit()
    result = _list(db)
    assert result.total == 1
    payload = result.model_dump()
    assert "PRIVATE-HISTORY-TEXT" not in str(payload)
    assert payload["items"][0]["content_redacted"] is True
    assert payload["items"][0]["target_id"] == "private-id"
    db.expire_all()
    assert db.get(AuditLog, row.id).detail == "PRIVATE-HISTORY-TEXT"


@pytest.mark.parametrize("owner", [8, None])
@pytest.mark.parametrize("target", ["agent_team", "approval", "agent_knowledge_doc"])
def test_private_detail_keyword_cannot_infer_cross_account_or_unowned_history(db, owner, target):
    db.add(AuditLog(actor_id=owner, actor_name="other-user", action="legacy_private", target_type=target,
                    detail="SENSITIVE-KEYWORD", status="success"))
    db.commit()
    assert _list(db, keyword="SENSITIVE-KEYWORD", actor_id=owner).total == 0
    # 已授权操作者元数据查询仍可用，返回体中的私人文本仍隐藏。
    result = _list(db, keyword="other-user")
    assert result.total == 1
    assert "SENSITIVE-KEYWORD" not in str(result.model_dump())


def test_owner_private_detail_and_business_audit_remain_visible(db):
    db.add_all([
        AuditLog(actor_id=7, actor_name="self", action="agent_team_create", target_type="agent_team",
                 detail="OWN-PRIVATE", status="success"),
        AuditLog(actor_id=8, actor_name="other-user", action="project_create", target_type="project",
                 detail="AUTHORIZED-BUSINESS", status="success"),
    ])
    db.commit()
    assert _list(db, keyword="OWN-PRIVATE").total == 1
    assert _list(db, keyword="AUTHORIZED-BUSINESS").total == 1
    payload = _list(db).model_dump()
    assert "OWN-PRIVATE" in str(payload) and "AUTHORIZED-BUSINESS" in str(payload)


def test_system_heartbeat_is_hidden_by_default_and_can_be_expanded(db):
    db.add_all([
        AuditLog(actor_id=None, actor_name="system", action="admin_copilot.ops.status",
                 target_type="ops", detail="routine heartbeat", status="success"),
        AuditLog(actor_id=7, actor_name="admin", action="user_role_update",
                 target_type="user", target_id="8", detail="role changed", status="success"),
    ])
    db.commit()

    default_page = _list(db)
    expanded_page = _list(db, include_system_heartbeat=True)

    assert default_page.total == 1
    assert [row.action for row in default_page.items] == ["user_role_update"]
    assert expanded_page.total == 2
    assert {row.action for row in expanded_page.items} == {
        "admin_copilot.ops.status", "user_role_update",
    }


def _seed_mixed_actors(db):
    db.add_all([
        AuditLog(actor_id=78, actor_name="reviewer-self", action="project_create", target_type="project",
                 detail="SELF-ROW", status="success", ip="10.0.0.78"),
        AuditLog(actor_id=1, actor_name="admin-user", action="security_anomaly_review", target_type="security",
                 detail="OTHER-ADMIN-ROW", status="success", ip="10.0.0.1"),
        AuditLog(actor_id=5, actor_name="other-user", action="project_create", target_type="project",
                 detail="OTHER-USER-ROW", status="success", ip="10.0.0.5"),
    ])
    db.commit()


def test_reviewer_only_sees_own_audit_rows(db):
    _seed_mixed_actors(db)
    result = _list(db, viewer_id=78, role="reviewer")
    assert result.total == 1
    assert {item.actor_id for item in result.items} == {78}
    payload = str(result.model_dump())
    assert "OTHER-ADMIN-ROW" not in payload and "OTHER-USER-ROW" not in payload
    assert "10.0.0.1" not in payload and "10.0.0.5" not in payload


def test_reviewer_cannot_widen_scope_with_actor_id_or_keyword(db):
    _seed_mixed_actors(db)
    # actor_id 指向他人时不得生效，仍只返回本人记录。
    assert {i.actor_id for i in _list(db, viewer_id=78, role="reviewer", actor_id=1).items} == {78}
    # 关键词命中他人 actor_name 时 total 也不得包含他人记录。
    assert _list(db, viewer_id=78, role="reviewer", keyword="other-user").total == 0
    assert _list(db, viewer_id=78, role="reviewer", keyword="admin-user").total == 0


def test_admin_still_sees_all_audit_rows(db):
    _seed_mixed_actors(db)
    result = _list(db, viewer_id=7, role="admin")
    assert result.total == 3
    assert {item.actor_id for item in result.items} == {1, 5, 78}


def test_list_logs_own_only_without_viewer_fails_closed(db):
    from app.services import audit_service

    _seed_mixed_actors(db)
    page = audit_service.list_logs(db, "", "", None, "", "", 1, 20, viewer=None, own_only=True)
    assert page["total"] == 0
