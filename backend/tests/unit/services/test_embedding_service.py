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
    import app.models.code_file  # noqa: F401
    import app.models.enterprise_project  # noqa: F401
    import app.models.knowledge_chunk  # noqa: F401
    import app.models.knowledge_doc  # noqa: F401
    import app.models.project  # noqa: F401
    import app.models.review_issue  # noqa: F401
    import app.models.review_task  # noqa: F401

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
    monkeypatch.setattr(embedding_service.settings, "billing_allow_unmetered_external_calls", True)

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

    def fake_embed_texts(session, texts, *, user_id):
        assert session is db and user_id == 7
        return ([[0.5, 0.5] for _ in texts], "api:test-model")

    monkeypatch.setattr(embedding_service, "embed_texts", fake_embed_texts)
    stats = embedding_service.reembed_all_stores(db, batch_size=2, user_id=7)
    assert stats == {"kb_chunks": 1, "agent_chunks": 1, "failed_batches": 0}
    kb = db.query(KnowledgeChunk).one()
    assert json.loads(kb.embedding) == [0.5, 0.5]
    assert kb.embed_model == "api:test-model"
    agent = db.query(AgentKnowledgeChunk).one()
    assert agent.embed_model == "api:test-model"


def test_reembed_does_not_send_bound_enterprise_sources_to_embedding(db, monkeypatch):
    """全局重建向量不能把企业项目的个人历史副本发送到嵌入端点。"""
    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
    from app.models.code_file import CodeFile
    from app.models.enterprise_project import EnterpriseProject
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc
    from app.models.project import Project
    from app.models.review_issue import ReviewIssue
    from app.models.review_task import ReviewTask

    project = Project(user_id=1, project_name="enterprise source", status="active")
    db.add(project)
    db.flush()
    personal_project = Project(user_id=2, project_name="personal agent source", status="active")
    db.add(personal_project)
    db.flush()
    source_file = CodeFile(
        project_id=project.id,
        file_name="secret.py",
        file_path="secret.py",
        language="python",
        size_bytes=7,
        line_count=1,
        content="company code",
        status="active",
    )
    personal_source_file = CodeFile(
        project_id=personal_project.id,
        file_name="personal.py",
        file_path="personal.py",
        language="python",
        size_bytes=9,
        line_count=1,
        content="personal source",
        status="active",
    )
    task = ReviewTask(user_id=1, project_id=project.id, task_name="review", status="success")
    db.add_all([source_file, personal_source_file, task])
    db.flush()
    issue = ReviewIssue(
        task_id=task.id,
        issue_type="security",
        severity="high",
        description="company finding",
        status="fixed",
    )
    db.add(issue)
    db.flush()
    db.add(EnterpriseProject(enterprise_id=50, project_id=project.id, group_id=70, status="active"))
    company_code = KnowledgeDoc(
        user_id=1, title="company code", source_type="code", source_ref=f"file:{source_file.id}",
        status="active", char_count=12, chunk_count=1,
    )
    company_issue = KnowledgeDoc(
        user_id=1, title="company issue", source_type="issue", source_ref=f"issue:{issue.id}",
        status="active", char_count=15, chunk_count=1,
    )
    personal = KnowledgeDoc(
        user_id=1, title="personal note", source_type="manual", status="active", char_count=14, chunk_count=1,
    )
    db.add_all([company_code, company_issue, personal])
    db.flush()
    db.add_all([
        KnowledgeChunk(doc_id=doc.id, user_id=1, seq=0, content=doc.title,
                       embedding="[]", embed_model="fallback:hash")
        for doc in (company_code, company_issue, personal)
    ])
    agent_doc = AgentKnowledgeDoc(
        agent_code="chat_assistant", title="global note", source_type="manual",
        status="active", char_count=11, chunk_count=1,
    )
    db.add(agent_doc)
    db.flush()
    db.add_all([
        AgentKnowledgeChunk(
            doc_id=agent_doc.id, agent_code="chat_assistant", seq=0, content="global note",
            embedding="[]", embed_model="fallback:hash",
        ),
        AgentKnowledgeDoc(
            agent_code="chat_assistant", title="bound project global copy", source_type="project",
            source_ref=f"code_file:{source_file.id}", status="active", char_count=20, chunk_count=1,
        ),
        AgentKnowledgeDoc(
            agent_code="chat_assistant", title="private runtime note", source_type="manual",
            source_ref="response_run:private-run", status="active", char_count=20, chunk_count=1,
        ),
        AgentKnowledgeDoc(
            agent_code="chat_assistant", title="personal project global copy", source_type="project",
            source_ref=f"code_file:{personal_source_file.id}", status="active", char_count=20, chunk_count=1,
        ),
    ])
    db.flush()
    private_run_doc = db.query(AgentKnowledgeDoc).filter_by(source_ref="response_run:private-run").one()
    bound_agent_doc = db.query(AgentKnowledgeDoc).filter_by(source_ref=f"code_file:{source_file.id}").one()
    personal_agent_doc = db.query(AgentKnowledgeDoc).filter_by(
        source_ref=f"code_file:{personal_source_file.id}",
    ).one()
    db.add_all([
        AgentKnowledgeChunk(
            doc_id=bound_agent_doc.id, agent_code="chat_assistant", seq=0,
            content="company agent source", embedding="[]", embed_model="fallback:hash",
        ),
        AgentKnowledgeChunk(
            doc_id=private_run_doc.id, agent_code="chat_assistant", seq=0,
            content="private agent response", embedding="[]", embed_model="fallback:hash",
        ),
        AgentKnowledgeChunk(
            doc_id=personal_agent_doc.id, agent_code="chat_assistant", seq=0,
            content="personal agent project", embedding="[]", embed_model="fallback:hash",
        ),
    ])
    db.commit()

    sent = []
    monkeypatch.setattr(embedding_service.system_config_service, "get_embedding_config", lambda _db: {"enabled": False})
    monkeypatch.setattr(
        embedding_service,
        "embed_texts",
        lambda _db, texts, **_kwargs: (sent.extend(texts) or [[1.0] for _ in texts], "api:test-model"),
    )

    stats = embedding_service.reembed_all_stores(db, user_id=7)

    assert sent == ["personal note", "global note", "personal agent project"]
    assert stats == {"kb_chunks": 1, "agent_chunks": 2, "failed_batches": 0}


def test_reembed_does_not_skip_candidates_after_byte_budget_cut(db, monkeypatch):
    """候选页被批次字节上限截断后，剩余切片仍须进入后续批次。"""
    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc

    docs = [
        AgentKnowledgeDoc(
            agent_code="manager",
            title=f"large document {index}",
            source_type="manual",
            status="active",
            char_count=300_000,
            chunk_count=1,
        )
        for index in range(3)
    ]
    db.add_all(docs)
    db.flush()
    db.add_all([
        AgentKnowledgeChunk(
            doc_id=doc.id,
            agent_code="manager",
            seq=0,
            content=f"{index}" * 300_000,
            embedding="[]",
            embed_model="fallback:hash",
        )
        for index, doc in enumerate(docs)
    ])
    db.commit()

    sent = []
    monkeypatch.setattr(
        embedding_service.system_config_service,
        "get_embedding_config",
        lambda _db: {"enabled": False},
    )
    monkeypatch.setattr(
        embedding_service,
        "embed_texts",
        lambda _db, texts, **_kwargs: (sent.extend(texts) or [[1.0] for _ in texts], "api:test-model"),
    )

    stats = embedding_service.reembed_all_stores(db, batch_size=3, user_id=7)

    assert len(sent) == 3
    assert stats == {"kb_chunks": 0, "agent_chunks": 3, "failed_batches": 0}


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
