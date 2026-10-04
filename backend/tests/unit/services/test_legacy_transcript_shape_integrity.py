"""旧检查点恢复不能静默删除损坏消息；只用隔离 SQLite 和协议桩。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api.v1 import agent_responses as api
from app.core.exceptions import ConflictError
from app.models.agent_mesh import AgentMeshConversation
from app.models.agent_response_run import AgentResponseRun, AgentResponseTranscriptMessage
from app.models.user import User
from app.services.agent_responses_service import DatabaseCheckpointStore
from app.services.deepseek_responses_runtime import InvalidRunStateError, RunCheckpoint

SOURCE = [{"role": "user", "content": "原始事实不可删除：账号 A 只读"}]
BAD_CHECKPOINTS = [
    pytest.param({"transcript": SOURCE + ["damaged-source"]}, id="string-message"),
    pytest.param({"transcript": SOURCE + [None]}, id="null-message"),
    pytest.param({"transcript": SOURCE + [[]]}, id="list-message"),
    pytest.param({"transcript": [17] + SOURCE}, id="integer-message"),
    pytest.param([], id="list-checkpoint"),
    pytest.param(None, id="null-checkpoint"),
    pytest.param("wrong-root", id="string-checkpoint"),
    pytest.param({}, id="missing-transcript"),
    pytest.param({"transcript": None}, id="null-transcript"),
]


def _seed(db, value, *, session="legacy-shape-session", status="completed"):
    original = json.dumps(value, ensure_ascii=False)
    row = AgentResponseRun(
        run_id="legacy-shape-run", user_id=7, surface="user", session_key=session,
        status=status, checkpoint_json=original, version=1,
    )
    db.add(row)
    db.add(AgentMeshConversation(
        user_id=7, surface="user", session_key=session, title="已有业务会话", status="active",
        last_seen_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return row, original, DatabaseCheckpointStore(db, user_id=7, surface="user", session_key=session)


def _assert_untouched(db, original):
    db.expire_all()
    assert db.query(AgentResponseRun).one().checkpoint_json == original
    assert db.query(AgentResponseRun).one().version == 1
    assert db.query(AgentResponseTranscriptMessage).count() == 0


@pytest.mark.parametrize("value", BAD_CHECKPOINTS)
def test_private_legacy_merge_rejects_incomplete_shape(value):
    store = DatabaseCheckpointStore(None, user_id=7, surface="user", session_key="shape-only")
    original = json.dumps(value, ensure_ascii=False)
    row = SimpleNamespace(checkpoint_json=original)
    with pytest.raises(InvalidRunStateError):
        store._merge_legacy_transcripts([row])
    assert row.checkpoint_json == original


@pytest.mark.asyncio
@pytest.mark.parametrize("value", BAD_CHECKPOINTS)
async def test_first_append_rejects_bad_old_history_without_writing(db, value):
    _row, original, store = _seed(db, value)
    incoming = RunCheckpoint(
        run_id="next-after-legacy", model="local-stub", tools=[],
        transcript=SOURCE + [{"role": "user", "content": "继续当前账号的任务"}],
    )
    with pytest.raises(InvalidRunStateError):
        await store.create(incoming)
    _assert_untouched(db, original)
    db.rollback()
    _assert_untouched(db, original)


@pytest.mark.parametrize("value", BAD_CHECKPOINTS)
def test_actual_history_route_and_next_chat_reader_reject_bad_legacy(db, value):
    _row, original, _store = _seed(db, value)
    with pytest.raises(ConflictError) as error:
        api.get_agent_response_session_messages(
            surface="user", session_id="legacy-shape-session", before_message=None,
            limit=50, db=db, user=SimpleNamespace(id=7),
        )
    assert (error.value.http_status, error.value.code) == (409, 40931)
    with pytest.raises(ConflictError) as error:
        api._server_history_transcript(db, user_id=7, surface="user", session_id="legacy-shape-session")
    assert error.value.code == 40931
    _assert_untouched(db, original)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_item", [None, "lost-source", [], 17])
async def test_load_and_claim_reject_bad_inline_items_and_rollback_claim(db, bad_item):
    value = {"transcript": SOURCE + [bad_item], "status": "failed"}
    _row, original, store = _seed(db, value, status="failed")
    with pytest.raises(InvalidRunStateError):
        await store.load("legacy-shape-run")
    with pytest.raises(InvalidRunStateError):
        await store.claim("legacy-shape-run", expected_status="failed", claimed_status="running")
    _assert_untouched(db, original)
    assert db.query(AgentResponseRun).one().status == "failed"


@pytest.mark.asyncio
async def test_actual_start_stream_rejects_bad_history_before_model_start(db, monkeypatch):
    _seed(db, {"transcript": SOURCE + [None]})
    user = User(id=7, username="isolated-account", password="not-a-real-password", role="user")
    db.add(user)
    db.commit()
    calls = []

    class LocalService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self, *_args, **_kwargs):
            calls.append("model-start")
            raise AssertionError("损坏历史不能进入模型调用")

    monkeypatch.setattr(api, "AgentResponsesService", LocalService)
    monkeypatch.setattr(api, "resolve_api_config", lambda *_args: None)
    monkeypatch.setattr(api.agent_mesh_service, "heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.agent_mesh_service, "update_title_from_user_message", lambda *_args, **_kwargs: None)
    response = await api.stream_agent_response(api.AgentResponsesRequest(
        surface="user", session_id="legacy-shape-session", action="start", use_server_history=True,
        messages=[{"role": "user", "content": "继续当前任务"}],
    ), db=db, user=user)
    frames = [frame async for frame in response.body_iterator]
    text = "".join(frames)
    assert "event: response.failed" in text
    assert "历史" in text
    assert calls == []
    assert db.query(AgentResponseRun).count() == 1
    assert db.query(AgentResponseTranscriptMessage).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [[], SOURCE, SOURCE + [{"role": "assistant", "content": "确认只读"}]])
async def test_valid_legacy_history_round_trip_and_isolated_api(db, history):
    _row, original, store = _seed(db, {"transcript": history})
    assert api._server_history_transcript(
        db, user_id=7, surface="user", session_id="legacy-shape-session",
    ) == history
    restored = await store.load("legacy-shape-run")
    assert restored.transcript == history
    incoming = history + [{"role": "user", "content": "新一轮事实"}]
    checkpoint = RunCheckpoint(run_id="next-valid-run", model="local-stub", tools=[], transcript=incoming)
    assert await store.create(checkpoint)
    assert (await store.load(checkpoint.run_id)).transcript == incoming
    route = api.get_agent_response_session_messages(
        surface="user", session_id="legacy-shape-session", before_message=None,
        limit=50, db=db, user=SimpleNamespace(id=7),
    )
    assert route.data["messages"] == incoming
    other = api.get_agent_response_session_messages(
        surface="user", session_id="legacy-shape-session", before_message=None,
        limit=50, db=db, user=SimpleNamespace(id=8),
    )
    assert other.data == {"messages": [], "oldest_message_index": 0, "has_more": False, "total": 0}
    assert db.query(AgentResponseRun).filter_by(run_id="legacy-shape-run").one().checkpoint_json == original
    assert db.query(AgentResponseTranscriptMessage).count() == len(incoming)


@pytest.mark.parametrize("history", [[], SOURCE])
def test_valid_ledger_reference_preserves_original_ledger_path(db, history):
    store = DatabaseCheckpointStore(db, user_id=7, surface="user", session_key="reference-only")
    for position, item in enumerate(history):
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        db.add(AgentResponseTranscriptMessage(
            user_id=7, surface="user", session_key="reference-only", position=position,
            message_json=encoded, message_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
        ))
    db.commit()
    value = {"_transcript_ref": {
        "version": 2, "start": 0, "end": len(history), "sha256": store._transcript_digest(history),
    }}
    original = json.dumps(value)
    assert store._merge_legacy_transcripts([SimpleNamespace(checkpoint_json=original)]) == history
    row = SimpleNamespace(user_id=7, surface="user", session_key="reference-only")
    assert api._stored_run_transcript(db, row, value) == history
    assert value == json.loads(original)


@pytest.mark.parametrize("change", ["orphan", "wrong-digest", "invalid-version", "conflicting-inline"])
def test_invalid_ledger_reference_rejected_without_loss(db, change):
    store = DatabaseCheckpointStore(db, user_id=7, surface="user", session_key="bad-reference")
    value = {"_transcript_ref": {
        "version": 2, "start": 0, "end": 0, "sha256": store._transcript_digest([]),
    }}
    if change == "orphan":
        value["_transcript_ref"]["end"] = 1
    elif change == "wrong-digest":
        value["_transcript_ref"]["sha256"] = "0" * 64
    elif change == "invalid-version":
        value["_transcript_ref"]["version"] = 3
    else:
        value["transcript"] = SOURCE
    original = copy.deepcopy(value)
    with pytest.raises(InvalidRunStateError):
        store._merge_legacy_transcripts([SimpleNamespace(checkpoint_json=json.dumps(value))])
    with pytest.raises(ConflictError) as error:
        api._stored_run_transcript(db, SimpleNamespace(user_id=7, surface="user", session_key="bad-reference"), value)
    assert error.value.code == 40931
    assert value == original
    assert db.query(AgentResponseTranscriptMessage).count() == 0


@pytest.mark.asyncio
async def test_empty_ledger_reference_allows_first_append_and_history_route(db):
    value = {"_transcript_ref": {
        "version": 2, "start": 0, "end": 0,
        "sha256": DatabaseCheckpointStore._transcript_digest([]),
    }}
    _row, original, store = _seed(db, value)
    assert api.get_agent_response_session_messages(
        surface="user", session_id="legacy-shape-session", before_message=None,
        limit=50, db=db, user=SimpleNamespace(id=7),
    ).data["total"] == 0
    checkpoint = RunCheckpoint(run_id="next-after-empty-reference", model="local-stub", tools=[], transcript=SOURCE)
    assert await store.create(checkpoint)
    assert (await store.load(checkpoint.run_id)).transcript == SOURCE
    assert api._server_history_transcript(
        db, user_id=7, surface="user", session_id="legacy-shape-session",
    ) == SOURCE
    assert db.query(AgentResponseRun).filter_by(run_id="legacy-shape-run").one().checkpoint_json == original
    assert db.query(AgentResponseTranscriptMessage).count() == len(SOURCE)


@pytest.mark.parametrize("other_user,other_surface", [(8, "user"), (7, "admin")])
def test_nonempty_reference_cannot_read_other_account_or_surface(db, other_user, other_surface):
    value = {"_transcript_ref": {
        "version": 2, "start": 0, "end": 1,
        "sha256": DatabaseCheckpointStore._transcript_digest(SOURCE),
    }}
    encoded = json.dumps(SOURCE[0], ensure_ascii=False, separators=(",", ":"))
    db.add(AgentResponseTranscriptMessage(
        user_id=7, surface="user", session_key="scoped-reference", position=0,
        message_json=encoded, message_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
    ))
    db.commit()
    row = SimpleNamespace(user_id=other_user, surface=other_surface, session_key="scoped-reference")
    store = DatabaseCheckpointStore(db, user_id=other_user, surface=other_surface, session_key="scoped-reference")
    with pytest.raises(InvalidRunStateError):
        store._merge_legacy_transcripts([SimpleNamespace(checkpoint_json=json.dumps(value))])
    with pytest.raises(ConflictError) as error:
        api._stored_run_transcript(db, row, value)
    assert error.value.code == 40931
    assert db.query(AgentResponseTranscriptMessage).one().message_json == encoded


@pytest.mark.asyncio
async def test_later_bad_legacy_run_does_not_partially_initialize_good_history(db):
    first, original, store = _seed(db, {"transcript": SOURCE})
    damaged_original = json.dumps({"transcript": SOURCE + [None]}, ensure_ascii=False)
    db.add(AgentResponseRun(
        run_id="later-damaged-legacy", user_id=7, surface="user", session_key="legacy-shape-session",
        status="completed", checkpoint_json=damaged_original, version=1,
    ))
    db.commit()
    before = [
        (row.run_id, row.checkpoint_json, row.version)
        for row in db.query(AgentResponseRun).order_by(AgentResponseRun.id)
    ]
    with pytest.raises(InvalidRunStateError):
        await store.create(RunCheckpoint(
            run_id="cannot-partially-append", model="local-stub", tools=[], transcript=SOURCE,
        ))
    db.rollback()
    assert [
        (row.run_id, row.checkpoint_json, row.version)
        for row in db.query(AgentResponseRun).order_by(AgentResponseRun.id)
    ] == before
    assert db.query(AgentResponseTranscriptMessage).count() == 0
    assert first.checkpoint_json == original


@pytest.mark.parametrize("encoded", ["", "{broken", "[unclosed"])
def test_malformed_json_remains_unchanged_and_is_rejected(db, encoded):
    row, _original, store = _seed(db, {"transcript": []})
    row.checkpoint_json = encoded
    db.commit()
    with pytest.raises(InvalidRunStateError):
        store._merge_legacy_transcripts([row])
    with pytest.raises(ConflictError) as error:
        api.get_agent_response_session_messages(
            surface="user", session_id="legacy-shape-session", before_message=None,
            limit=50, db=db, user=SimpleNamespace(id=7),
        )
    assert error.value.code == 40931
    _assert_untouched(db, encoded)


@pytest.mark.asyncio
async def test_legacy_tool_evidence_and_asset_reference_are_not_rewritten(db):
    transcript = SOURCE + [
        {"type": "function_call", "name": "read_only_status", "call_id": "known-call", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "known-call", "output": '{"source":"account-A"}'},
        {"role": "assistant", "content": "按只读范围回答", "evidence": {"source": "persisted-tool"}},
        {"role": "user", "content": [{"type": "input_image", "image_url": "prism-asset://known-hash"}]},
    ]
    _row, original, store = _seed(db, {"transcript": transcript})
    incoming = transcript + [{"role": "user", "content": "保留所有证据，继续"}]
    checkpoint = RunCheckpoint(run_id="keep-evidence-run", model="local-stub", tools=[], transcript=incoming)
    assert await store.create(checkpoint)
    assert (await store.load(checkpoint.run_id)).transcript == incoming
    assert api._server_history_transcript(
        db, user_id=7, surface="user", session_id="legacy-shape-session",
    ) == incoming
    assert db.query(AgentResponseRun).filter_by(run_id="legacy-shape-run").one().checkpoint_json == original
