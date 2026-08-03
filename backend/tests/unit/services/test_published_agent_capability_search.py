"""已发布 Agent 在授权边界内使用持久化近义词检索。"""

from __future__ import annotations

from app.models.agent_capability import AgentCapabilityAlias
from app.services import capability_catalog_service, published_agent_tools


def test_persisted_alias_selects_published_agent(db, admin_user, monkeypatch) -> None:
    catalog = [
        {
            "id": 11,
            "code": "quality_guard",
            "name": "质量守门员",
            "description": "审查代码可靠性",
            "version_number": 3,
            "release_id": 31,
            "skills": [],
        },
        {
            "id": 12,
            "code": "style_reviewer",
            "name": "风格审查员",
            "description": "检查代码风格",
            "version_number": 1,
            "release_id": 32,
            "skills": [],
        },
    ]
    alias = AgentCapabilityAlias(
        capability_code="published_agent:quality_guard",
        alias="可靠代码检查",
        normalized_alias="可靠代码检查",
        locale="zh-CN",
        weight=1.0,
        enabled=1,
    )
    db.add(alias)
    db.commit()
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_args: None)
    monkeypatch.setattr(published_agent_tools.agent_studio_service, "list_catalog", lambda _db: catalog)
    monkeypatch.setattr(
        capability_catalog_service.embedding_service,
        "embed_texts",
        lambda _db, values: ([[0.0] * 8 for _ in values], "test"),
    )

    result = published_agent_tools.search_published_agents(
        db,
        admin_user,
        query="可靠代码检查",
    )

    assert result["selection_state"] == "exact"
    assert result["requires_clarification"] is False
    assert result["total_catalog_size"] == 2
    assert result["candidates"][0]["code"] == "quality_guard"
    assert result["candidates"][0]["score"] == 1.0
    assert "alias" in result["candidates"][0]["match_reasons"]
