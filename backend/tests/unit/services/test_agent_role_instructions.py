from types import SimpleNamespace

from app.services.agent_responses_service import _instructions


def test_member_assistant_distinguishes_reviewer_audit_permission() -> None:
    reviewer = _instructions("member", SimpleNamespace(username="reviewer", role="reviewer"))
    ordinary = _instructions("member", SimpleNamespace(username="user", role="user"))

    assert "审查员(已合并原审计员角色)" in reviewer
    assert "审查员具备该权限时可访问" in reviewer
    assert "普通用户不可访问" in ordinary
    assert "/api/admin/audit 是接口路径" in reviewer
