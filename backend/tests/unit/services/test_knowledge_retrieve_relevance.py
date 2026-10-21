"""Codex 4.1.16：知识库检索不得以约 60% 相似度返回无关文档。"""

from app.services import embedding_service, knowledge_service


def _ingest(db, user_id, title, content):
    return knowledge_service.add_document(db, user_id, title=title, content=content, source_type="manual")


def test_fallback_retrieval_drops_unrelated_documents(db, monkeypatch):
    monkeypatch.setattr(embedding_service.system_config_service, "get_embedding_config", lambda db: {"enabled": False})
    _ingest(db, 1, "SQL 注入防护", "使用参数化查询防止 SQL 注入，禁止拼接用户输入。")
    _ingest(db, 1, "周末菜谱", "红烧肉需要冰糖和老抽，小火慢炖一个小时。")
    hits = knowledge_service.retrieve(db, 1, "如何防止 SQL 注入", top_k=5)
    titles = [hit["title"] for hit in hits]
    assert "SQL 注入防护" in titles
    assert "周末菜谱" not in titles


def test_unrelated_query_returns_nothing(db, monkeypatch):
    monkeypatch.setattr(embedding_service.system_config_service, "get_embedding_config", lambda db: {"enabled": False})
    _ingest(db, 1, "周末菜谱", "红烧肉需要冰糖和老抽，小火慢炖一个小时。")
    assert knowledge_service.retrieve(db, 1, "Kubernetes 滚动发布策略", top_k=5) == []
