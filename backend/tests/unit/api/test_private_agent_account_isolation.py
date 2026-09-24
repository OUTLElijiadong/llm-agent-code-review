"""私人 Agent 对话必须按账号隔离，管理员业务权限不授予他人的聊天内容。"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents.clarify_store import ClarifyStore
from app.agents.discussion_bus import DiscussionBus
from app.agents.events import AgentEvent, AgentEventType, DiscussionTurn
from app.api.v1 import agent_responses, agents, ai_logs, ws_discussion
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.agent_mesh import AgentMeshConversation
from app.models.agent_multimodal import AgentMultimodalAsset
from app.models.agent_response_run import AgentResponseRun
from app.models.agent_team import AgentTeam
from app.models.ai_call_log import AiCallLog
from app.services import agent_mesh_service, agent_responses_service, agent_team_service
from app.services.deepseek_responses_runtime import ToolCall, ToolExecutionResult


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
async def test_private_sse_only_delivers_owned_events(monkeypatch, role):
    events = [
        AgentEvent(AgentEventType.THINKING, "chat_assistant", "private", message=message, user_id=owner)
        for message, owner in (("foreign-private", 8), ("unowned-private", None), ("own-private", 7))
    ]

    class Bus:
        async def subscribe(self, replay=0):
            for event in events:
                yield event

    monkeypatch.setattr(agents, "_resolve_sse_user", lambda *_args: SimpleNamespace(id=7, role=role))
    monkeypatch.setattr(agents.rbac_service, "is_admin_user", lambda *_args: role != "user")
    monkeypatch.setattr(agents, "_is_sse_session_active", lambda *_args: True)
    monkeypatch.setattr(agents.AgentEventBus, "instance", lambda: Bus())
    response = await agents.stream_agent_events(replay=3, authorization="Bearer fixture", token=None, db=object())
    body = "".join([chunk async for chunk in response.body_iterator])
    assert "own-private" in body
    assert "foreign-private" not in body
    assert "unowned-private" not in body


@pytest.mark.parametrize("owner_id", [8, None, 0])
@pytest.mark.parametrize("role", ["user", "admin"])
def test_clarification_rejects_other_or_missing_owner_without_consuming(monkeypatch, owner_id, role):
    store = ClarifyStore()
    pending = {"user_id": owner_id, "intent": "dashboard", "payload": {"private": "foreign-context"}}
    store.put("guessable-clarify", pending)
    monkeypatch.setattr(agents.ClarifyStore, "instance", lambda: store)
    monkeypatch.setattr(agents.rbac_service, "is_admin_user", lambda *_args: role == "admin")
    factory = Mock(
        return_value=SimpleNamespace(
            chat_agent=SimpleNamespace(
                dispatch_with_payload=Mock(
                    return_value=SimpleNamespace(success=True, data="foreign-context", model="fixture")
                )
            )
        )
    )
    monkeypatch.setattr(agents, "get_request_orchestrator", factory)
    with pytest.raises(ForbiddenError):
        agents.submit_clarification(
            agents.ClarifyAnswers(clarify_id="guessable-clarify"), db=object(), user=SimpleNamespace(id=7, role=role)
        )
    factory.assert_not_called()
    assert store.peek("guessable-clarify") is pending


@pytest.mark.parametrize("role", ["user", "admin"])
@pytest.mark.parametrize("owner_id", [8, 0, None])
def test_websocket_session_has_no_admin_or_missing_owner_bypass(monkeypatch, role, owner_id):
    monkeypatch.setattr("app.services.rbac_service.is_admin_user", lambda *_args: role == "admin")
    assert ws_discussion._can_access_session(SimpleNamespace(id=7, role=role), owner_id, object()) is False


def test_websocket_session_owner_retains_access():
    assert ws_discussion._can_access_session(SimpleNamespace(id=7, role="user"), 7, object()) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id", [8, 0, None])
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_roundtable_tool_cannot_read_or_control_another_accounts_chat(monkeypatch, owner_id, role):
    bus = DiscussionBus()
    session = bus.create_session("guessable-roundtable", 1, "private.py", owner_user_id=owner_id)
    session.turns.append(DiscussionTurn(1, "reviewer", "reviewer", "agent", "foreign-private"))
    controller = Mock(return_value=True)
    monkeypatch.setattr(bus, "control_session", controller)
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    monkeypatch.setattr(agent_responses_service, "_is_admin_actor", lambda *_args: role == "admin")
    executor = object.__new__(agent_responses_service.PrismToolExecutor)
    executor._user = SimpleNamespace(id=7, role=role)
    executor._db = object()
    executor._surface = "user"
    result = executor._get_roundtable_discussion(
        ToolCall("get", "get_roundtable_discussion", {"session_id": session.session_id}, "{}")
    )
    assert result.status == "error"
    assert "foreign-private" not in str(result.output)
    result = await executor._control_roundtable_discussion(
        ToolCall(
            "stop",
            "control_roundtable_discussion",
            {
                "session_id": session.session_id,
                "action": "stop",
            },
            "{}",
        )
    )
    assert result.status == "error"
    controller.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "admin"])
async def test_roundtable_owner_can_read_and_control_own_chat(monkeypatch, role):
    bus = DiscussionBus()
    session = bus.create_session("owned-roundtable", 1, "own.py", owner_user_id=7)
    controller = Mock(return_value=True)
    monkeypatch.setattr(bus, "control_session", controller)
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    executor = object.__new__(agent_responses_service.PrismToolExecutor)
    executor._user = SimpleNamespace(id=7, role=role)
    executor._db = object()
    executor._surface = "user"
    assert (
        executor._get_roundtable_discussion(
            ToolCall(
                "get",
                "get_roundtable_discussion",
                {
                    "session_id": session.session_id,
                },
                "{}",
            )
        ).status
        == "success"
    )
    result = await executor._control_roundtable_discussion(
        ToolCall(
            "pause",
            "control_roundtable_discussion",
            {
                "session_id": session.session_id,
                "action": "pause",
            },
            "{}",
        )
    )
    assert result.status == "success"
    controller.assert_called_once_with(session.session_id, "pause")


@pytest.mark.asyncio
async def test_roundtable_tool_rejects_unhandled_input_without_ghost_turn(monkeypatch):
    bus = DiscussionBus()
    session = bus.create_session("unhandled-roundtable", 1, "own.py", owner_user_id=7)
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    executor = object.__new__(agent_responses_service.PrismToolExecutor)
    executor._user = SimpleNamespace(id=7, role="user")
    executor._db = object()
    executor._surface = "user"

    result = await executor._control_roundtable_discussion(
        ToolCall(
            "send", "control_roundtable_discussion",
            {"session_id": session.session_id, "action": "user_input", "content": "重新核对权限边界"}, "{}",
        )
    )

    assert result.status == "error"
    assert session.turns == []


def test_roundtable_tool_pages_complete_ordered_history(monkeypatch):
    bus = DiscussionBus()
    session = bus.create_session("long-roundtable", 1, "own.py", owner_user_id=7)
    for seq in range(1, 136):
        turn = DiscussionTurn(seq, "reviewer", "审查员", "agent", f"第 {seq} 条")
        bus.publish_turn(session.session_id, turn)
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    executor = object.__new__(agent_responses_service.PrismToolExecutor)
    executor._user = SimpleNamespace(id=7, role="user")
    executor._db = object()
    executor._surface = "user"

    pages = []
    cursor = None
    while True:
        arguments = {"session_id": session.session_id}
        if cursor is not None:
            arguments["before_seq"] = cursor
        result = executor._get_roundtable_discussion(
            ToolCall(f"get-{len(pages)}", "get_roundtable_discussion", arguments, "{}")
        )
        assert result.status == "success"
        assert len(json.dumps({"status": "success", "output": result.output}, ensure_ascii=False).encode()) <= 8000
        pages.append(result.output)
        if not result.output["has_more"]:
            break
        cursor = result.output["next_before_seq"]
        assert cursor is not None
        assert len(pages) < 30

    assert len(pages) > 1
    assert pages[0]["turn_count"] == 135
    assert [turn["content"] for page in reversed(pages) for turn in page["turns"]] == [
        f"第 {seq} 条" for seq in range(1, 136)
    ]


def test_roundtable_tool_chunks_long_chinese_turn_without_losing_content(monkeypatch):
    bus = DiscussionBus()
    session = bus.create_session("chunked-roundtable", 1, "own.py", owner_user_id=7)
    content = "中英文混排🙂\n" * 2500
    bus.publish_turn(session.session_id, DiscussionTurn(1, "reviewer", "审查员", "agent", content))
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    executor = object.__new__(agent_responses_service.PrismToolExecutor)
    executor._user = SimpleNamespace(id=7, role="user")
    executor._db = object()
    executor._surface = "user"

    chunks = []
    arguments = {"session_id": session.session_id}
    while True:
        result = executor._get_roundtable_discussion(
            ToolCall(f"chunk-{len(chunks)}", "get_roundtable_discussion", arguments, "{}")
        )
        assert result.status == "success"
        output = result.output
        assert len(json.dumps({"status": "success", "output": output}, ensure_ascii=False,
                              separators=(",", ":")).encode()) <= 8000
        assert len(output["turns"]) == 1
        turn = output["turns"][0]
        assert turn["seq"] == 1
        assert turn["content_offset"] == len("".join(chunks))
        assert turn["content_total_chars"] == len(content)
        chunks.append(turn["content"])
        if output["next_chunk_seq"] is None:
            assert turn["content_complete"] is True
            assert output["has_more"] is False
            break
        assert turn["content_complete"] is False
        arguments = {
            "session_id": session.session_id,
            "chunk_seq": output["next_chunk_seq"],
            "chunk_offset": output["next_chunk_offset"],
        }
        assert len(chunks) < 30
    assert "".join(chunks) == content


def test_roundtable_tool_resumes_older_history_after_large_middle_turn(monkeypatch):
    bus = DiscussionBus()
    session = bus.create_session("mixed-roundtable", 1, "own.py", owner_user_id=7)
    expected = ["较早发言", "长" * 12000, "最新发言"]
    for index, content in enumerate(expected, start=1):
        bus.publish_turn(session.session_id, DiscussionTurn(
            index, "reviewer", "审查员", "agent", content,
        ))
    monkeypatch.setattr(DiscussionBus, "instance", lambda: bus)
    executor = object.__new__(agent_responses_service.PrismToolExecutor)
    executor._user = SimpleNamespace(id=7, role="user")
    executor._db = object()
    executor._surface = "user"

    newest = executor._get_roundtable_discussion(ToolCall(
        "mixed-1", "get_roundtable_discussion", {"session_id": session.session_id}, "{}",
    ))
    assert [turn["seq"] for turn in newest.output["turns"]] == [3]
    assert newest.output["next_before_seq"] == 3

    partial = executor._get_roundtable_discussion(ToolCall(
        "mixed-2", "get_roundtable_discussion",
        {"session_id": session.session_id, "before_seq": 3}, "{}",
    ))
    pieces = []
    while True:
        assert partial.status == "success"
        assert len(json.dumps({"status": "success", "output": partial.output},
                              ensure_ascii=False, separators=(",", ":")).encode()) <= 8000
        pieces.append(partial.output["turns"][0]["content"])
        if partial.output["next_chunk_seq"] is None:
            break
        partial = executor._get_roundtable_discussion(ToolCall(
            "mixed-next", "get_roundtable_discussion",
            {
                "session_id": session.session_id,
                "chunk_seq": partial.output["next_chunk_seq"],
                "chunk_offset": partial.output["next_chunk_offset"],
            }, "{}",
        ))
        assert len(pieces) < 12

    assert "".join(pieces) == expected[1]
    assert partial.output["has_more"] is True
    assert partial.output["next_before_seq"] == 2
    oldest = executor._get_roundtable_discussion(ToolCall(
        "mixed-3", "get_roundtable_discussion",
        {"session_id": session.session_id, "before_seq": 2}, "{}",
    ))
    assert [turn["content"] for turn in oldest.output["turns"]] == [expected[0]]
    assert oldest.output["has_more"] is False

    invalid = executor._get_roundtable_discussion(ToolCall(
        "mixed-invalid", "get_roundtable_discussion",
        {"session_id": session.session_id, "chunk_seq": 2, "chunk_offset": len(expected[1]) + 1}, "{}",
    ))
    assert invalid.status == "error"


@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
def test_team_detail_and_list_never_expose_other_accounts_chat(db, role):
    team = AgentTeam(
        user_id=8,
        surface="user",
        session_key="same-session",
        title="foreign-private",
        objective="foreign-private",
        trace_id="foreign-private",
    )
    db.add(team)
    db.commit()
    actor = SimpleNamespace(id=7, role=role)
    with pytest.raises(agent_team_service.AgentTeamNotFoundError):
        agent_team_service.get_team(db, actor, team.id)
    assert agent_team_service.list_teams(db, actor)["items"] == []


def _asset(db, *, owner=7, surface="user"):
    row = AgentMultimodalAsset(
        user_id=owner,
        surface=surface,
        run_id="known-run",
        role="input",
        mime="image/png",
        sha256="a" * 64,
        data=b"private-image",
    )
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("role", ["user", "admin"])
def test_image_assets_are_owner_only_even_for_admin(db, role):
    row = _asset(db, owner=8)
    actor = SimpleNamespace(id=7, role=role)
    assert agent_responses.list_run_assets("known-run", db, actor).data == []
    with pytest.raises(NotFoundError):
        agent_responses.get_asset_image(row.id, db, actor)


def test_admin_image_is_not_readable_after_account_loses_admin(db, monkeypatch):
    row = _asset(db, surface="admin")
    monkeypatch.setattr(agent_responses, "_is_admin_actor", lambda *_args: False)
    actor = SimpleNamespace(id=7, role="user")
    assert agent_responses.list_run_assets("known-run", db, actor).data == []
    with pytest.raises(NotFoundError):
        agent_responses.get_asset_image(row.id, db, actor)


def test_private_image_must_not_be_reused_from_browser_cache_after_account_switch(db):
    row = _asset(db)
    response = agent_responses.get_asset_image(row.id, db, SimpleNamespace(id=7, role="user"))
    assert response.body == b"private-image"
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("endpoint", ["/runs/known-run/assets", "/assets/{asset_id}/image"])
def test_image_api_requires_current_chat_permission(db, monkeypatch, endpoint):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from app.core.dependencies import get_current_user
    from app.core.exceptions import PermissionError

    row = _asset(db)
    application = FastAPI()
    application.include_router(agent_responses.router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=7, role="user")
    monkeypatch.setattr("app.core.rbac_dependency.check_permission", lambda *_args: False)

    @application.exception_handler(PermissionError)
    async def denied(_request, _exc):
        return JSONResponse({"detail": "permission denied"}, status_code=403)

    response = TestClient(application).get(endpoint.format(asset_id=row.id))
    assert response.status_code == 403
    assert "private-image" not in response.text


@pytest.mark.parametrize("role", ["user", "admin"])
def test_identical_session_keys_do_not_join_other_accounts_history(db, role):
    for owner, text in ((7, "own-private"), (8, "foreign-private")):
        db.add(
            AgentResponseRun(
                run_id=f"run-{owner}",
                user_id=owner,
                surface="user",
                session_key="same-session",
                status="completed",
                checkpoint_json=json.dumps({"transcript": [{"role": "user", "content": text}], "output_text": text}),
            )
        )
    db.commit()
    response = agent_responses.get_agent_response_session("user", "same-session", db, SimpleNamespace(id=7, role=role))
    assert response.data["run"]["run_id"] == "run-7"
    assert "own-private" in json.dumps(response.data)
    assert "foreign-private" not in json.dumps(response.data)


@pytest.mark.asyncio
async def test_audit_progress_is_bound_to_both_account_and_current_run(monkeypatch):
    cases = [
        (8, "current-run", "foreign-private"),
        (7, "other-run", "other-chat-private"),
        (None, "current-run", "unowned-private"),
        (7, "current-run", "own-progress"),
    ]
    received = []
    relayed = asyncio.Event()

    class Bus:
        async def subscribe(self):
            for owner, trace_id, message in cases:
                yield AgentEvent(
                    AgentEventType.PROGRESS,
                    "security_sentinel",
                    trace_id,
                    message=message,
                    payload={"phase": "analysis"},
                    user_id=owner,
                )
            relayed.set()

    async def execute_once(_call, _operation):
        await asyncio.wait_for(relayed.wait(), timeout=1)
        return ToolExecutionResult.success({"done": True})

    monkeypatch.setattr(agents.AgentEventBus, "instance", lambda: Bus())
    executor = object.__new__(agent_responses_service.PrismToolExecutor)
    executor._user = SimpleNamespace(id=7)
    executor._run_id = "current-run"
    executor._event_sink = received.append
    executor._execute_once = execute_once
    result = await executor._execute_audit_with_progress(ToolCall("audit-call", "scan_project", {}, "{}"))
    assert result.status == "success"
    assert [e["message"] for e in received] == ["own-progress"]


@pytest.mark.parametrize("role", ["user", "admin"])
def test_session_directory_respects_current_admin_surface_access(db, role):
    for surface in ("user", "admin"):
        db.add(
            AgentMeshConversation(
                user_id=7,
                surface=surface,
                session_key=f"session-{surface}",
                title=f"{surface}-private",
                status="active",
                last_seen_at=datetime.now(timezone.utc),
            )
        )
    db.commit()
    actor = SimpleNamespace(id=7, role=role)
    result = agent_mesh_service.list_conversations(db, actor)
    expected = {"user", "admin"} if role == "admin" else {"user"}
    assert {r["surface"] for r in result["items"]} == expected


@pytest.mark.parametrize(
    "attribution",
    [
        {"agent_label": "chat_assistant"},
        {"agent_label": "manager"},
        {"agent_label": "code_reviewer", "root_agent_run_id": 42, "task_id": 50},
        {"agent_label": "security_sentinel", "agent_run_id": 42, "task_id": 50},
        {"agent_label": "custom_reviewer", "tool_execution_id": 42, "task_id": 50},
        {"agent_label": "custom_reviewer", "agent_team_id": 42, "task_id": 50},
        {"agent_label": "unknown_legacy", "task_id": None},
    ],
)
def test_admin_log_detail_does_not_bypass_private_chat_isolation(db, attribution):
    row = AiCallLog(
        user_id=8,
        model_name="fixture-model",
        prompt="foreign-private-prompt",
        response="foreign-private-response",
        error_message="foreign-private-error",
        **attribution,
    )
    db.add(row)
    db.commit()
    detail = ai_logs.get_log(row.id, db, SimpleNamespace(id=7, role="admin")).data.model_dump()
    assert "foreign-private" not in json.dumps(detail, default=str)
    assert detail["id"] == row.id
    assert detail["user_id"] == 8
    assert detail["model_name"] == "fixture-model"
    assert detail["content_redacted"] is True


def test_owner_admin_retains_own_private_log_detail(db):
    row = AiCallLog(user_id=7, model_name="fixture-model", agent_label="chat_assistant", prompt="own-private")
    db.add(row)
    db.commit()
    detail = ai_logs.get_log(row.id, db, SimpleNamespace(id=7, role="admin")).data
    assert detail.prompt == "own-private"


def test_admin_business_review_log_access_remains_authorized(db):
    row = AiCallLog(
        user_id=8,
        task_id=50,
        model_name="fixture-model",
        agent_label="code_reviewer",
        prompt="authorized-business-review",
    )
    db.add(row)
    db.commit()
    detail = ai_logs.get_log(row.id, db, SimpleNamespace(id=7, role="admin")).data
    assert detail.prompt == "authorized-business-review"
