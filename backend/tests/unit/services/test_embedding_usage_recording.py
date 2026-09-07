"""现有远端嵌入每次实际 HTTP 记账，哈希降级与未知用量保持真实。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
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
        ((200, {"usage": {"total_tokens": 5}, "data": "invalid"}), 5, "AttributeError"),
        (httpx.ReadTimeout("sk-private-test"), None, "ReadTimeout"),
    ],
)
def test_embedding_failure_keeps_reported_usage_then_falls_back(ledger_db, remote, action, total, error_name):
    calls, actions, _config = remote
    actions.append(action)
    vectors, tag = embedding_service.embed_texts(ledger_db, ["private source"], user_id=7)
    ledger_db.rollback()
    row = ledger_db.query(AiCallLog).one()
    assert len(calls) == 1 and vectors and tag == embedding_service.FALLBACK_TAG
    assert (row.status, row.total_tokens, row.completion_tokens) == ("failed", total, None)
    assert row.error_message == error_name and row.prompt is None and row.response is None


def test_embedding_preflight_disabled_and_empty_inputs_do_not_create_model_calls(ledger_db, remote, monkeypatch):
    calls, _actions, config = remote
    config["enabled"] = False
    embedding_service.embed_texts(ledger_db, ["x"], user_id=7)
    embedding_service.embed_texts(ledger_db, [], user_id=7)
    config["enabled"] = True
    monkeypatch.setattr(
        embedding_service, "pin_public_http_url", lambda _url: (_ for _ in ()).throw(ValueError("preflight"))
    )
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
