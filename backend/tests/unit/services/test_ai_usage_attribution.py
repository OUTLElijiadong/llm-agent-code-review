"""精确根任务外键、执行尝试与未知用量回归。"""

import httpx

from app.ai.deepseek_agent import DeepSeekAgent
from app.models.ai_call_log import AiCallLog


def test_logs_expose_foreign_keys_for_each_execution_layer():
    expected = {
        "root_agent_run_id": "agent_response_run.id",
        "agent_run_id": "agent_response_run.id",
        "tool_execution_id": "agent_tool_execution.id",
        "agent_team_id": "agent_team.id",
        "agent_team_task_id": "agent_team_task.id",
        "agent_execution_event_id": "agent_team_event.id",
    }
    for name, target in expected.items():
        column = AiCallLog.__table__.columns.get(name)
        assert column is not None, name
        assert {key.target_fullname for key in column.foreign_keys} == {target}
        assert column.nullable


def test_raw_missing_usage_stays_unknown(monkeypatch):
    agent = DeepSeekAgent(api_key="unit-only", model="unit-model", max_retries=0)
    response = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})
    monkeypatch.setattr(agent, "_do_request", lambda *_args: (response, 3))
    _, meta = agent.call_raw(system_prompt="unit", user_prompt="unit")
    assert (meta["prompt_tokens"], meta["completion_tokens"], meta["total_tokens"]) == (None, None, None)


def _run(db, user_id=7, key="root-run", **fields):
    from app.models.agent_response_run import AgentResponseRun

    row = AgentResponseRun(
        user_id=user_id, run_id=key, surface="user", session_key=key, status="completed", checkpoint_json="{}", **fields
    )
    db.add(row)
    db.commit()
    return row


def _team_payload():
    from app.schemas.agent_team import AgentTeamCreateIn

    return AgentTeamCreateIn.model_validate(
        {
            "surface": "user",
            "session_id": "usage-session",
            "title": "用量归因",
            "objective": "读取并复核",
            "members": [
                {"member_key": "reader", "display_name": "读取", "address": "agent:project_analyzer"},
                {
                    "member_key": "verifier",
                    "display_name": "复核",
                    "address": "agent:code_reviewer",
                    "role": "verifier",
                },
            ],
            "tasks": [
                {"task_key": "read", "member_key": "reader", "title": "读取", "instructions": "读取"},
                {
                    "task_key": "verify",
                    "member_key": "verifier",
                    "title": "复核",
                    "instructions": "复核",
                    "depends_on": ["read"],
                },
            ],
        }
    )


def test_delayed_raw_log_uses_original_scope_and_does_not_leak_accounts(db, monkeypatch):
    from app.services.ai_usage_context import current_attribution, usage_context

    root_a, root_b = _run(db), _run(db, key="other-run")
    agent = DeepSeekAgent(api_key="unit-only", model="unit-model", max_retries=0)
    response = httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3},
        },
    )
    monkeypatch.setattr(agent, "_do_request", lambda *_args: (response, 3))
    with usage_context(7, {"root_agent_run_id": root_a.id, "agent_run_id": root_a.id}):
        _, meta = agent.call_raw(system_prompt="unit", user_prompt="unit")
        assert current_attribution(8) == {}
    with usage_context(7, {"root_agent_run_id": root_b.id}):
        agent.log_deferred(db, user_id=7, meta=meta)
        agent.log_deferred(db, user_id=8, meta=meta)
    rows = db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert rows[0].root_agent_run_id == root_a.id
    assert rows[0].prompt_tokens == 0
    assert rows[1].root_agent_run_id is None
    assert current_attribution(7) == {}


def test_parallel_contexts_are_isolated_and_empty_snapshot_stays_empty(db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from app.services.ai_usage_context import attribution_snapshot, current_attribution, usage_context

    with ThreadPoolExecutor(max_workers=2) as pool:
        with usage_context(7, {"root_agent_run_id": 11}):
            a = pool.submit(copy_context().run, attribution_snapshot)
        with usage_context(8, {"root_agent_run_id": 22}):
            b = pool.submit(copy_context().run, attribution_snapshot)
    assert a.result() == {"owner_user_id": 7, "root_agent_run_id": 11}
    assert b.result() == {"owner_user_id": 8, "root_agent_run_id": 22}
    with usage_context(7, {"root_agent_run_id": _run(db).id}):
        DeepSeekAgent.log_deferred(db, user_id=7, meta={"model_tag": "unit", "_usage_attribution": {}})
    assert db.query(AiCallLog).one().root_agent_run_id is None
    assert current_attribution(7) == {}


async def test_response_rounds_link_root_and_preserve_zero_invalid_unknown(db, monkeypatch):
    from types import SimpleNamespace

    from app.services import agent_responses_service as service

    captured = {}
    root = _run(db)
    child = _run(db, key="child-run", root_agent_run_id=root.id)
    monkeypatch.setattr(service, "resolve_api_config", lambda *_args: SimpleNamespace(model="unit", source="system"))
    monkeypatch.setattr(service, "NativeResponsesTransport", lambda *_args: object())
    monkeypatch.setattr(service, "get_request_orchestrator", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(service, "DeepSeekResponsesRuntime", lambda **kwargs: captured.update(kwargs))
    runner = service.AgentResponsesService(
        db, SimpleNamespace(id=7, username="unit", role="user"), surface="user", session_key="child-run"
    )
    await runner._runtime("child-run", None)
    captured["on_round"](
        {
            "status": "completed",
            "usage": {"input_tokens": 0, "prompt_tokens": 999, "output_tokens": 4, "total_tokens": 4},
        }
    )
    captured["on_round"](
        {"status": "failed", "usage": {"input_tokens": True, "output_tokens": -2, "total_tokens": "8"}}
    )
    captured["on_round"]({"status": "completed"})
    logs = db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert [(row.root_agent_run_id, row.agent_run_id) for row in logs] == [(root.id, child.id)] * 3
    assert logs[0].prompt_tokens == 0
    assert (logs[1].prompt_tokens, logs[1].completion_tokens, logs[1].total_tokens) == (None, None, None)
    assert logs[2].total_tokens is None


async def test_tool_team_claim_dispatch_and_result_keep_exact_origin(db, monkeypatch):
    from app.models.agent_mesh import AgentMeshMessage
    from app.models.agent_response_run import AgentToolExecution
    from app.models.agent_team import AgentTeam, AgentTeamEvent
    from app.models.user import User
    from app.services import agent_mesh_dispatcher, agent_responses_service, agent_team_dispatcher, agent_team_service
    from app.services.ai_usage_context import current_attribution
    from app.services.deepseek_responses_runtime import ToolCall

    root = _run(db)
    user = User(id=7, username="usage-owner", password="x", role="user", status=1)
    db.add(user)
    db.commit()
    monkeypatch.setattr(agent_responses_service, "get_request_orchestrator", lambda *_args, **_kwargs: object())
    executor = agent_responses_service.PrismToolExecutor(
        db, user, surface="user", run_id=root.run_id, session_key="usage-session", mcp_provider=None
    )
    monkeypatch.setattr(executor, "_assert_session_active", lambda: None)
    created = await executor._execute_once(
        ToolCall(call_id="usage-call", name="create_agent_team", arguments={}, raw_arguments="{}"),
        lambda: agent_team_service.create_team(db, user, _team_payload()),
    )
    assert created.status == "success"
    team = db.query(AgentTeam).one()
    tool = db.query(AgentToolExecution).filter_by(call_id="usage-call").one()
    assert (team.root_agent_run_id, team.agent_run_id, team.tool_execution_id) == (root.id, root.id, tool.id)
    claimed = agent_team_service.claim_next_task(db, team.id)
    event = db.get(AgentTeamEvent, claimed["execution_event_id"])
    assert event.event_type == "task.claimed"
    assert event.task_id == claimed["task_id"]
    expected = {
        "root_agent_run_id": root.id,
        "agent_run_id": root.id,
        "tool_execution_id": tool.id,
        "agent_team_id": team.id,
        "agent_team_task_id": claimed["task_id"],
        "agent_execution_event_id": event.id,
    }

    def handle(session, actor, *_args, **_kwargs):
        assert current_attribution(actor.id) == expected
        DeepSeekAgent.log_deferred(session, user_id=actor.id, meta={"model_tag": "unit", "total_tokens": 13})
        return "unit", {"status": "completed", "summary": "已读取"}

    monkeypatch.setattr(agent_mesh_dispatcher, "_handle", handle)
    monkeypatch.setattr(agent_team_dispatcher, "SessionLocal", lambda: db)
    assert agent_team_dispatcher._execute_claimed(team.id, claimed) == {"success": True}
    log = db.query(AiCallLog).one()
    assert {key: getattr(log, key) for key in expected} == expected
    result = (
        db.query(AgentMeshMessage)
        .filter_by(causation_id=claimed["request_message_id"], message_type="task.result")
        .first()
    )
    assert result is not None
    assert result.root_agent_run_id == root.id
    assert result.agent_execution_event_id == event.id
    assert current_attribution(7) == {}


async def test_nested_run_persists_root_across_checkpoint_reconstruction(db):
    from app.services.agent_responses_service import DatabaseCheckpointStore
    from app.services.ai_usage_context import run_attribution, usage_context
    from app.services.deepseek_responses_runtime import RunCheckpoint

    root = _run(db)
    with usage_context(7, {"root_agent_run_id": root.id, "agent_run_id": root.id}):
        store = DatabaseCheckpointStore(db, user_id=7, surface="user", session_key="child-session")
        assert await store.create(RunCheckpoint(run_id="persist-child", model="unit", transcript=[], tools=[]))
    db.expire_all()
    fields = run_attribution(db, 7, "persist-child")
    assert fields["root_agent_run_id"] == root.id
    assert fields["agent_run_id"] != root.id
    assert run_attribution(db, 8, "persist-child") == {}


def test_background_review_source_and_api_filter_use_persisted_relations(db):
    from app.models.review_task import ReviewTask
    from app.schemas.ai_log import AiLogOut
    from app.services.ai_log_service import list_logs
    from app.services.ai_usage_context import log_attribution, usage_context

    root, other = _run(db), _run(db, key="other-root")
    task = ReviewTask(user_id=7, project_id=1, root_agent_run_id=root.id, agent_run_id=root.id)
    db.add(task)
    db.commit()
    with usage_context(7, {"root_agent_run_id": other.id}):
        assert log_attribution(db, 7, task.id)["root_agent_run_id"] == other.id
        DeepSeekAgent.log_deferred(db, user_id=7, task_id=task.id, meta={"model_tag": "unit"})
    assert log_attribution(db, 7, task.id)["root_agent_run_id"] == root.id
    page = list_logs(db, root_agent_run_id=other.id)
    assert page["total"] == 1
    row = AiLogOut.model_validate(page["items"][0])
    assert row.root_agent_run_key == "other-root"
    assert row.usage_state == "unknown"
    assert row.total_tokens is None
    assert list_logs(db, root_agent_run_id=root.id)["total"] == 0


async def test_checkpoint_delete_keeps_usage_and_existing_tool_ledger(db):
    from sqlalchemy import text

    from app.models.agent_response_run import AgentToolExecution
    from app.services.agent_responses_service import DatabaseCheckpointStore

    db.execute(text("PRAGMA foreign_keys=ON"))
    db.commit()
    root = _run(db)
    root_key = root.run_id
    log = AiCallLog(user_id=7, model_name="unit", root_agent_run_id=root.id, agent_run_id=root.id, total_tokens=12)
    tool = AgentToolExecution(
        request_id="delete-source",
        run_id=root_key,
        call_id="old-call",
        user_id=7,
        tool_name="read",
        status="executing",
        arguments_json="{}",
    )
    db.add_all([log, tool])
    db.commit()
    await DatabaseCheckpointStore(db, user_id=7, surface="user", session_key=root_key).delete(root_key)
    db.expire_all()
    persisted = db.query(AiCallLog).one()
    assert persisted.root_agent_run_id is None
    assert persisted.agent_run_id is None
    assert persisted.total_tokens == 12
    retained = db.query(AgentToolExecution).one()
    assert (retained.run_id, retained.status) == (root_key, "executing")
    assert db.execute(text("PRAGMA foreign_key_check")).all() == []


def test_retry_claims_have_different_immutable_execution_foreign_keys(db, monkeypatch):
    import json
    from datetime import timedelta
    from types import SimpleNamespace

    from app.models.agent_team import AgentTeamEvent, AgentTeamTask
    from app.services import agent_team_service

    user = SimpleNamespace(id=7, role="user", username="owner")
    created = agent_team_service.create_team(db, user, _team_payload())
    first = agent_team_service.claim_next_task(db, created["team_id"])
    agent_team_service.complete_task(
        db,
        created["team_id"],
        first["task_id"],
        lease_token=first["lease_token"],
        result={"status": "failed", "retryable": True},
        success=False,
        error="网络失败",
    )
    next_at = db.get(AgentTeamTask, first["task_id"]).next_attempt_at
    monkeypatch.setattr(agent_team_service, "_now", lambda: next_at + timedelta(seconds=1))
    second = agent_team_service.claim_next_task(db, created["team_id"])
    assert first["execution_event_id"] != second["execution_event_id"]
    events = [db.get(AgentTeamEvent, item["execution_event_id"]) for item in (first, second)]
    assert [json.loads(event.detail_json)["attempt"] for event in events] == [1, 2]
    assert events[0].task_id == events[1].task_id


def test_mesh_reply_restores_persisted_origin_outside_worker_context(db, monkeypatch):
    from types import SimpleNamespace

    from app.services import agent_mesh_service
    from app.services.ai_usage_context import current_attribution

    source = SimpleNamespace(
        user_id=7,
        root_agent_run_id=11,
        agent_run_id=12,
        tool_execution_id=13,
        context_json="{}",
        message_id="message-one",
        trace_id="unrelated-trace",
        sent_from="session:user:usage-session",
        priority="normal",
        subject="读取",
        artifacts_json="[]",
        max_attempts=1,
    )
    captured = {}
    monkeypatch.setattr(agent_mesh_service, "_source_session", lambda _row: ("user", "usage-session"))

    def send(_db, user, **_kwargs):
        captured.update(current_attribution(user.id))
        return {"message_id": "reply-one"}

    monkeypatch.setattr(agent_mesh_service, "send_message", send)
    result = agent_mesh_service._send_dispatch_reply(
        db,
        SimpleNamespace(id=7),
        source,
        target_address="agent:project_analyzer",
        target_name="读取",
        message_type="task.result",
        payload={},
        errors=[],
    )
    assert result["message_id"] == "reply-one"
    assert captured == {"root_agent_run_id": 11, "agent_run_id": 12, "tool_execution_id": 13}
    assert current_attribution(7) == {}
