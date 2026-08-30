"""嵌入服务回归: 私网端点直连开关 + 存量重嵌入。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.services import embedding_service


@pytest.fixture
def db():
    # 先导入域模型确保建表覆盖(部分模型模块仅按需导入)
    import app.models.agent_governance  # noqa: F401
    import app.models.knowledge_chunk  # noqa: F401
    import app.models.knowledge_doc  # noqa: F401

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_private_url_detection():
    assert embedding_service._is_private_url("http://embedding:80/v1") is True
    assert embedding_service._is_private_url("http://127.0.0.1:8080/v1") is True
    assert embedding_service._is_private_url("http://192.168.1.5/v1") is True
    assert embedding_service._is_private_url("http://10.0.0.2/v1") is True
    # 公网地址不得被判定为私网
    assert embedding_service._is_private_url("https://api.siliconflow.cn/v1") is False
    assert embedding_service._is_private_url("") is False


def test_api_embed_private_direct(monkeypatch, db):
    """开关开启且端点为私网时: 直连不走 SSRF 校验/pin。"""
    import httpx

    captured: dict = {}

    def fake_post(url, headers=None, json=None, **_kw):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        dims = 4

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"index": i, "embedding": [0.1] * dims} for i in range(len(json["input"]))]}

        return _Resp()

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return fake_post(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(embedding_service.settings, "embedding_allow_private_endpoint", True)

    vecs = embedding_service._api_embed(
        ["你好", "世界"], {"base_url": "http://embedding:80/v1", "api_key": "k", "model": "m"}
    )
    assert len(vecs) == 2 and len(vecs[0]) == 4
    assert captured["url"] == "http://embedding:80/v1/embeddings"
    # 未开私网开关时私网端点必须被 SSRF 校验拒绝
    monkeypatch.setattr(embedding_service.settings, "embedding_allow_private_endpoint", False)
    from app.core.exceptions import ValidationError as _VE

    with pytest.raises(_VE):
        embedding_service._api_embed(["x"], {"base_url": "http://embedding:80/v1", "api_key": "k", "model": "m"})


def test_reembed_all_stores_rebuilds_both_domains(db, monkeypatch):
    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc

    kb_doc = KnowledgeDoc(user_id=1, title="个人文档", source_type="manual",
                         status="active", char_count=10, chunk_count=1)
    db.add(kb_doc)
    db.flush()
    db.add(KnowledgeChunk(user_id=1, doc_id=kb_doc.id, seq=0, content="旧内容甲",
                         embedding="[]", embed_model="fallback:hash"))

    agent_doc = AgentKnowledgeDoc(agent_code="chat_assistant", title="Agent笔记", source_type="manual",
                                  status="active", char_count=10, chunk_count=1)
    db.add(agent_doc)
    db.flush()
    db.add(AgentKnowledgeChunk(doc_id=agent_doc.id, agent_code="chat_assistant", seq=0,
                                content="旧内容乙", embedding="[]", embed_model="fallback:hash"))
    db.commit()

    def fake_embed_texts(session, texts):
        return ([[0.5, 0.5] for _ in texts], "api:test-model")

    monkeypatch.setattr(embedding_service, "embed_texts", fake_embed_texts)
    stats = embedding_service.reembed_all_stores(db, batch_size=2)
    assert stats == {"kb_chunks": 1, "agent_chunks": 1, "failed_batches": 0}
    kb = db.query(KnowledgeChunk).one()
    assert json.loads(kb.embedding) == [0.5, 0.5]
    assert kb.embed_model == "api:test-model"
    agent = db.query(AgentKnowledgeChunk).one()
    assert agent.embed_model == "api:test-model"


def test_update_embedding_config_private_endpoint_gate(db, monkeypatch):
    """保存配置的私网放行: 开关开才允许内网端点, 默认仍拒绝(SSRF 防护)。"""
    from app.core.exceptions import ValidationError as _VE
    from app.services import system_config_service as scs

    monkeypatch.setattr(scs.settings, "embedding_allow_private_endpoint", False)
    with pytest.raises(_VE):
        scs.update_embedding_config(db, base_url="http://embedding:80/v1")

    monkeypatch.setattr(scs.settings, "embedding_allow_private_endpoint", True)
    data = scs.update_embedding_config(
        db, base_url="http://embedding:80/v1", api_key="local-tei",
        model="BAAI/bge-small-zh-v1.5", enabled=True,
    )
    assert data["enabled"] is True
    # 开启开关后公网端点仍走常规校验(非法值拒绝)
    with pytest.raises(_VE):
        scs.update_embedding_config(db, base_url="ht!tp://bad url")
