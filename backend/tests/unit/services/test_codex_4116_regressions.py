"""Codex 4.1.16 验收问题回归：沙箱空项目、只读运维团队免确认。"""

import io
import zipfile

from app.services import agent_supervisor_service, sandbox_service


def _zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return out.getvalue()


def test_empty_project_archive_is_detected():
    assert sandbox_service._archive_is_empty(b"") is True
    assert sandbox_service._archive_is_empty(_zip({})) is True
    assert sandbox_service._archive_is_empty(_zip({"src/": ""})) is True
    assert sandbox_service._archive_is_empty(_zip({"app.py": "print(1)"})) is False
    # 非 ZIP 的隔离归档交给后续格式校验，不在这里误判为空。
    assert sandbox_service._archive_is_empty(b"\x1f\x8b not a zip") is False


def _ops_plan(action):
    return {
        "members": [
            {"member_key": "ops", "address": "agent:operations", "role": "worker"},
            {"member_key": "sum", "address": "agent:reporter", "role": "summarizer"},
        ],
        "tasks": [
            {"task_key": "check", "member_key": "ops", "title": "巡检", "instructions": "只读巡检",
             "input": {"action": action, "params": {}}},
            {"task_key": "sum", "member_key": "sum", "title": "汇总", "instructions": "汇总",
             "depends_on": ["check"]},
        ],
    }


def test_readonly_operations_team_runs_without_confirmation():
    review = agent_supervisor_service.review_agent_team_plan(_ops_plan("db_health"))
    assert review["needs_confirmation"] is False


def test_server_changing_operations_team_still_needs_confirmation():
    review = agent_supervisor_service.review_agent_team_plan(_ops_plan("restart_service"))
    assert review["needs_confirmation"] is True


def test_temporary_member_team_runs_without_confirmation():
    plan = {
        "members": [
            {"member_key": "tmp", "address": "temporary:reader", "kind": "temporary", "role": "worker"},
            {"member_key": "sum", "address": "agent:reporter", "role": "summarizer"},
        ],
        "tasks": [
            {"task_key": "read", "member_key": "tmp", "title": "读", "instructions": "读取源码并分析"},
            {"task_key": "sum", "member_key": "sum", "title": "汇总", "instructions": "汇总", "depends_on": ["read"]},
        ],
    }
    assert agent_supervisor_service.review_agent_team_plan(plan)["needs_confirmation"] is False


# ── 4.1.20：小菱是唯一主 Agent，圆桌/已发布 Agent/旧聊天入口不能绕开小菱 ──

def test_direct_agent_entry_points_only_through_xiaoling():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts/verify_permission_acceptance_https.py"
    spec = importlib.util.spec_from_file_location("xiaoling_only_route_runner", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    routes = {(row["method"], row["path"]) for row in runner.build_plan()["routes"]}
    for removed in [
        ("POST", "/api/discuss/start"),
        ("POST", "/api/ai/chat"),
        ("POST", "/api/ai/detect-language"),
        ("POST", "/api/agents/clarify"),
        ("GET", "/api/ai/agents"),
    ]:
        assert removed not in routes
    assert not any(method == "POST" and path.startswith("/api/agent-catalog") and path.endswith("/invoke")
                   for method, path in routes)
    assert ("POST", "/api/discuss/sessions/{session_id}/stop") in routes
    assert ("POST", "/api/ai/analyze-folder") in routes


def _roundtable_plan(task_input, objective="对项目做圆桌讨论审查"):
    from app.schemas.agent_team import AgentTeamCreateIn

    return AgentTeamCreateIn.model_validate({
        "surface": "user",
        "session_id": "session-roundtable",
        "title": "圆桌讨论",
        "objective": objective,
        "members": [
            {"member_key": "rt", "address": "agent:review_orchestrator", "role": "worker", "display_name": "圆桌"},
            {"member_key": "sum", "address": "agent:reporter", "role": "summarizer", "display_name": "汇总"},
        ],
        "tasks": [
            {"task_key": "rt", "member_key": "rt", "title": "圆桌", "instructions": "组织圆桌讨论",
             "input": task_input},
            {"task_key": "sum", "member_key": "sum", "title": "汇总", "instructions": "汇总",
             "depends_on": ["rt"]},
        ],
    })


def test_team_roundtable_operation_is_allowed_and_runs_without_confirmation():
    from app.services import agent_team_service

    payload = agent_team_service._normalize_task_inputs(
        _roundtable_plan({"operation": "start_roundtable", "project_id": 1, "file_id": 2})
    )
    assert payload.tasks[0].input["operation"] == "start_roundtable"
    review = agent_supervisor_service.review_agent_team_plan(payload.model_dump())
    assert review["needs_confirmation"] is False


def test_readonly_team_cannot_start_roundtable():
    import pytest

    from app.services import agent_team_service

    payload = _roundtable_plan({"operation": "start_roundtable", "project_id": 1, "file_id": 2},
                               objective="只读核对已有审查结果")
    with pytest.raises(agent_team_service.AgentTeamValidationError):
        agent_team_service._normalize_task_inputs(payload)


class _Row:
    def __init__(self, status):
        self.session_id = "rt-1"
        self.status = status
        self.project_id = 1
        self.file_id = 2
        self.file_name = "a.py"
        self.report_task_id = 99


class _FakeDb:
    def __init__(self, row):
        self.row = row

    def get(self, _model, _key):
        return self.row

    def commit(self):
        pass

    def rollback(self):
        pass

    def expire_all(self):
        pass


def _patch_roundtable(monkeypatch, *, raw, row, lease_ok=lambda: True):
    import json
    from types import SimpleNamespace

    from app.agents import discussion_bus
    from app.api.v1 import discussion, ws_discussion
    from app.services import agent_team_review, agent_team_service

    task = SimpleNamespace(input_json=json.dumps(raw), instructions="组织圆桌讨论")
    team = SimpleNamespace(objective="圆桌讨论审查")
    member = SimpleNamespace(address="agent:review_orchestrator")
    calls = {"start": 0, "launch": 0, "stop": []}

    def lease(*_args, **_kwargs):
        if not lease_ok():
            raise agent_team_service.AgentTeamLeaseError("lease lost")
        return team, task, member

    def start(**_kwargs):
        calls["start"] += 1
        return SimpleNamespace(data={"session_id": "rt-1"})

    def launch(_pending, timeout=10.0):
        calls["launch"] += 1
        return True

    bus = SimpleNamespace(
        get_session=lambda *_a, **_k: row,
        request_stop=lambda session_id, reason: calls["stop"].append((session_id, reason)),
    )
    monkeypatch.setattr(agent_team_service, "require_active_task_lease", lease)
    monkeypatch.setattr(agent_team_review, "_require_actor", lambda *_a: SimpleNamespace(id=7))
    monkeypatch.setattr(discussion, "start_discussion", start)
    monkeypatch.setattr(ws_discussion, "take_pending", lambda _sid: object())
    monkeypatch.setattr(ws_discussion, "launch_pending_discussion_from_thread", launch)
    monkeypatch.setattr(discussion_bus.DiscussionBus, "instance", classmethod(lambda cls: bus))
    monkeypatch.setattr(agent_team_review.time, "sleep", lambda _s: None)
    return task, calls


def _run_roundtable(row):
    from types import SimpleNamespace

    from app.services import agent_team_review

    return agent_team_review.run_team_roundtable(
        _FakeDb(row), SimpleNamespace(id=7), team_id=1, task_id=2, lease_token="t", project_id=1, file_id=2,
    )


def test_team_roundtable_completes_only_when_concluded(monkeypatch):
    import json

    raw = {"operation": "start_roundtable", "project_id": 1, "file_id": 2}
    row = _Row("concluded")
    task, calls = _patch_roundtable(monkeypatch, raw=raw, row=row)
    result = _run_roundtable(row)
    assert result["status"] == "completed"
    assert result["evidence"][0]["data"]["report_url"] == "/reports/99"
    assert calls["start"] == calls["launch"] == 1
    assert json.loads(task.input_json)["_roundtable"]["session_id"] == "rt-1"

    row.status = "partial"
    assert _run_roundtable(row)["status"] == "failed"


def test_team_roundtable_retry_reuses_session(monkeypatch):
    raw = {"operation": "start_roundtable", "project_id": 1, "file_id": 2, "_roundtable": {"session_id": "rt-1"}}
    row = _Row("concluded")
    _task, calls = _patch_roundtable(monkeypatch, raw=raw, row=row)
    assert _run_roundtable(row)["status"] == "completed"
    assert calls["start"] == calls["launch"] == 0


def test_team_stop_stops_running_roundtable(monkeypatch):
    raw = {"operation": "start_roundtable", "project_id": 1, "file_id": 2}
    row = _Row("running")
    state = {"n": 0}

    def lease_ok():
        state["n"] += 1
        return state["n"] == 1  # 首次取租约成功，轮询时团队已被人为停止

    _task, calls = _patch_roundtable(monkeypatch, raw=raw, row=row, lease_ok=lease_ok)
    result = _run_roundtable(row)
    assert result["status"] == "cancelled"
    assert calls["stop"] and calls["stop"][0][0] == "rt-1"


def test_team_roundtable_rejects_mismatched_parameters(monkeypatch):
    import pytest

    from app.services import agent_team_service

    raw = {"operation": "start_roundtable", "project_id": 1, "file_id": 3}
    row = _Row("concluded")
    _patch_roundtable(monkeypatch, raw=raw, row=row)
    with pytest.raises(agent_team_service.AgentTeamAccessError):
        _run_roundtable(row)
