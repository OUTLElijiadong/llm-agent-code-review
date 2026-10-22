"""远端嵌入按实际 HTTP 记账;失败明确披露,重嵌保留旧向量。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import ExternalServiceError
from app.models.ai_call_log import AiCallLog
from app.services import embedding_service, knowledge_service
from app.services.ai_usage_context import UsageAccountingError, usage_context


@pytest.fixture
def ledger_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'embedding-ledger.sqlite'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db
    engine.dispose()


@pytest.fixture
def remote(monkeypatch):
    calls, actions = [], []
    original_client = httpx.Client
    config = {
        "enabled": True,
        "base_url": "https://embed.example/v1",
        "api_key": "sk-private-test",
        "model": "embedding-model",
    }

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        action = (
            actions.pop(0)
            if actions
            else (
                200,
                {
                    "model": "actual-embedding",
                    "usage": {"prompt_tokens": 0, "total_tokens": 8},
                    "data": [{"index": i, "embedding": [0.6, 0.8]} for i in range(len(body["input"]))],
                },
            )
        )
        if isinstance(action, Exception):
            raise action
        return httpx.Response(action[0], json=action[1])

    monkeypatch.setattr(embedding_service.system_config_service, "get_embedding_config", lambda _db: config)
    monkeypatch.setattr(embedding_service, "validate_ai_base_url", lambda url, **kwargs: url)
    monkeypatch.setattr(
        embedding_service,
        "pin_public_http_url",
        lambda url: SimpleNamespace(request_url=url, host_header="embed.example", request_extensions={}),
    )
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(embedding_service.time, "sleep", lambda _seconds: None)
    return calls, actions, config


def test_embedding_each_batch_records_provider_usage_without_text_or_vectors(ledger_db, remote):
    calls, _actions, _config = remote
    vectors, tag = embedding_service.embed_texts(ledger_db, ["private source text"] * 33, user_id=7)
    ledger_db.rollback()
    rows = ledger_db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert len(calls) == len(rows) == 2 and len(vectors) == 33
    assert tag == "api:embedding-model"
    assert all(
        (row.user_id, row.status, row.agent_label, row.model_name) == (7, "success", "embedding", "actual-embedding")
        for row in rows
    )
    assert all((row.prompt_tokens, row.completion_tokens, row.total_tokens) == (0, None, 8) for row in rows)
    assert all(row.prompt is None and row.response is None and row.error_message is None for row in rows)
    assert [len(json.loads(call.content)["input"]) for call in calls] == [32, 1]


@pytest.mark.parametrize(
    "action,total,error_name",
    [
        ((503, {"usage": {"prompt_tokens": 3, "total_tokens": 3}, "error": "sk-private-test"}), 3, "HTTPStatusError"),
        ((200, {"usage": {"total_tokens": 5}, "data": "invalid"}), 5, "ValueError"),
        (httpx.ReadTimeout("sk-private-test"), None, "ReadTimeout"),
    ],
)
def test_embedding_failure_keeps_reported_usage_and_raises(ledger_db, remote, action, total, error_name):
    calls, actions, _config = remote
    actions.append(action)
    with pytest.raises(ExternalServiceError, match="远端嵌入失败") as error:
        embedding_service.embed_texts(ledger_db, ["private source"], user_id=7)
    assert error_name in str(error.value) and "sk-private-test" not in str(error.value)
    ledger_db.rollback()
    row = ledger_db.query(AiCallLog).one()
    assert len(calls) == 1
    assert (row.status, row.total_tokens, row.completion_tokens) == ("failed", total, None)
    assert row.error_message == error_name and row.prompt is None and row.response is None


def test_embedding_preflight_disabled_and_empty_inputs_do_not_create_model_calls(ledger_db, remote, monkeypatch):
    calls, _actions, config = remote
    active_config = dict(config)
    config.update(enabled=False, base_url="", api_key="")
    vectors, tag = embedding_service.embed_texts(ledger_db, ["x"], user_id=7)
    assert vectors and tag == embedding_service.FALLBACK_TAG
    embedding_service.embed_texts(ledger_db, [], user_id=7)
    config.update(active_config)
    monkeypatch.setattr(
        embedding_service, "pin_public_http_url", lambda _url: (_ for _ in ()).throw(ValueError("preflight"))
    )
    with pytest.raises(ExternalServiceError, match="远端嵌入失败"):
        embedding_service.embed_texts(ledger_db, ["x"], user_id=7)
    assert calls == [] and ledger_db.query(AiCallLog).count() == 0


def test_embedding_unknown_owner_stays_null_and_trusted_context_is_used(ledger_db, remote):
    embedding_service.embed_one(ledger_db, "system")
    with usage_context(7, {}, db=ledger_db):
        embedding_service.embed_one(ledger_db, "user")
    rows = ledger_db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert [row.user_id for row in rows] == [None, 7]
    assert all(row.root_agent_run_id is None for row in rows)


def test_personal_knowledge_actual_embedding_uses_service_owner(ledger_db, remote):
    doc = knowledge_service.add_document(ledger_db, 7, "test", "source text")
    knowledge_service.retrieve(ledger_db, 7, "source")
    rows = ledger_db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert doc.chunk_count == 1 and len(remote[0]) == len(rows) == 2
    assert [row.user_id for row in rows] == [7, 7]


def test_embedding_accounting_failure_does_not_fallback_or_reembed_retry(ledger_db, remote, monkeypatch):
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc

    def fail_accounting(**kwargs):
        raise UsageAccountingError("ledger unavailable")

    doc = KnowledgeDoc(user_id=7, title="test", status="active", source_type="manual", chunk_count=1, char_count=1)
    ledger_db.add(doc)
    ledger_db.commit()
    ledger_db.add(
        KnowledgeChunk(doc_id=doc.id, user_id=7, seq=0, content="x", embedding="[]", embed_model="fallback:hash")
    )
    ledger_db.commit()
    monkeypatch.setattr(embedding_service, "record_usage_attempt", fail_accounting)
    with pytest.raises(UsageAccountingError):
        embedding_service.reembed_all_stores(ledger_db, user_id=7)
    assert len(remote[0]) == 1
    assert ledger_db.query(KnowledgeChunk).one().embed_model == "fallback:hash"


def test_reembed_real_retry_is_one_row_per_http_and_uses_admin_owner(ledger_db, remote):
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc

    doc = KnowledgeDoc(user_id=1, title="test", status="active", source_type="manual", chunk_count=1, char_count=1)
    ledger_db.add(doc)
    ledger_db.commit()
    ledger_db.add(
        KnowledgeChunk(doc_id=doc.id, user_id=1, seq=0, content="x", embedding="[]", embed_model="fallback:hash")
    )
    ledger_db.commit()
    remote[1].append((503, {"usage": {"total_tokens": 2}}))
    stats = embedding_service.reembed_all_stores(ledger_db, user_id=7)
    rows = ledger_db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert len(remote[0]) == len(rows) == 2
    assert [(row.user_id, row.status, row.total_tokens) for row in rows] == [(7, "failed", 2), (7, "success", 8)]
    assert stats == {"kb_chunks": 1, "agent_chunks": 0, "failed_batches": 0}


def test_agent_knowledge_and_capability_ranking_preserve_trusted_root(ledger_db, remote):
    from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
    from app.services import agent_knowledge_service, capability_catalog_service

    root = AgentResponseRun(
        user_id=7,
        run_id="embedding-root",
        surface="user",
        session_key="test",
        status="running",
        checkpoint_json="{}",
    )
    ledger_db.add(root)
    ledger_db.commit()
    tool = AgentToolExecution(
        run_id=root.run_id,
        user_id=7,
        call_id="knowledge",
        tool_name="knowledge_note_save",
        request_id="a" * 64,
        arguments_json="{}",
        status="running",
    )
    ledger_db.add(tool)
    ledger_db.commit()
    origin = {"root_agent_run_id": root.id, "agent_run_id": root.id, "tool_execution_id": tool.id}
    with usage_context(7, origin, db=ledger_db):
        agent_knowledge_service.add_document(
            ledger_db,
            agent_code="review",
            title="test",
            content="source",
            user_id=7,
        )
        agent_knowledge_service.unified_retrieve(ledger_db, user_id=7, agent_code="review", query="source")
        capability_catalog_service.rank_rows(
            ledger_db,
            [{"code": "review", "name": "source", "description": "test"}],
            "source",
            user_id=7,
        )
    ledger_db.rollback()
    rows = ledger_db.query(AiCallLog).all()
    # Agent 入库 1 次，个人/Agent 联合检索各 1 次，候选重排 1 次。
    assert len(remote[0]) == len(rows) == 4
    assert all(row.user_id == 7 for row in rows)
    assert all(
        (row.root_agent_run_id, row.agent_run_id, row.tool_execution_id) == (root.id, root.id, tool.id) for row in rows
    )


def test_embedding_no_usage_stays_unknown_and_untrusted_model_key_is_redacted(ledger_db, remote):
    remote[1].append(
        (
            200,
            {
                "model": "provider-sk-private-test\nmodel",
                "data": [{"index": 0, "embedding": [0.6, 0.8]}],
            },
        )
    )
    embedding_service.embed_one(ledger_db, "private input", user_id=7)
    row = ledger_db.query(AiCallLog).one()
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (None, None, None)
    assert row.model_name == "provider-[redacted]model"
    assert row.prompt is None and row.response is None


def _stored_embedding(db, domain, content="old source", *, tag="api:old-model"):
    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc

    if domain == "kb":
        doc = KnowledgeDoc(user_id=7, title="old", source_type="manual", source_ref=content,
                           status="active", chunk_count=1, char_count=len(content))
        chunk_type = KnowledgeChunk
        owner = {"user_id": 7}
    else:
        doc = AgentKnowledgeDoc(agent_code="review", title="old", source_type="manual", source_ref=content,
                                status="active", chunk_count=1, char_count=len(content))
        chunk_type = AgentKnowledgeChunk
        owner = {"agent_code": "review"}
    db.add(doc)
    db.flush()
    chunk = chunk_type(doc_id=doc.id, seq=0, content=content, embedding="[0.25, -0.75]", embed_model=tag,
                       **owner)
    db.add(chunk)
    db.commit()
    return chunk


@pytest.mark.parametrize("failure", ["http", "timeout", "incomplete"])
def test_reembed_exhausted_remote_failure_preserves_both_domains(ledger_db, remote, failure):
    chunks = [_stored_embedding(ledger_db, domain) for domain in ("kb", "agent")]
    snapshots = [(chunk.embedding, chunk.embed_model) for chunk in chunks]
    if failure == "http":
        action = (503, {"usage": {"total_tokens": 3}})
    elif failure == "timeout":
        action = httpx.ReadTimeout("private provider detail")
    else:
        action = (200, {"data": [], "usage": {"total_tokens": 3}})
    remote[1].extend([action] * 6)

    stats = embedding_service.reembed_all_stores(ledger_db, user_id=7)

    ledger_db.expire_all()
    assert [(chunk.embedding, chunk.embed_model) for chunk in chunks] == snapshots
    assert stats == {"kb_chunks": 0, "agent_chunks": 0, "failed_batches": 2}
    calls = ledger_db.query(AiCallLog).all()
    assert len(remote[0]) == len(calls) == 6
    assert all(call.status == "failed" and call.user_id == 7 for call in calls)


def test_reembed_failed_batch_continues_and_can_be_recovered(ledger_db, remote):
    failed = _stored_embedding(ledger_db, "kb", "first")
    following = _stored_embedding(ledger_db, "kb", "second")
    agent = _stored_embedding(ledger_db, "agent", "third")
    snapshot = (failed.embedding, failed.embed_model)
    remote[1].extend([(503, {})] * 3)

    stats = embedding_service.reembed_all_stores(ledger_db, batch_size=1, user_id=7)

    assert stats == {"kb_chunks": 1, "agent_chunks": 1, "failed_batches": 1}
    ledger_db.expire_all()
    assert (failed.embedding, failed.embed_model) == snapshot
    assert following.embed_model == agent.embed_model == "api:embedding-model"
    assert json.loads(following.embedding) == json.loads(agent.embedding) == [0.6, 0.8]
    assert [json.loads(call.content)["input"] for call in remote[0]] == [
        ["first"], ["first"], ["first"], ["second"], ["third"],
    ]

    recovered = embedding_service.reembed_all_stores(ledger_db, batch_size=1, user_id=7)

    assert recovered == {"kb_chunks": 1, "agent_chunks": 0, "failed_batches": 0}
    ledger_db.expire_all()
    assert failed.embed_model == "api:embedding-model"
    assert len(remote[0]) == 6


@pytest.mark.parametrize("domain", ["kb", "agent", "agent_strict"])
def test_remote_failure_rejects_new_knowledge_without_hash_chunks(ledger_db, remote, domain):
    from app.models.agent_governance import AgentKnowledgeDoc
    from app.models.knowledge_doc import KnowledgeDoc
    from app.services import agent_knowledge_service

    chunk = _stored_embedding(ledger_db, "kb" if domain == "kb" else "agent")
    doc_type = KnowledgeDoc if domain == "kb" else AgentKnowledgeDoc
    document_count = ledger_db.query(doc_type).count()
    snapshot = (chunk.embedding, chunk.embed_model)
    remote[1].append((503, {"error": "sk-private-test", "usage": {"total_tokens": 2}}))

    with pytest.raises(ExternalServiceError, match="远端嵌入失败") as error:
        if domain == "kb":
            knowledge_service.add_document(ledger_db, 7, "new", "new source", source_ref="old source")
        else:
            agent_knowledge_service.add_document(
                ledger_db, agent_code="review", title="new", content="new source", user_id=7,
                source_ref="old source", require_api_embedding=domain == "agent_strict",
            )

    assert "sk-private-test" not in str(error.value)
    assert ledger_db.query(doc_type).count() == document_count
    ledger_db.rollback()
    ledger_db.expire_all()
    assert ledger_db.query(doc_type).count() == document_count
    assert (chunk.embedding, chunk.embed_model) == snapshot
    assert ledger_db.query(type(chunk)).count() == 1
    assert len(remote[0]) == 1
    assert ledger_db.query(AiCallLog).one().status == "failed"


@pytest.mark.parametrize("data", [
    [],
    [{"index": 0, "embedding": [0.6, 0.8]}],
    [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [1.0]}],
    [{"index": 1, "embedding": [1.0]}, {"index": 2, "embedding": [1.0]}],
    [{"embedding": [1.0]}, {"embedding": [1.0]}],
    [{"index": 0, "embedding": []}, {"index": 1, "embedding": []}],
    [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [0.6, 0.8]}],
    [{"index": 0, "embedding": [True]}, {"index": 1, "embedding": [1.0]}],
    [{"index": 0, "embedding": ["nan"]}, {"index": 1, "embedding": [1.0]}],
    [{"index": 0, "embedding": ["inf"]}, {"index": 1, "embedding": [1.0]}],
])
def test_invalid_remote_vectors_fail_instead_of_succeeding_or_hashing(ledger_db, remote, data):
    remote[1].append((200, {"data": data, "usage": {"total_tokens": 5}}))

    with pytest.raises(ExternalServiceError, match="远端嵌入失败"):
        embedding_service.embed_texts(ledger_db, ["first", "second"], user_id=7)

    call = ledger_db.query(AiCallLog).one()
    assert call.status == "failed" and call.total_tokens == 5


def test_reembed_incomplete_batch_never_partially_overwrites(ledger_db, remote):
    chunks = [_stored_embedding(ledger_db, domain, str(index))
              for domain in ("kb", "agent") for index in range(2)]
    snapshots = [(chunk.embedding, chunk.embed_model) for chunk in chunks]
    remote[1].extend([(200, {"data": [{"index": 0, "embedding": [0.6, 0.8]}]})] * 6)

    stats = embedding_service.reembed_all_stores(ledger_db, user_id=7)

    ledger_db.expire_all()
    assert [(chunk.embedding, chunk.embed_model) for chunk in chunks] == snapshots
    assert stats == {"kb_chunks": 0, "agent_chunks": 0, "failed_batches": 2}


def test_remote_embedding_dimensions_must_match_across_http_batches(ledger_db, remote):
    remote[1].extend([
        (200, {"data": [{"index": i, "embedding": [0.6, 0.8]} for i in range(32)]}),
        (200, {"data": [{"index": 0, "embedding": [1.0]}]}),
    ])

    with pytest.raises(ExternalServiceError, match="远端嵌入失败"):
        embedding_service.embed_texts(ledger_db, ["source"] * 33, user_id=7)

    assert [row.status for row in ledger_db.query(AiCallLog).order_by(AiCallLog.id)] == ["success", "failed"]


def test_reembed_includes_unembedded_chunks_in_both_domains(ledger_db, remote):
    chunks = [_stored_embedding(ledger_db, domain, tag=None) for domain in ("kb", "agent")]

    stats = embedding_service.reembed_all_stores(ledger_db, user_id=7)

    assert stats == {"kb_chunks": 1, "agent_chunks": 1, "failed_batches": 0}
    assert all(chunk.embed_model == "api:embedding-model" for chunk in chunks)


@pytest.mark.parametrize("domain", ["kb", "agent"])
def test_disabled_configured_remote_preserves_vectors_and_rejects_new_data(ledger_db, remote, domain):
    from app.services import agent_knowledge_service

    chunk = _stored_embedding(ledger_db, domain)
    snapshot = (chunk.embedding, chunk.embed_model)
    # 配置解析也可能因端点安全校验失败返回 enabled=False,不能当成本地模式。
    remote[2]["enabled"] = False

    stats = embedding_service.reembed_all_stores(ledger_db, user_id=7)

    ledger_db.expire_all()
    assert (chunk.embedding, chunk.embed_model) == snapshot
    assert stats == {"kb_chunks": 0, "agent_chunks": 0, "failed_batches": 1}
    with pytest.raises(ExternalServiceError, match="远端嵌入"):
        if domain == "kb":
            knowledge_service.add_document(ledger_db, 7, "new", "new source")
        else:
            agent_knowledge_service.add_document(
                ledger_db, agent_code="review", title="new", content="new source", user_id=7,
            )
    assert ledger_db.query(type(chunk)).count() == 1
    assert remote[0] == [] and ledger_db.query(AiCallLog).count() == 0


@pytest.mark.parametrize("vectors,tag", [
    ([], "api:embedding"),
    (None, "api:embedding"),
    ([[1.0, 0.0]], "api:embedding"),
    ([[], []], "api:embedding"),
    ([[1.0], [1.0, 0.0]], "api:embedding"),
    ([[True], [1.0]], "api:embedding"),
    ([[float("nan")], [1.0]], "api:embedding"),
    ([[1.0], [1.0]], None),
])
def test_agent_embedding_validation_precedes_document_persistence(ledger_db, monkeypatch, vectors, tag):
    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
    from app.services import agent_knowledge_service

    monkeypatch.setattr(embedding_service, "embed_texts", lambda *_args, **_kwargs: (vectors, tag))
    with pytest.raises(RuntimeError, match="嵌入"):
        agent_knowledge_service.add_document(
            ledger_db, agent_code="review", title="invalid embedding", content="x" * 701, user_id=7,
        )

    ledger_db.rollback()
    assert ledger_db.query(AgentKnowledgeDoc).count() == 0
    assert ledger_db.query(AgentKnowledgeChunk).count() == 0


def test_agent_document_and_chunks_are_visible_in_the_same_commit(ledger_db, monkeypatch):
    from sqlalchemy import event

    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
    from app.services import agent_knowledge_service

    snapshots = []
    monkeypatch.setattr(
        embedding_service, "embed_texts",
        lambda _db, texts, **_kwargs: ([[0.6, 0.8] for _ in texts], "api:embedding"),
    )

    def inspect_committed_rows(_session):
        with sessionmaker(bind=ledger_db.get_bind())() as reader:
            snapshots.append((reader.query(AgentKnowledgeDoc).count(), reader.query(AgentKnowledgeChunk).count()))

    event.listen(ledger_db, "after_commit", inspect_committed_rows)
    try:
        doc = agent_knowledge_service.add_document(
            ledger_db, agent_code="review", title="atomic", content="x" * 701, user_id=7,
        )
    finally:
        event.remove(ledger_db, "after_commit", inspect_committed_rows)

    assert doc.chunk_count == 2
    assert snapshots == [(1, 2)]


def test_agent_chunk_write_failure_does_not_commit_empty_document(ledger_db, monkeypatch):
    from sqlalchemy import event

    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
    from app.services import agent_knowledge_service

    monkeypatch.setattr(embedding_service, "embed_texts", lambda *_args, **_kwargs: ([[0.6, 0.8]], "api:embedding"))

    def reject_chunk_write(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("INSERT INTO AGENT_KNOWLEDGE_CHUNK "):
            raise RuntimeError("chunk write failed")

    engine = ledger_db.get_bind()
    event.listen(engine, "before_cursor_execute", reject_chunk_write)
    try:
        with pytest.raises(RuntimeError, match="chunk write failed"):
            agent_knowledge_service.add_document(
                ledger_db, agent_code="review", title="failed write", content="source", user_id=7,
            )
    finally:
        event.remove(engine, "before_cursor_execute", reject_chunk_write)
        ledger_db.rollback()

    assert ledger_db.query(AgentKnowledgeDoc).count() == 0
    assert ledger_db.query(AgentKnowledgeChunk).count() == 0


@pytest.mark.parametrize("risk,confidence,status", [
    ("low", 1.0, "active"),
    ("high", 1.0, "pending_approval"),
    ("low", 0.2, "pending_approval"),
])
def test_agent_embedding_success_preserves_approval_policy(ledger_db, remote, monkeypatch, risk, confidence, status):
    from app.models.agent_governance import AgentKnowledgeChunk
    from app.services import agent_knowledge_service

    approvals = []
    monkeypatch.setattr(
        agent_knowledge_service.approval_service, "create_or_auto_decide",
        lambda _db, **kwargs: approvals.append(kwargs),
    )
    doc = agent_knowledge_service.add_document(
        ledger_db, agent_code="review", title="approval", content="source", user_id=7,
        risk_level=risk, confidence=confidence,
    )

    assert doc.status == status and doc.chunk_count == 1
    assert ledger_db.query(AgentKnowledgeChunk).filter_by(doc_id=doc.id).count() == 1
    assert len(remote[0]) == 1
    assert len(approvals) == (1 if status == "pending_approval" else 0)
    if approvals:
        assert (approvals[0]["action"], approvals[0]["resource"], approvals[0]["decision"]) == (
            "knowledge.activate", f"agent_knowledge_doc:{doc.id}", "escalate",
        )
        assert approvals[0]["risk_level"] == risk


def test_agent_private_source_is_rejected_before_embedding_or_persistence(ledger_db, remote):
    from app.core.exceptions import ForbiddenError
    from app.models.agent_governance import AgentKnowledgeDoc
    from app.services import agent_knowledge_service

    with pytest.raises(ForbiddenError, match="私人知识来源不属于当前账号"):
        agent_knowledge_service.add_document(
            ledger_db, agent_code="review", title="forged", content="source", user_id=7,
            source_ref="response_run:missing-run",
        )

    assert remote[0] == []
    assert ledger_db.query(AgentKnowledgeDoc).count() == 0
