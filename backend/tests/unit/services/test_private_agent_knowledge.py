"""私人聊天笔记不得因 Agent 全局知识、管理员或孤儿引用而跨账号召回。"""

import json
from types import SimpleNamespace

import pytest

from app.api.v1 import agent_governance as api
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc, AgentKnowledgeSource
from app.models.agent_response_run import AgentResponseRun
from app.models.code_file import CodeFile
from app.models.enterprise_project import EnterpriseProject
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_doc import KnowledgeDoc
from app.models.project import Project
from app.models.user import User
from app.services import agent_knowledge_service as service
from app.services import agent_responses_service as responses
from app.services import scheduler_service
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


def test_personal_project_source_remains_shared_and_records_its_origin(db, private_knowledge):
    owner, peer, admin, _ = private_knowledge
    project = Project(user_id=owner.id, project_name="个人源码知识")
    db.add(project)
    db.flush()
    source_file = CodeFile(
        project_id=project.id,
        file_name="auth.py",
        file_path="auth.py",
        language="python",
        size_bytes=25,
        raw_size=25,
        content="PERSONAL_PROJECT_SOURCE_SECRET",
        status="active",
        is_binary=0,
    )
    source = service.upsert_source(
        db,
        agent_code="manager",
        source_type="project",
        source_uri=str(project.id),
        whitelist=1,
        enabled=1,
        config={"project_id": project.id},
    )
    db.add(source_file)
    db.commit()

    result = service.crawl_enabled_sources(db, agent_code="manager", user_id=admin.id)

    assert result["doc_count"] == 1
    doc = db.query(AgentKnowledgeDoc).filter_by(source_id=source.id).one()
    assert doc.status == "active"
    assert doc.source_ref == f"code_file:{source_file.id}"
    peer_hits = service.unified_retrieve(
        db,
        user_id=peer.id,
        agent_code="manager",
        query="personal project",
        top_k=50,
    )
    assert any(hit["doc_id"] == doc.id for hit in peer_hits)

    db.add(
        EnterpriseProject(
            enterprise_id=9000,
            project_id=project.id,
            group_id=9001,
            status="active",
        )
    )
    db.commit()
    bound_hits = service.unified_retrieve(
        db,
        user_id=peer.id,
        agent_code="manager",
        query="personal project",
        top_k=50,
    )
    assert doc.id not in {hit["doc_id"] for hit in bound_hits}
    result = scheduler_service._execute_job(
        db,
        SimpleNamespace(job_type="crawl", agent_code="manager"),
    )
    assert result["doc_count"] == 0
    db.refresh(doc)
    assert doc.status == "deleted"


def test_bound_project_source_is_rejected_and_legacy_docs_are_hidden_and_retired(
    db,
    private_knowledge,
    monkeypatch,
):
    owner, peer, admin, _ = private_knowledge
    project = Project(user_id=owner.id, project_name="企业源码知识")
    db.add(project)
    db.flush()
    source_file = CodeFile(
        project_id=project.id,
        file_name="tenant.py",
        file_path="tenant.py",
        language="python",
        size_bytes=20,
        raw_size=20,
        content="ENTERPRISE_PROJECT_SOURCE_SECRET",
        status="active",
        is_binary=0,
    )
    source = AgentKnowledgeSource(
        agent_code="manager",
        source_type="project",
        source_uri=str(project.id),
        whitelist=1,
        enabled=1,
        config_json=json.dumps({"project_id": project.id}),
    )
    db.add_all([source_file, source])
    db.flush()
    db.add(
        EnterpriseProject(
            enterprise_id=9001,
            project_id=project.id,
            group_id=9002,
            status="active",
        )
    )
    legacy_doc = AgentKnowledgeDoc(
        agent_code="manager",
        title="legacy enterprise source",
        source_type="project",
        source_ref=f"code_file:{source_file.id}",
        status="active",
        risk_level="low",
        confidence=1.0,
        char_count=40,
        chunk_count=1,
    )
    exact_project_reference_doc = AgentKnowledgeDoc(
        agent_code="manager",
        title="legacy exact project reference",
        source_type="project",
        source_ref=f"project:{project.id}",
        status="active",
        risk_level="low",
        confidence=1.0,
        char_count=38,
        chunk_count=1,
    )
    db.add_all([legacy_doc, exact_project_reference_doc])
    db.flush()
    db.add(
        AgentKnowledgeChunk(
            doc_id=legacy_doc.id,
            agent_code="manager",
            seq=0,
            content="ENTERPRISE_PROJECT_SOURCE_SECRET",
            embedding="[1.0,0.0]",
            embed_model="fixture",
        )
    )
    db.add(
        AgentKnowledgeChunk(
            doc_id=exact_project_reference_doc.id,
            agent_code="manager",
            seq=0,
            content="EXACT_PROJECT_REFERENCE_SECRET",
            embedding="[1.0,0.0]",
            embed_model="fixture",
        )
    )
    db.commit()

    for reader in (owner, peer, admin):
        hits = service.unified_retrieve(
            db,
            user_id=reader.id,
            agent_code="manager",
            query="enterprise source",
            top_k=50,
        )
        assert legacy_doc.id not in {hit["doc_id"] for hit in hits}
        assert legacy_doc.id not in {row.id for row in service.list_docs(db, agent_code="manager", user_id=reader.id)}
        assert exact_project_reference_doc.id not in {
            row.id for row in service.list_docs(db, agent_code="manager", user_id=reader.id)
        }
        exact_hits = service.unified_retrieve(
            db,
            user_id=reader.id,
            agent_code="manager",
            query="EXACT_PROJECT_REFERENCE_SECRET",
            top_k=50,
        )
        assert exact_project_reference_doc.id not in {hit["doc_id"] for hit in exact_hits}

    with pytest.raises(ForbiddenError, match="企业绑定项目"):
        service.upsert_source(
            db,
            agent_code="manager",
            source_type="project",
            source_uri=str(project.id),
            whitelist=1,
            enabled=1,
            config={"project_id": project.id},
        )
    with pytest.raises(ForbiddenError, match="企业绑定项目"):
        service._project_source_payload(db, source, {"project_id": project.id})
    embedding_calls = []
    monkeypatch.setattr(
        service.embedding_service,
        "embed_texts",
        lambda _db, texts, **_kwargs: (embedding_calls.append(list(texts)) or [[1.0] for _ in texts], "fixture"),
    )
    with pytest.raises(ForbiddenError, match="企业绑定项目"):
        service.add_document(
            db,
            agent_code="manager",
            title="blocked direct ingest",
            content="enterprise source content",
            source_type="project",
            source_ref=f"project:{project.id}:code_file:{source_file.id}",
        )
    assert embedding_calls == []

    with pytest.raises(ForbiddenError, match="无法确认项目来源"):
        service.add_document(
            db,
            agent_code="manager",
            title="unverifiable project source",
            content="must never leave the server",
            source_type="project",
            source_ref="project:not-an-id:code_file:999999",
        )
    assert embedding_calls == []

    result = service.crawl_enabled_sources(db, agent_code="manager", user_id=admin.id)
    assert result["doc_count"] == 0
    assert any(item["source_id"] == source.id for item in result["skipped"])
    db.refresh(legacy_doc)
    db.refresh(exact_project_reference_doc)
    assert legacy_doc.status == "deleted"
    assert exact_project_reference_doc.status == "deleted"
    assert db.query(AgentKnowledgeChunk).filter_by(doc_id=exact_project_reference_doc.id).count() == 0
    assert not any(
        hit["doc_id"] == legacy_doc.id
        for hit in service.unified_retrieve(
            db,
            user_id=peer.id,
            agent_code="manager",
            query="enterprise source",
            top_k=50,
        )
    )


def test_project_knowledge_source_ids_must_use_canonical_ascii_decimal(db, private_knowledge, monkeypatch):
    """项目来源只接受标准正整数，避免绑定清理精确引用时留下旧切片。"""
    owner, _peer, _admin, _ = private_knowledge
    project = Project(user_id=owner.id, project_name="非规范 Agent 知识来源")
    db.add(project)
    db.flush()
    source_file = CodeFile(
        project_id=project.id,
        file_name="source.py",
        file_path="source.py",
        language="python",
        size_bytes=10,
        content="personal project source",
        status="active",
        is_binary=0,
    )
    db.add(source_file)
    db.commit()
    invalid_refs = (
        f"project:0{project.id}",
        f"project:{project.id}:code_file:0{source_file.id}",
        f"code_file:0{source_file.id}",
    )
    embedding_calls = []
    monkeypatch.setattr(
        service.embedding_service,
        "embed_texts",
        lambda _db, texts, **_kwargs: (embedding_calls.append(list(texts)) or [[1.0] for _ in texts], "fixture"),
    )

    for source_ref in invalid_refs:
        with pytest.raises(ForbiddenError, match="无法确认项目来源"):
            service.add_document(
                db,
                agent_code="manager",
                title="非规范项目来源",
                content="must not be embedded",
                source_type="project",
                source_ref=source_ref,
                user_id=owner.id,
            )
    assert embedding_calls == []


def test_crawler_rechecks_binding_after_fetch_before_remote_embedding(db, private_knowledge, monkeypatch):
    """来源抓取完成后项目刚被绑定，也不能把正文送去外部嵌入。"""
    owner, _peer, admin, _ = private_knowledge
    project = Project(user_id=owner.id, project_name="抓取期间被绑定")
    db.add(project)
    db.flush()
    source = AgentKnowledgeSource(
        agent_code="manager",
        source_type="project",
        source_uri=str(project.id),
        whitelist=1,
        enabled=1,
        config_json=json.dumps({"project_id": project.id}),
    )
    db.add(source)
    db.commit()

    embedding_calls = []
    monkeypatch.setattr(
        service.embedding_service,
        "embed_texts",
        lambda _db, texts, **_kwargs: (embedding_calls.append(list(texts)) or [[1.0] for _ in texts], "fixture"),
    )

    def bind_after_fetch(session, _source):
        session.add(EnterpriseProject(
            enterprise_id=9007,
            project_id=project.id,
            group_id=9008,
            status="active",
        ))
        session.commit()
        return [{
            "title": "fetched project text",
            "content": "must stay inside the company boundary",
            "source_ref": f"project:{project.id}",
        }]

    monkeypatch.setattr(service, "_resolve_source_payload", bind_after_fetch)
    result = service.crawl_enabled_sources(db, agent_code="manager", user_id=admin.id)

    assert result["doc_count"] == 0
    assert embedding_calls == []
    assert result["skipped"] == [{
        "source_id": source.id,
        "reason": "项目来源已绑定企业或无法验证，跳过全局知识入库",
    }]
    db.refresh(source)
    assert source.enabled == 0 and source.whitelist == 0


def test_project_source_document_stays_blocked_after_source_file_row_is_removed(db, private_knowledge):
    owner, peer, _admin, _ = private_knowledge
    project = Project(user_id=owner.id, project_name="绑定后删除源码行")
    db.add(project)
    db.flush()
    db.add(
        EnterpriseProject(
            enterprise_id=9003,
            project_id=project.id,
            group_id=9004,
            status="active",
        )
    )
    source = AgentKnowledgeSource(
        agent_code="manager",
        source_type="project",
        source_uri=str(project.id),
        whitelist=1,
        enabled=1,
        config_json=json.dumps({"project_id": project.id}),
    )
    db.add(source)
    db.flush()
    doc = AgentKnowledgeDoc(
        agent_code="manager",
        source_id=source.id,
        title="enterprise source with a removed file row",
        source_type="project",
        source_ref="code_file:999999",
        status="active",
        risk_level="low",
        confidence=1.0,
        char_count=30,
        chunk_count=1,
    )
    db.add(doc)
    db.flush()
    db.add(
        AgentKnowledgeChunk(
            doc_id=doc.id,
            agent_code="manager",
            seq=0,
            content="ENTERPRISE_SOURCE_AFTER_FILE_DELETE",
            embedding="[1.0,0.0]",
            embed_model="fixture",
        )
    )
    db.commit()

    hits = service.unified_retrieve(
        db,
        user_id=peer.id,
        agent_code="manager",
        query="enterprise source",
        top_k=50,
    )
    assert doc.id not in {hit["doc_id"] for hit in hits}


def test_repointing_bound_source_retires_documents_after_source_file_deletion(db, private_knowledge):
    owner, peer, admin, _ = private_knowledge
    bound_project = Project(user_id=owner.id, project_name="已绑定来源")
    replacement_project = Project(user_id=owner.id, project_name="新的个人来源")
    db.add_all([bound_project, replacement_project])
    db.flush()
    source_file = CodeFile(
        project_id=bound_project.id,
        file_name="secret.py",
        file_path="secret.py",
        language="python",
        size_bytes=20,
        raw_size=20,
        content="BOUND_SOURCE_MUST_STAY_RETIRED",
        status="active",
        is_binary=0,
    )
    source = service.upsert_source(
        db,
        agent_code="manager",
        source_type="project",
        source_uri=str(bound_project.id),
        config={"project_id": bound_project.id},
    )
    db.add(source_file)
    db.commit()
    assert service.crawl_enabled_sources(db, agent_code="manager", user_id=admin.id)["doc_count"] == 1
    doc = db.query(AgentKnowledgeDoc).filter_by(source_id=source.id).one()

    db.add(
        EnterpriseProject(
            enterprise_id=9005,
            project_id=bound_project.id,
            group_id=9006,
            status="active",
        )
    )
    db.commit()
    db.delete(source_file)
    db.commit()
    assert doc.id not in {
        hit["doc_id"]
        for hit in service.unified_retrieve(
            db,
            user_id=peer.id,
            agent_code="manager",
            query="bound source",
            top_k=50,
        )
    }

    service.upsert_source(
        db,
        agent_code="manager",
        source_type="project",
        source_uri=str(replacement_project.id),
        config={"project_id": replacement_project.id},
        source_id=source.id,
    )
    db.refresh(doc)
    assert doc.status == "deleted"
    assert doc.id not in {
        hit["doc_id"]
        for hit in service.unified_retrieve(
            db,
            user_id=peer.id,
            agent_code="manager",
            query="bound source",
            top_k=50,
        )
    }
