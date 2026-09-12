"""每次真实 HTTP 的精确记账、去重、独立事务及失败边界。"""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.agents.base import AgentContext, BaseAgent
from app.ai.deepseek_agent import DeepSeekAgent
from app.core.database import Base
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.ai_call_log import AiCallLog
from app.models.code_file import CodeFile
from app.models.review_task import ReviewTask
from app.services.ai_usage_context import UsageAccountingError, current_attribution, model_attribution, usage_context


@pytest.fixture
def ledger_db(tmp_path):
    # 文件 SQLite 的不同连接，能检验审计事务不被业务 rollback 撤销。
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite'}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def enforce_fk(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        root = AgentResponseRun(
            user_id=7,
            run_id="ledger-root",
            surface="user",
            session_key="unit",
            status="completed",
            checkpoint_json="{}",
        )
        db.add(root)
        db.commit()
        tool = AgentToolExecution(
            run_id=root.run_id,
            user_id=7,
            call_id="tool-call",
            tool_name="scan",
            request_id="a" * 64,
            arguments_json="{}",
            status="success",
        )
        task = ReviewTask(
            user_id=7,
            project_id=1,
            task_name="ledger-review",
            review_type="full",
            status="running",
            root_agent_run_id=root.id,
            agent_run_id=root.id,
        )
        code = CodeFile(
            project_id=1,
            file_name="unit.py",
            file_path="unit.py",
            language="python",
            content="x=1",
            version_no=1,
            is_binary=0,
            status="active",
        )
        db.add_all([tool, task, code])
        db.commit()
        yield (
            db,
            {"root_agent_run_id": root.id, "agent_run_id": root.id, "tool_execution_id": tool.id},
            task.id,
            code.id,
        )
    engine.dispose()


def _body(usage=True, finish="stop", content='{"ok":true}'):
    result = {"model": "reported-model", "choices": [{"finish_reason": finish, "message": {"content": content}}]}
    if usage:
        result["usage"] = {"prompt_tokens": 0, "completion_tokens": 9, "total_tokens": 9}
    return result


def _base(monkeypatch, script):
    from app.agents import base

    requests = []
    original_client = httpx.Client

    def handler(request):
        requests.append(request)
        action = script.pop(0)
        if isinstance(action, Exception):
            raise action
        return httpx.Response(action[0], json=action[1])

    monkeypatch.setattr(
        base.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(
        base,
        "pin_public_http_url",
        lambda url: SimpleNamespace(request_url=url, host_header="unit.example", request_extensions={}),
    )
    monkeypatch.setattr(base.time, "sleep", lambda _seconds: None)
    agent = BaseAgent(system_prompt="unit", model="requested-model")
    agent._base_url, agent._api_key, agent._max_retries = "https://unit.example/v1", "unit", 1
    agent._emit = lambda *_a, **_kw: None
    return agent, requests


def test_real_base_retry_each_attempt_and_deferred_enrichment_are_exactly_once(ledger_db, monkeypatch):
    db, origin, task_id, file_id = ledger_db
    agent, requests = _base(monkeypatch, [(503, {"usage": {"total_tokens": 4}}), (200, _body())])
    with usage_context(7, origin, db=db):
        result = agent.call_json("unit", AgentContext(user_id=7, task_id=task_id, file_id=file_id))
    assert result.success and len(requests) == 2 and len(result.usage_log_ids) == 2
    agent._log_call(db, user_id=7, task_id=task_id, file_id=file_id, chunk_index=102, result=result)
    agent._log_call(db, user_id=7, task_id=task_id, file_id=file_id, chunk_index=102, result=result)
    db.rollback()
    rows = db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert [row.id for row in rows] == result.usage_log_ids
    assert [(row.status, row.total_tokens) for row in rows] == [("retry", 4), ("success", 9)]
    assert rows[-1].error_message is None and rows[-1].model_name == "reported-model"
    assert rows[-1].prompt_tokens == 0
    assert all((row.task_id, row.file_id, row.chunk_index) == (task_id, file_id, 102) for row in rows)
    assert all(model_attribution(row) == origin for row in rows)


@pytest.mark.parametrize(
    "status,body,total,success",
    [
        (200, _body(usage=False), None, True),
        (400, {"usage": {"total_tokens": 6}}, 6, False),
        (200, _body(finish="length"), 9, False),
        (200, _body(content="not-json"), 9, False),
    ],
)
def test_real_base_missing_usage_errors_and_truncation_are_recorded(
    ledger_db, monkeypatch, status, body, total, success
):
    db, origin, _task, _file = ledger_db
    agent, requests = _base(monkeypatch, [(status, body)])
    with usage_context(7, origin, db=db):
        result = agent.call_json("unit")
    db.rollback()
    row = db.query(AiCallLog).one()
    assert len(requests) == 1 and result.success is success
    assert result.usage_log_ids == [row.id] and row.total_tokens == total
    assert model_attribution(row) == origin


def test_real_base_worker_uses_own_session_and_preserves_context(ledger_db, monkeypatch):
    db, origin, _task, _file = ledger_db
    agent, requests = _base(monkeypatch, [(200, _body())])
    with usage_context(7, origin, db=db), ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(copy_context().run, agent.call_json, "unit").result()
    assert len(requests) == 1 and result.success
    assert current_attribution(7) == {}
    assert model_attribution(db.query(AiCallLog).one()) == origin


def test_explicit_source_records_without_context_but_fake_database_does_not_connect(ledger_db, monkeypatch):
    db, _origin, _task, _file = ledger_db
    agent, requests = _base(monkeypatch, [(200, _body()), (200, _body())])
    agent.bind_usage_source(db, SimpleNamespace(id=7))
    result = agent.call("unit")
    assert len(result.usage_log_ids) == 1
    row = db.query(AiCallLog).one()
    assert row.user_id == 7 and model_attribution(row) == {}
    agent.bind_usage_source(SimpleNamespace(), SimpleNamespace(id=7))
    result = agent.call("unit", AgentContext(user_id=7))
    assert len(requests) == 2 and result.usage_log_ids == []
    assert db.query(AiCallLog).count() == 1


@pytest.mark.parametrize("kind", ["base", "raw", "chat"])
def test_accounting_commit_failure_does_not_resend_successful_model_request(ledger_db, monkeypatch, kind):
    db, origin, _task, _file = ledger_db
    if kind == "base":
        agent, requests = _base(monkeypatch, [(200, _body())])
        def invoke():
            return agent.call("unit")
    else:
        agent = DeepSeekAgent(api_key="unit", model="unit", max_retries=2)
        requests = []

        def request(*_args):
            requests.append(True)
            return httpx.Response(200, json=_body()), 2

        monkeypatch.setattr(agent, "_do_request", request)
        invoke = (
            (lambda: agent.call_raw("unit", "unit"))
            if kind == "raw"
            else (lambda: agent.chat(system_prompt="unit", user_prompt="unit", db=db, user_id=7))
        )
    original_commit = Session.commit

    def fail_audit(session):
        if any(isinstance(row, AiCallLog) for row in session.new):
            raise OperationalError("INSERT ai_call_log", {}, RuntimeError("audit unavailable"))
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_audit)
    with usage_context(7, origin, db=db), pytest.raises(UsageAccountingError, match="不能重发"):
        invoke()
    assert len(requests) == 1
    assert db.query(AiCallLog).count() == 0


@pytest.mark.parametrize("kind", ["raw", "chat"])
def test_deepseek_failed_response_preserves_reported_usage_after_rollback(ledger_db, monkeypatch, kind):
    db, origin, task_id, file_id = ledger_db
    agent = DeepSeekAgent(api_key="unit", model="unit", max_retries=0)
    monkeypatch.setattr(agent, "_do_request", lambda *_a: (httpx.Response(400, json={"usage": {"total_tokens": 6}}), 2))
    with (
        usage_context(7, {**origin, "_review_task_id": task_id, "_file_id": file_id, "_chunk_index": 3}, db=db),
        pytest.raises(Exception),
    ):
        if kind == "raw":
            agent.call_raw("unit", "unit")
        else:
            agent.chat(system_prompt="unit", user_prompt="unit", db=db, user_id=7)
    db.rollback()
    row = db.query(AiCallLog).one()
    assert (row.total_tokens, row.status, row.task_id, row.file_id, row.chunk_index) == (
        6,
        "failed",
        task_id,
        file_id,
        3,
    )
    assert model_attribution(row) == origin


def test_raw_network_retry_and_success_deferred_ids_do_not_duplicate(ledger_db, monkeypatch):
    db, origin, task_id, file_id = ledger_db
    agent = DeepSeekAgent(api_key="unit", model="unit", max_retries=1)
    script = [httpx.ReadTimeout("upstream timeout"), httpx.Response(200, json=_body())]

    def request(*_args):
        action = script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action, 2

    monkeypatch.setattr(agent, "_do_request", request)
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _s: None)
    with usage_context(7, origin, db=db):
        _, meta = agent.call_raw("unit", "unit")
    agent.log_deferred(db, user_id=7, task_id=task_id, file_id=file_id, chunk_index=0, meta=meta)
    db.rollback()
    rows = db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert [row.id for row in rows] == meta["_usage_log_ids"]
    assert [(row.status, row.total_tokens) for row in rows] == [("retry", None), ("success", 9)]
    assert all(row.task_id == task_id for row in rows)


def test_chat_assistant_history_request_records_one_actual_call(ledger_db, monkeypatch):
    from app.agents import chat_agent
    from app.agents.chat_agent import ChatAssistantAgent

    db, origin, task_id, _file = ledger_db
    configured, requests = _base(monkeypatch, [(200, _body())])
    monkeypatch.setattr(
        chat_agent,
        "pin_public_http_url",
        lambda url: SimpleNamespace(request_url=url, host_header="unit.example", request_extensions={}),
    )
    agent = ChatAssistantAgent()
    agent._base_url, agent._api_key = configured._base_url, configured._api_key
    agent._max_retries = 0
    agent._orchestrator = SimpleNamespace(list_agents=lambda: {})
    db.get(ReviewTask, task_id).task_name = "uncommitted business change"
    with usage_context(7, origin, db=db):
        result = agent._handle_chat([{"role": "user", "content": "original question"}], AgentContext(user_id=7))
    db.rollback()
    assert db.get(ReviewTask, task_id).task_name == "ledger-review"
    row = db.query(AiCallLog).one()
    assert len(requests) == 1 and result.usage_log_ids == [row.id]
    assert model_attribution(row) == origin and row.total_tokens == 9
    assert "original question" in row.prompt


def test_explicit_empty_execution_scope_does_not_borrow_target_task_root(ledger_db):
    from app.services.ai_usage_context import record_usage_attempt

    db, _origin, task_id, _file = ledger_db
    with usage_context(7, {}, db=db):
        log_id = record_usage_attempt(model_name="unit", agent_label="inspection", task_id=task_id, status="success")
    row = db.get(AiCallLog, log_id)
    assert row.task_id == task_id and model_attribution(row) == {}


@pytest.mark.parametrize("rejection", ["pin", "budget"])
def test_base_preflight_rejection_cannot_be_backfilled_as_model_call(ledger_db, monkeypatch, rejection):
    import time

    db, origin, _task_id, _file_id = ledger_db
    agent, requests = _base(monkeypatch, [])
    if rejection == "pin":

        def reject(_url):
            raise ValueError("private address denied")

        monkeypatch.setattr("app.agents.base.pin_public_http_url", reject)
    with usage_context(7, origin, db=db):
        result = agent.call("unit", deadline_monotonic=time.monotonic() - 1 if rejection == "budget" else None)
    agent._log_call(db, user_id=7, result=result, status="failed", error=result.error)
    assert not result.success and result.http_attempts == 0
    assert requests == [] and db.query(AiCallLog).count() == 0
    DeepSeekAgent.log_deferred(db, user_id=7, meta={"_http_attempts": 0, "_usage_log_ids": []}, status="failed")
    assert db.query(AiCallLog).count() == 0


def test_actual_unbound_base_http_can_still_be_deferred_once(ledger_db, monkeypatch):
    db, _origin, _task_id, _file_id = ledger_db
    agent, requests = _base(monkeypatch, [(200, _body())])
    result = agent.call("unit", AgentContext(user_id=7))
    assert result.http_attempts == 1 and result.usage_log_ids == []
    agent._log_call(db, user_id=7, result=result)
    db.commit()
    assert len(requests) == 1 and db.query(AiCallLog).one().total_tokens == 9


def test_real_request_orchestrator_binds_plain_language_and_folder_models(ledger_db, monkeypatch):
    from app.agents import orchestrator as module
    from app.models.user import User
    from app.utils.api_resolver import ApiConfig

    db, _origin, _task_id, _file_id = ledger_db
    _, requests = _base(
        monkeypatch,
        [
            (200, _body(content='{"language":"python"}')),
            (200, _body(content='{"language":"python","project_name":"示例"}')),
        ],
    )
    user = User(id=7, username="real-binding", password="unit-hash", role="user", status=1)
    db.add(user)
    db.commit()
    monkeypatch.setattr(
        module,
        "resolve_api_config",
        lambda *_a: ApiConfig(api_key="unit", base_url="https://unit.example/v1", model="unit", max_retries=0),
    )
    orch = module.get_request_orchestrator(db, user=user)
    assert current_attribution(7) == {}
    language = orch.detect_language("普通项目", "语言识别")
    folder = orch.analyze_project("示例目录", ["main.py"])
    assert language.success and folder.success and len(requests) == 2
    db.rollback()
    rows = db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert [row.agent_label for row in rows] == ["language_detector", "project_analyzer"]
    assert all(row.user_id == 7 and row.total_tokens == 9 and model_attribution(row) == {} for row in rows)
    assert [row.id for row in rows] == [*language.usage_log_ids, *folder.usage_log_ids]
