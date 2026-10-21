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
