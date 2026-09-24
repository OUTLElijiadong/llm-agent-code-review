from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.discussion_bus import DiscussionBus
from app.agents.events import AgentEventType, DiscussionTurn
from app.agents.security_sentinel_agent import SecuritySentinelAgent, _AuditChunkResult
from app.ai import discussion_orchestrator as discussion
from app.ai.exceptions import AiServiceError
from app.ai.multi_agent import GENERAL_AGENT, SECURITY_AGENT
from app.ai.result_parser import Issue
from app.core.database import Base
from app.core.exceptions import ValidationError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.roundtable import RoundtableSession, RoundtableTurn  # noqa: F401 - 注册隔离 SQLite 会话表
from app.models.user import User
from app.services import project_source_service
from app.services.review_input_service import load_task_inputs


@pytest.mark.parametrize("scan_mode", ["full", "static_full", "triage"])
@pytest.mark.parametrize("contents", [[], [("", 0)], [(" \n\t", 0)], [("binary payload", 1)]])
@pytest.mark.parametrize("audit_run_id", ["", "isolated-audit"])
def test_security_project_rejects_no_effective_input(monkeypatch, scan_mode, contents, audit_run_id):
    agent = SecuritySentinelAgent()
    agent.inject(Mock())
    agent._db.get.return_value = Project(id=7, project_name="isolated", status="active")
    monkeypatch.setattr(agent, "_authz_project", lambda _project: None)
    files = [
        CodeFile(id=index, project_id=7, file_name="input.bin", file_path="input.bin",
                 language="python", content=content, is_binary=is_binary, status="active")
        for index, (content, is_binary) in enumerate(contents, 1)
    ]
    monkeypatch.setattr(project_source_service, "begin_source_archive_audit", lambda *_args: audit_run_id)
    monkeypatch.setattr(project_source_service, "load_project_source_files", lambda *_args: files)
    archive = Mock(return_value=(b"isolated archive", "source.zip"))
    finish = Mock(return_value=True)
    monkeypatch.setattr(project_source_service, "build_source_archive", archive)
    monkeypatch.setattr(project_source_service, "finish_source_archive_audit", finish)
    model = Mock(side_effect=AssertionError("empty scan must not invoke a model"))
    monkeypatch.setattr(agent, "call_json", model)
    events = Mock()
    monkeypatch.setattr(agent, "_emit", events)

    result = agent.scan_project(7, scan_mode=scan_mode)

    assert result.success is False
    assert result.failure_kind == "empty_scan_input"
    assert "非空文本" in result.error
    assert result.data["scan_mode"] == scan_mode
    assert result.data["archive_text_file_count"] == 0
    assert result.data["semantic_source_chars"] == 0
    assert result.data["audit_complete"] is False
    archive.assert_not_called()
    model.assert_not_called()
    assert any(call.args[0] == AgentEventType.FAILED for call in events.call_args_list)
    if audit_run_id:
        assert finish.call_args.args[2] == "failed"
        assert finish.call_args.kwargs["audit_run_id"] == audit_run_id
    else:
        finish.assert_not_called()


@pytest.fixture
def roundtable_store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'roundtable.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(discussion, "SessionLocal", factory)
    with factory() as database:
        database.add(CodeFile(id=11, project_id=7, file_name="before.py", file_path="before.py",
                              language="python", content="value = 2\n", version_no=2,
                              is_binary=0, status="active", line_count=1))
        database.add_all([
            CodeVersion(file_id=11, version_no=1, content="value = 1\n", create_time=datetime.now(timezone.utc)),
            CodeVersion(file_id=11, version_no=2, content="value = 2\n", create_time=datetime.now(timezone.utc)),
        ])
        database.commit()
    yield factory
    engine.dispose()


def create_roundtable(**overrides):
    arguments = dict(user_id=3, project_id=7, file_id=11, file_name="before.py", code="value = 1\n",
                     language="python", review_type="full", model_name="isolated", profiles=(GENERAL_AGENT,))
    arguments.update(overrides)
    return discussion._create_review_task(**arguments)


def test_roundtable_freezes_actual_pending_code_not_latest(roundtable_store):
    task_id = create_roundtable()
    with roundtable_store() as database:
        link = database.query(ReviewTaskFile).filter_by(task_id=task_id).one()
        assert link.version_no == 1
        assert link.content_sha256 == hashlib.sha256(b"value = 1\n").hexdigest()
        assert load_task_inputs(database, task_id)[0].content == "value = 1\n"
        assert database.get(CodeFile, 11).content == "value = 2\n"


@pytest.mark.parametrize("code", ["", " \n\t", "not recorded in version history"])
def test_roundtable_invalid_or_missing_version_creates_nothing(roundtable_store, code):
    with pytest.raises(ValidationError):
        create_roundtable(code=code)
    with roundtable_store() as database:
        assert database.query(ReviewTask).count() == 0
        assert database.query(ReviewTaskFile).count() == 0


def test_roundtable_snapshot_failure_rolls_back_task(roundtable_store, monkeypatch):
    monkeypatch.setattr(discussion, "freeze_task_inputs", Mock(side_effect=ValidationError("snapshot failed")),
                        raising=False)
    with pytest.raises(ValidationError, match="snapshot failed"):
        create_roundtable()
    with roundtable_store() as database:
        assert database.query(ReviewTask).count() == 0
        assert database.query(ReviewTaskFile).count() == 0


@pytest.mark.parametrize("status", ["cancelled", "deleted", "failed", "success"])
@pytest.mark.parametrize("extraction_fails", [False, True])
def test_finalization_never_revives_terminal_task(roundtable_store, monkeypatch, status, extraction_fails):
    with roundtable_store() as database:
        task = ReviewTask(user_id=3, project_id=7, review_type="discuss", status=status, summary="terminal")
        database.add(task)
        database.commit()
        task_id = task.id
    extract = Mock(side_effect=RuntimeError("extract failed")) if extraction_fails else Mock(return_value=[])
    monkeypatch.setattr(discussion, "_extract_issues", extract)
    discussion._finalize_review(
        task_id=task_id, user_id=3, file_id=11, file_name="before.py", all_turns=[],
        code="value = 1\n", language="python", deferred_logs=[], agent=SimpleNamespace(model="isolated"),
        stopped=False,
    )
    extract.assert_not_called()
    with roundtable_store() as database:
        task = database.get(ReviewTask, task_id)
        assert task.status == status
        assert task.summary == "terminal"
        assert database.query(ReviewIssue).count() == 0


@pytest.fixture
def live_roundtable(roundtable_store, monkeypatch):
    bus = DiscussionBus()
    monkeypatch.setattr(discussion.DiscussionBus, "instance", lambda: bus)
    monkeypatch.setattr(discussion, "build_discussion_environment", Mock(side_effect=RuntimeError("offline")))
    agent = SimpleNamespace(model="isolated", call_raw=Mock(side_effect=AssertionError("unexpected model call")))
    monkeypatch.setattr(discussion, "DeepSeekAgent", Mock(return_value=agent))
    orchestrator = discussion.DiscussionOrchestrator()
    session = bus.create_session("isolated", task_id=0, file_name="before.py", owner_user_id=3, max_rounds=1)
    real_sleep = asyncio.sleep

    async def no_delay(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(discussion.asyncio, "sleep", no_delay)
    arguments = dict(session_id="isolated", profiles=(GENERAL_AGENT, SECURITY_AGENT), code="value = 1\n",
                     language="python", file_name="before.py", user_id=3, project_id=7, file_id=11, max_rounds=1)
    return orchestrator, bus, session, arguments, agent


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["", " \n\t", "missing historic evidence"])
async def test_discussion_invalid_input_closes_with_error_without_model(live_roundtable, roundtable_store, code):
    orchestrator, bus, session, arguments, agent = live_roundtable
    queue = await bus.subscribe("isolated")
    arguments["code"] = code
    await orchestrator.start_discussion(**arguments)
    agent.call_raw.assert_not_called()
    assert session.status == "concluded"
    controls = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    assert any(item.get("payload", {}).get("error") for item in controls)
    with roundtable_store() as database:
        assert database.query(ReviewTask).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes", ["failed", "silent", "partial", "success"])
async def test_discussion_only_complete_valid_speech_can_succeed(
    live_roundtable, roundtable_store, monkeypatch, outcomes,
):
    orchestrator, bus, session, arguments, _agent = live_roundtable
    valid = (
        discussion.SpeakerDecision("speak", "neutral", None, "经检查没有发现问题"),
        {"model_name": "isolated"}, True,
    )
    failed = (discussion.SpeakerDecision("speak", "neutral", None, "技术问题"), None, False)
    silent = (discussion.SpeakerDecision("silent", "neutral", None, "没有新增观点"), {}, True)
    results = {
        "failed": [failed, failed], "silent": [silent, silent],
        "partial": [valid, failed], "success": [valid, valid],
    }
    monkeypatch.setattr(orchestrator, "_speaker_turn", AsyncMock(side_effect=results[outcomes]))
    summarize = Mock(return_value=("完整小结", {"model_name": "isolated"}))
    monkeypatch.setattr(orchestrator, "_summarize", summarize)
    extract = Mock(return_value=(
        [Issue(title="已有发现", severity="高", description="证据")]
        if outcomes == "partial" else []
    ))
    monkeypatch.setattr(discussion, "_extract_issues", extract)
    monkeypatch.setattr(discussion, "_normalize_discussion_issues", lambda findings, **_kwargs: findings)
    await orchestrator.start_discussion(**arguments)
    with roundtable_store() as database:
        task = database.query(ReviewTask).one()
        assert task.status == ("success" if outcomes == "success" else "failed")
        assert task.coverage["valid_speeches"] == {"failed": 0, "silent": 0, "partial": 1, "success": 2}[outcomes]
        assert task.processed_files == (1 if outcomes == "success" else 0)
        if outcomes == "success":
            assert task.score == 100
        else:
            assert task.error_message
            assert task.score != 100
        if outcomes == "partial":
            assert database.query(ReviewIssue).count() == task.total_issues == 1
        if outcomes in {"silent", "failed"}:
            summarize.assert_not_called()
            extract.assert_not_called()
    assert session.report_task_id > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["deleted", "cancelled"])
async def test_deleted_or_cancelled_during_speaker_stops_other_models(
    live_roundtable, roundtable_store, monkeypatch, status,
):
    orchestrator, _bus, _session, arguments, _agent = live_roundtable

    async def speaker(**_kwargs):
        with roundtable_store() as database:
            database.query(ReviewTask).update({"status": status, "summary": "terminal"})
            database.commit()
        return discussion.SpeakerDecision("speak", "neutral", None, "first speech"), {}, True

    turn = AsyncMock(side_effect=speaker)
    monkeypatch.setattr(orchestrator, "_speaker_turn", turn)
    summary = Mock()
    extract = Mock(return_value=[])
    monkeypatch.setattr(orchestrator, "_summarize", summary)
    monkeypatch.setattr(discussion, "_extract_issues", extract)
    await orchestrator.start_discussion(**arguments)
    assert turn.await_count == 1
    summary.assert_not_called()
    extract.assert_not_called()
    with roundtable_store() as database:
        assert database.query(ReviewTask).one().status == status


@pytest.mark.parametrize("status", ["cancelled", "deleted"])
@pytest.mark.parametrize("extraction_fails", [False, True])
def test_terminal_status_written_during_extraction_wins(
    roundtable_store, monkeypatch, status, extraction_fails,
):
    task_id = create_roundtable()

    def extract(*_args, **_kwargs):
        with roundtable_store() as database:
            task = database.get(ReviewTask, task_id)
            task.status = status
            task.summary = "terminal from another worker"
            database.commit()
        if extraction_fails:
            raise RuntimeError("late extraction failure")
        return [Issue(title="late finding", severity="高")]

    monkeypatch.setattr(discussion, "_extract_issues", extract)
    discussion._finalize_review(
        task_id=task_id, user_id=3, file_id=11, file_name="before.py",
        all_turns=[DiscussionTurn(turn_id=1, agent_code="general", agent_name="reviewer",
                                  role="agent", content="valid speech")],
        code="value = 1\n", language="python", deferred_logs=[], agent=SimpleNamespace(model="isolated"),
        stopped=False, consensus="summary",
    )
    with roundtable_store() as database:
        assert database.get(ReviewTask, task_id).status == status
        assert database.get(ReviewTask, task_id).summary == "terminal from another worker"
        assert database.query(ReviewIssue).count() == 0


@pytest.mark.asyncio
async def test_summary_fallback_keeps_partial_report_failed(live_roundtable, roundtable_store, monkeypatch):
    orchestrator, _bus, _session, arguments, _agent = live_roundtable
    monkeypatch.setattr(orchestrator, "_speaker_turn", AsyncMock(return_value=(
        discussion.SpeakerDecision("speak", "neutral", None, "valid speech"), {}, True,
    )))
    monkeypatch.setattr(orchestrator, "_summarize", Mock(return_value=("local fallback", None)))
    monkeypatch.setattr(discussion, "_extract_issues", Mock(return_value=[]))
    await orchestrator.start_discussion(**arguments)
    with roundtable_store() as database:
        task = database.query(ReviewTask).one()
        assert task.status == "failed"
        assert task.score != 100
        assert task.coverage["summary_status"] == "failed"
        assert task.coverage["stage"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", " \n\t"])
async def test_real_empty_speaker_output_does_not_trigger_summary_or_extraction(
    live_roundtable, roundtable_store, response,
):
    orchestrator, _bus, _session, arguments, agent = live_roundtable
    agent.call_raw.side_effect = None
    agent.call_raw.return_value = (response, {})
    await orchestrator.start_discussion(**arguments)
    assert agent.call_raw.call_count == len(arguments["profiles"])
    with roundtable_store() as database:
        task = database.query(ReviewTask).one()
        assert task.status == "failed"
        assert task.coverage["valid_speeches"] == 0
        assert task.coverage["failed_turns"] == len(arguments["profiles"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation", ["task", "stop"])
async def test_cancellation_while_finalizer_runs_revokes_worker_before_late_result(
    live_roundtable, roundtable_store, monkeypatch, cancellation,
):
    orchestrator, bus, session, arguments, _agent = live_roundtable
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(orchestrator, "_speaker_turn", AsyncMock(return_value=(
        discussion.SpeakerDecision("speak", "neutral", None, "valid speech"), {}, True,
    )))
    monkeypatch.setattr(orchestrator, "_summarize", Mock(return_value=("summary", {})))

    def blocking_extract(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return []

    finalize = discussion._finalize_review

    def finish_report(**kwargs):
        try:
            return finalize(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(discussion, "_extract_issues", blocking_extract)
    monkeypatch.setattr(discussion, "_finalize_review", finish_report)
    running = bus.start_discussion_task("isolated", orchestrator.start_discussion(**arguments))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        if cancellation == "task":
            running.cancel()
        else:
            orchestrator._handle_control("stop", {})
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 3), 4)
    with roundtable_store() as database:
        assert database.query(ReviewTask).one().status == "cancelled"
        assert database.query(ReviewIssue).count() == 0
    assert session.status == "concluded"


@pytest.mark.asyncio
async def test_rest_pending_to_websocket_keeps_preflight_version(
    live_roundtable, roundtable_store, monkeypatch,
):
    from app.api.v1 import discussion as rest
    from app.api.v1 import ws_discussion as websocket

    _orchestrator, bus, _session, _arguments, agent = live_roundtable
    monkeypatch.setattr(rest, "_gen_session_id", lambda: "rest-to-ws")
    monkeypatch.setattr(rest.rule_service, "get_enabled_rules", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(websocket, "_is_session_version_active", lambda *_args: True)
    monkeypatch.setattr(websocket, "_pending", {})
    monkeypatch.setattr(websocket, "_session_owners", {})
    monkeypatch.setattr(websocket, "_owner_registered_at", {})
    with roundtable_store() as database:
        owner = User(
            id=3, username="offline-owner", password="offline",
            email="owner@example.test", role="admin", status=1,
        )
        database.add(owner)
        database.add(Project(id=7, user_id=3, project_name="offline-project", language="python", status="active"))
        code_file = database.get(CodeFile, 11)
        code_file.content = "value = 1\n"
        code_file.version_no = 1
        database.commit()
        response = rest.start_discussion(project_id=7, file_id=11, review_type="full", db=database, user=owner)
        assert response.data["session_id"] == "rest-to-ws"
        code_file.content = "value = 2\n"
        code_file.version_no = 2
        database.commit()
    pending = websocket.take_pending("rest-to-ws")
    assert pending.kwargs["code"] == "value = 1\n"
    agent.call_raw.side_effect = None
    agent.call_raw.return_value = ("", {})
    await websocket._run_pending_discussion(pending)
    with roundtable_store() as database:
        task = database.query(ReviewTask).one()
        assert task.status == "failed"
        frozen = load_task_inputs(database, task.id)[0]
        assert frozen.version_no == 1
        assert frozen.content == "value = 1\n"
    assert bus.get_session("rest-to-ws").status == "concluded"
    assert agent.call_raw.call_count > 0
    assert all("value = 1" in call.kwargs["user_prompt"] for call in agent.call_raw.call_args_list)
    assert all("value = 2" not in call.kwargs["user_prompt"] for call in agent.call_raw.call_args_list)


@pytest.mark.parametrize("scan_depth", ["quick", "standard", "deep"])
@pytest.mark.parametrize("content,is_binary", [("", 0), (" \n\t", 0), ("binary payload", 1)])
def test_security_file_rejects_empty_text_before_any_scanner(monkeypatch, scan_depth, content, is_binary):
    agent = SecuritySentinelAgent()
    database = Mock()
    database.get.return_value = CodeFile(
        id=11, project_id=7, file_name="input.py", file_path="input.py", language="python",
        content=content, is_binary=is_binary, status="active",
    )
    agent.inject(database)
    scanners = []
    for name in ("_regex_findings", "_static_findings", "_llm_audit_collect"):
        scanner = Mock(return_value=_AuditChunkResult() if name == "_llm_audit_collect" else [])
        monkeypatch.setattr(agent, name, scanner)
        scanners.append(scanner)
    events = Mock()
    monkeypatch.setattr(agent, "_emit", events)
    result = agent.scan_file(11, scan_depth=scan_depth)
    assert result.success is False
    assert result.failure_kind == "empty_scan_input"
    assert "非空文本" in result.error
    assert result.data["compliance"]["scan_complete"] is False
    assert result.data["file_count"] == 0
    assert result.data.get("risk_score") != 100
    assert any(call.args[0] == AgentEventType.FAILED for call in events.call_args_list)
    for scanner in scanners:
        scanner.assert_not_called()


@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
def test_security_all_projects_rejects_empty_visible_scope(db, monkeypatch, role):
    agent = SecuritySentinelAgent()
    owner = User(id=7, username="empty-scope", role=role, status=1)
    if role == "user":
        db.add(Project(id=19, user_id=99, project_name="not-visible", language="python", status="active"))
        db.commit()
    agent.inject(db, user=owner)
    scan_project = Mock(side_effect=AssertionError("empty scope must not scan a project"))
    discussion_result = Mock(return_value={})
    monkeypatch.setattr(agent, "scan_project", scan_project)
    monkeypatch.setattr(agent, "_build_multi_agent_discussion", discussion_result)
    events = Mock()
    monkeypatch.setattr(agent, "_emit", events)
    result = agent.scan_all_projects()
    assert result.success is False
    assert result.failure_kind == "empty_scan_input"
    assert result.error
    assert result.data["compliance"]["scan_complete"] is False
    assert result.data["compliance"]["project_count"] == 0
    assert result.data.get("risk_score") != 100
    assert any(call.args[0] == AgentEventType.FAILED for call in events.call_args_list)
    scan_project.assert_not_called()
    discussion_result.assert_not_called()


@pytest.mark.parametrize("scan_depth", ["quick", "standard", "deep"])
def test_security_file_valid_zero_findings_still_succeeds(monkeypatch, scan_depth):
    agent = SecuritySentinelAgent()
    database = Mock()
    database.get.return_value = CodeFile(
        id=11, project_id=7, file_name="input.py", language="python",
        content="value = 1\n", is_binary=0, status="active",
    )
    agent.inject(database)
    monkeypatch.setattr(agent, "_regex_findings", Mock(return_value=[]))
    monkeypatch.setattr(agent, "_static_findings", Mock(return_value=[]))
    monkeypatch.setattr(agent, "_llm_audit_collect", Mock(return_value=_AuditChunkResult()))
    result = agent.scan_file(11, scan_depth=scan_depth)
    assert result.success is True
    assert result.data["file_count"] == 1
    assert result.data["findings"] == []
    assert result.data["risk_score"] == 100


@pytest.mark.parametrize("scope", ["file", "all_projects"])
def test_security_api_does_not_serialize_empty_input_as_success(db, monkeypatch, scope):
    from app.api.v1 import security as security_api
    from app.schemas.security import SecurityScanAllProjectsIn, SecurityScanFileIn

    agent = SecuritySentinelAgent()
    owner = User(id=7, username="offline-api", role="admin", status=1)
    agent.inject(db, user=owner)
    monkeypatch.setattr(security_api, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace(
        security_sentinel=agent,
    ))
    model = Mock(side_effect=AssertionError("empty input must not call the model"))
    monkeypatch.setattr(agent, "call_json", model)
    if scope == "file":
        db.add(Project(id=7, user_id=owner.id, project_name="empty-input-api", status="active"))
        db.add(CodeFile(id=11, project_id=7, file_name="input.py", language="python",
                        content=" \n\t", is_binary=0, status="active"))
        db.commit()
        with pytest.raises(AiServiceError, match="非空文本"):
            security_api.scan_file(SecurityScanFileIn(file_id=11), db=db, user=owner)
    else:
        with pytest.raises(AiServiceError, match="可见且可扫描"):
            security_api.scan_all_projects(SecurityScanAllProjectsIn(), db=db, user=owner)
    model.assert_not_called()
