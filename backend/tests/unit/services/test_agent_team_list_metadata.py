"""团队列表只提供状态元数据，完整业务结果由同账号详情读取。"""
# ruff: noqa: F811 -- 跨文件共享pytest fixture名与参数名保持一致。
import json
from datetime import datetime

import pytest
from sqlalchemy import event

from app.models.agent_team import AgentTeam, AgentTeamTask
from app.models.user import User
from app.services import agent_team_service
from tests.unit.services.test_site_agent_service import actor, claimed_team  # noqa: F401


@pytest.mark.parametrize("round_number", range(3))
def test_team_list_never_loads_large_task_or_team_json(db, actor, round_number):
    created, claim = claimed_team(db, actor)
    team = db.get(AgentTeam, created["team_id"])
    team.summary_json = json.dumps({"body": "s" * 1000000})
    task = db.get(AgentTeamTask, claim["task_id"])
    original = {"body": "正文" * 100000, "sentinel": f"完整尾部-{round_number}"}
    task.result_json = json.dumps(original)
    db.commit()
    db.expunge_all()
    statements = []
    def capture(_conn, _cursor, sql, _params, _context, _many):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql.lower())
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        output = agent_team_service.list_teams(db, actor, surface="user", session_id="site-session")
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert output["total"] == 1
    item = output["items"][0]
    assert item["team_id"] == created["team_id"]
    assert item["counts"] == {"total": 2, "completed": 0, "running": 1, "queued": 1, "failed": 0, "blocked": 0}
    assert not {"tasks", "members", "summary", "error", "messages", "events"}.intersection(item)
    assert not any("agent_team_member" in sql for sql in statements)
    assert not any(field in sql for sql in statements for field in (
        "result_json", "summary_json", "error_json", "input_json", "capabilities_json"))
    assert all("count(" in sql for sql in statements if "agent_team_task" in sql)
    # 列表未改动原结果；同账号详情和分页仍能读取末尾。
    detail = agent_team_service.get_team(db, actor, created["team_id"])
    assert detail["tasks"][0]["result"]["result_reference"]["task_id"] == claim["task_id"]
    leaf = agent_team_service.get_team_result_page(db, actor, created["team_id"], claim["task_id"],
                                                   result_path="/sentinel")
    assert leaf["text"] == original["sentinel"]


def test_list_metadata_keeps_owner_surface_session_status_order_and_counts(db, actor, monkeypatch):
    monkeypatch.setattr(agent_team_service, "_is_admin", lambda _db, user: user.username == "meta-admin")
    other = User(username="meta-admin", password="unused", role="admin", status=1)
    db.add(other)
    db.flush()
    teams = []
    for owner, surface, session, status in [
        (actor.id, "user", "one", "completed"), (actor.id, "user", "two", "failed"),
        (actor.id, "admin", "one", "completed"), (other.id, "user", "one", "completed"),
    ]:
        row = AgentTeam(user_id=owner, surface=surface, session_key=session, title="元数据",
            objective="只读", status=status, trace_id=f"trace-{len(teams)}",
            update_time=datetime(2026, 10, 14))
        db.add(row)
        db.flush()
        teams.append(row)
    statuses = ["completed", "running", "queued", "waiting_dependency", "failed", "dead_letter",
                "expired", "blocked", "cancelled"]
    for index, status in enumerate(statuses):
        db.add(AgentTeamTask(team_id=teams[0].id, member_id=1, task_key=str(index), title="任务",
            instructions="只读", status=status))
    db.commit()
    output = agent_team_service.list_teams(db, actor)
    assert [item["team_id"] for item in output["items"]] == [teams[1].id, teams[0].id]
    assert output["items"][1]["counts"] == {
        "total": 9, "completed": 1, "running": 1, "queued": 2, "failed": 3, "blocked": 1,
    }
    assert agent_team_service.list_teams(db, actor, session_id="one", status="completed")["total"] == 1
    assert agent_team_service.list_teams(db, actor, offset=1, limit=1)["items"][0]["team_id"] == teams[0].id
    assert agent_team_service.list_teams(db, other)["items"][0]["team_id"] == teams[3].id
    assert agent_team_service.list_teams(db, actor, session_id="missing") == {"items": [], "total": 0}
