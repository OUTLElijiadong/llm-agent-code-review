"""历史全局操作日志不能成为私人聊天的读取或关键词探测旁路。"""

from types import SimpleNamespace

import pytest

from app.api.v1 import audit
from app.models.audit_log import AuditLog
from app.schemas.audit import AuditLogOut
from app.schemas.common import PageOut


def _list(db, *, viewer_id=7, keyword="", actor_id=None):
    result = audit.list_audit_logs(
        action="", keyword=keyword, actor_id=actor_id, start="", end="", page=1, page_size=20,
        db=db, viewer=SimpleNamespace(id=viewer_id, role="admin"),
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
