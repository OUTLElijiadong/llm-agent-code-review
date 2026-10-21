"""小菱运行异常自愈：非人为中断自动恢复（有上限），人为停止直接停止。"""

import json

import pytest

from app.models.agent_response_run import AgentResponseRun
from app.services import agent_run_salvage
from app.services.deepseek_responses_runtime import emergency_compact_transcript


@pytest.fixture
def salvage_db(db, monkeypatch):
    monkeypatch.setattr(agent_run_salvage, "SessionLocal", lambda: _NoClose(db))
    dispatched = []
    monkeypatch.setattr(agent_run_salvage, "_dispatch_recovery",
                        lambda row, *, delay_seconds: dispatched.append(row.run_id))
    db.dispatched = dispatched
    return db


class _NoClose:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def _run(db, run_id, status="running", **checkpoint):
    row = AgentResponseRun(run_id=run_id, user_id=7, surface="user", session_key="s", status=status,
                           checkpoint_json=json.dumps({"status": status, **checkpoint}), version=1)
    db.add(row)
    db.commit()
    return row


def _checkpoint(db, run_id):
    db.expire_all()
    row = db.query(AgentResponseRun).filter_by(run_id=run_id).one()
    return row.status, json.loads(row.checkpoint_json)


def test_crashed_run_is_marked_recoverable_and_dispatched(salvage_db):
    _run(salvage_db, "crash-1")
    assert agent_run_salvage.salvage_crashed_run("crash-1", error="db hiccup") == "recovering"
    status, checkpoint = _checkpoint(salvage_db, "crash-1")
    assert status == "failed"
    assert checkpoint["recovery_requested"] is True
    assert checkpoint["context_metadata"]["auto_recovery_attempts"] == 1
    assert salvage_db.dispatched == ["crash-1"]


def test_auto_recovery_stops_after_limit(salvage_db):
    _run(salvage_db, "crash-2", context_metadata={"auto_recovery_attempts": agent_run_salvage.MAX_AUTO_RECOVERIES})
    assert agent_run_salvage.salvage_crashed_run("crash-2") == "failed"
    status, checkpoint = _checkpoint(salvage_db, "crash-2")
    assert status == "failed"
    assert checkpoint["recovery_requested"] is False
    assert "已安全停止" in checkpoint["error"]
    assert salvage_db.dispatched == []


def test_human_stop_is_never_recovered(salvage_db):
    _run(salvage_db, "stop-1", stop_requested=True, stop_reason="用户点了停止")
    assert agent_run_salvage.salvage_crashed_run("stop-1") == "cancelled"
    status, checkpoint = _checkpoint(salvage_db, "stop-1")
    assert status == "cancelled"
    assert checkpoint["recovery_requested"] is False
    assert salvage_db.dispatched == []


def test_orphan_sweep_skips_live_runs(salvage_db, monkeypatch):
    _run(salvage_db, "live-1")
    _run(salvage_db, "orphan-1")
    from app.api.v1 import agent_responses as api

    real = api._recover_stale_active_run
    monkeypatch.setattr(api, "_recover_stale_active_run",
                        lambda db, row, **kw: real(db, row, force=True))
    with agent_run_salvage.live_run("live-1"):
        assert agent_run_salvage.sweep_orphan_runs_once() == 1
    assert _checkpoint(salvage_db, "live-1")[0] == "running"
    assert _checkpoint(salvage_db, "orphan-1")[0] == "failed"
    assert salvage_db.dispatched == ["orphan-1"]


def test_emergency_compaction_keeps_user_constraints_and_shortens_tool_output():
    transcript = [{"role": "user", "content": "目标：审查项目 A"}]
    transcript.append({"type": "function_call", "call_id": "c1", "name": "get_agent_team", "arguments": "{}"})
    transcript.append({"type": "function_call_output", "call_id": "c1", "output": "结果" * 20000})
    transcript.append({"role": "user", "content": "约束：不得发布"})
    projected, metadata = emergency_compact_transcript(
        transcript, context_window_tokens=40000, max_output_tokens=4000,
        compaction_threshold_tokens=20000, keep_recent_tokens=8000,
    )
    text = json.dumps(projected, ensure_ascii=False)
    assert metadata["emergency_compaction"] is True
    assert "目标：审查项目 A" in text and "约束：不得发布" in text
    assert "平台已截断" in text
