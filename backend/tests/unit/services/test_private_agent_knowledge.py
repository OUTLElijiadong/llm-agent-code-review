"""私人聊天笔记不得因 Agent 全局知识、管理员或孤儿引用而跨账号召回。"""

from types import SimpleNamespace

import pytest

from app.api.v1 import agent_governance as api
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
from app.models.agent_response_run import AgentResponseRun
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_doc import KnowledgeDoc
from app.models.user import User
from app.services import agent_knowledge_service as service
from app.services import agent_responses_service as responses
from app.services.deepseek_responses_runtime import ToolCall


@pytest.fixture
def private_knowledge(db, monkeypatch, super_admin_user):
    monkeypatch.setattr(
        service.embedding_service, "embed_texts", lambda db, texts, **kw: ([[1.0, 0.0] for _ in texts], "fixture")
    )
    monkeypatch.setattr(service.embedding_service, "embed_one", lambda *a, **kw: ([1.0, 0.0], "fixture"))
    monkeypatch.setattr(responses, "get_request_orchestrator", lambda *a, **kw: SimpleNamespace())
    owner = User(username="private-note-owner", password="unused", role="admin", status=1)
    peer = User(username="private-note-peer", password="unused", role="admin", status=1)
    db.add_all([owner, peer])
    db.flush()
    db.add(
        AgentResponseRun(
            run_id="private-notebook-run",
            user_id=owner.id,
            surface="admin",
            session_key="private-notebook-session",
            checkpoint_json="{}",
            status="completed",
        )
    )
    db.flush()
    docs = {}
    for key, ref in (
        ("private", "response_run:private-notebook-run"),
        ("orphan", "response_run:missing-run"),
        ("shared", "source:handbook"),
    ):
        doc = AgentKnowledgeDoc(
            agent_code="manager",
            title=f"{key} knowledge",
            source_type="manual",
            source_ref=ref,
            status="active",
            risk_level="low",
            confidence=1.0,
            char_count=20,
            chunk_count=1,
        )
        db.add(doc)
        db.flush()
        db.add(
            AgentKnowledgeChunk(
                doc_id=doc.id,
                agent_code="manager",
                seq=0,
                content=f"{key} private-content",
                embedding="[1.0,0.0]",
                embed_model="fixture",
            )
        )
        docs[key] = doc
    db.commit()
    return owner, peer, super_admin_user, docs


@pytest.mark.parametrize("reader", ["owner", "peer", "super_admin"])
def test_recall_old_private_notes_requires_exact_account(db, private_knowledge, reader):
    owner, peer, admin, docs = private_knowledge
    actor = {"owner": owner, "peer": peer, "super_admin": admin}[reader]
    hits = service.unified_retrieve(db, user_id=actor.id, agent_code="manager", query="knowledge", top_k=50)
    ids = {item["doc_id"] for item in hits if item["owner_type"] == "agent"}
    assert docs["orphan"].id not in ids
    assert (docs["private"].id in ids) is (reader == "owner")
    assert docs["shared"].id in ids


@pytest.mark.parametrize("reader", ["owner", "peer", "super_admin"])
def test_governance_list_cannot_bypass_private_notes(db, private_knowledge, reader):
    owner, peer, admin, docs = private_knowledge
    actor = {"owner": owner, "peer": peer, "super_admin": admin}[reader]
    rows = api.list_agent_knowledge("manager", db=db, _=actor).data
    ids = {row.id for row in rows}
    assert docs["orphan"].id not in ids
    assert (docs["private"].id in ids) is (reader == "owner")
    assert docs["shared"].id in ids


def test_anonymous_service_access_sees_only_shared_documents(db, private_knowledge):
    *_, docs = private_knowledge
    assert {row.id for row in service.list_docs(db, agent_code="manager")} == {docs["shared"].id}


@pytest.mark.parametrize("reader", ["peer", "super_admin"])
@pytest.mark.parametrize("kind", ["private", "orphan"])
def test_activation_cannot_disclose_or_mutate_foreign_private_doc(db, private_knowledge, reader, kind):
    _, peer, admin, docs = private_knowledge
    actor = peer if reader == "peer" else admin
    doc = docs[kind]
    doc.status = "pending_approval"
    db.commit()
    with pytest.raises((NotFoundError, ForbiddenError)):
        api.activate_agent_knowledge_doc(doc.id, db=db, _=actor)
    assert db.get(AgentKnowledgeDoc, doc.id).status == "pending_approval"


def test_owner_and_shared_activation_continue_to_work(db, private_knowledge):
    owner, peer, _, docs = private_knowledge
    for key, actor in (("private", owner), ("shared", peer)):
        docs[key].status = "pending_approval"
        db.commit()
        result = api.activate_agent_knowledge_doc(docs[key].id, db=db, _=actor)
        assert result.data.status == "active"


def test_reserved_private_source_cannot_be_forged_by_other_account(db, private_knowledge):
    _, peer, _, _ = private_knowledge
    with pytest.raises((NotFoundError, ForbiddenError)):
        service.add_document(
            db,
            agent_code="manager",
            title="forged",
            content="pretend owner",
            user_id=peer.id,
            source_ref="response_run:private-notebook-run",
        )


def test_new_note_is_personal_and_replayed_call_replaces_only_itself(db, private_knowledge):
    owner, peer, admin, _ = private_knowledge
    executor = responses.PrismToolExecutor(db, owner, surface="admin", run_id="r" * 80, mcp_provider=SimpleNamespace())
    first = ToolCall("call-" + "x" * 150, "save_knowledge_note", {"title": "我的经验", "content": "只属于本人"}, "{}")
    result = executor._save_knowledge_note(first)
    repeated = executor._save_knowledge_note(first)
    executor._save_knowledge_note(
        ToolCall("other-call", "save_knowledge_note", {"title": "另一笔", "content": "另一条私人经验"}, "{}")
    )
    assert result.output["owner_type"] == repeated.output["owner_type"] == "user"
    rows = db.query(KnowledgeDoc).filter(KnowledgeDoc.user_id == owner.id, KnowledgeDoc.status == "active").all()
    assert {row.title for row in rows} == {"我的经验", "另一笔"}
    assert all(len(row.source_ref) <= 64 for row in rows)
    assert db.query(AgentKnowledgeDoc).filter(AgentKnowledgeDoc.title.in_(["我的经验", "另一笔"])).count() == 0
    chunks = db.query(KnowledgeChunk).all()
    assert chunks and all(chunk.user_id == owner.id for chunk in chunks)
    for other in (peer, admin):
        hits = service.unified_retrieve(db, user_id=other.id, agent_code="manager", query="经验", top_k=50)
        assert not any(hit["title"] in {"我的经验", "另一笔"} for hit in hits)
