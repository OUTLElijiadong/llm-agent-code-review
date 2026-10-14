"""团队列表仅读取摘要元数据与任务状态统计，详情继续读取完整业务结果。"""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import sessionmaker

from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
from app.services import agent_team_service


def _seed_team(db, *, owner=7, surface="user", session="summary-session", status="running", title="团队摘要"):
    team = AgentTeam(
        user_id=owner, surface=surface, session_key=session, title=title, objective="保留业务目标",
        status=status, max_active_children=3, max_attempts=2, priority=4, trace_id=f"trace-{title}",
        summary_json=json.dumps({"synthetic_payload": "x" * 1024 * 1024}), error_json="{}",
        create_time=datetime(2026, 10, 14), update_time=datetime(2026, 10, 14),
    )
    db.add(team)
    db.flush()
    member = AgentTeamMember(
        team_id=team.id, member_key="reader", display_name="读取子 Agent", address="agent:project_analyzer",
        kind="runtime", role="worker", status="running", capabilities_json="{}",
    )
    db.add(member)
    db.flush()
    for index, task_status in enumerate(("completed", "running", "waiting_dependency", "dead_letter", "blocked")):
        db.add(AgentTeamTask(
            team_id=team.id, member_id=member.id, task_key=f"task-{index}", title=f"工作项{index}",
            instructions="执行工作项", status=task_status,
            result_json=json.dumps({"synthetic_payload": "x" * 1024 * 1024}),
            input_json="{}", artifacts_json="[]", errors_json="[]",
        ))
    db.commit()
    return team


def _capture_selects(db):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    return statements, capture


def test_team_list_is_real_summary_without_loading_or_parsing_business_payload(db, monkeypatch):
    team = _seed_team(db)
    team_id = team.id
    db.expunge_all()
    statements, capture = _capture_selects(db)
    try:
        listed = agent_team_service.list_teams(db, SimpleNamespace(id=7, role="user"), limit=20)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)

    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["team_id"] == team_id
    assert {"team_id", "title", "surface", "session_id", "status", "max_active_children", "trace_id", "counts"} <= item.keys()
    assert item["objective"] == "保留业务目标"
    assert item["counts"] == {"total": 5, "completed": 1, "running": 1, "queued": 1, "failed": 1, "blocked": 1}
    assert not {"members", "tasks", "events", "messages", "summary", "error"} & item.keys()
    assert len(statements) == 3  # count、摘要页和一次批量任务状态聚合。
    assert all(not any(column in sql for column in (
        "summary_json", "error_json", "result_json", "input_json", "errors_json", "artifacts_json", "capabilities_json",
    )) for sql in statements)
    assert not any("agent_team_member" in sql for sql in statements)

    def reject_projection(*_args, **_kwargs):
        raise AssertionError("团队摘要列表不能解析或脱敏业务结果")

    monkeypatch.setattr(agent_team_service, "project_team_result", reject_projection)
    assert agent_team_service.list_teams(db, SimpleNamespace(id=7, role="user"))["total"] == 1


def test_team_summary_keeps_owner_surface_session_status_filters_and_page_total(db):
    first = _seed_team(db, title="同账号首项")
    latest = _seed_team(db, title="同账号最新项")
    admin = _seed_team(db, surface="admin", title="同账号管理端")
    _seed_team(db, owner=8, title="其他账号")
    _seed_team(db, session="other-session", status="failed", title="其他会话状态")
    user = SimpleNamespace(id=7, role="user")

    page = agent_team_service.list_teams(
        db, user, surface="user", session_id="summary-session", status="running", limit=1,
    )
    assert page["total"] == 2
    assert [item["team_id"] for item in page["items"]] == [latest.id]
    older = agent_team_service.list_teams(
        db, user, surface="user", session_id="summary-session", status="running", limit=1, offset=1,
    )
    assert older["total"] == 2
    assert [item["team_id"] for item in older["items"]] == [first.id]
    ordinary = agent_team_service.list_teams(db, user)
    assert ordinary["total"] == 3
    assert all(item["user_id"] == 7 and item["surface"] == "user" for item in ordinary["items"])
    assert agent_team_service.list_teams(
        db, SimpleNamespace(id=7, role="admin"), surface="admin",
    )["items"][0]["team_id"] == admin.id
    with pytest.raises(agent_team_service.AgentTeamAccessError):
        agent_team_service.list_teams(db, user, surface="admin")


def test_team_summary_refreshes_latest_status_timestamp_and_counts_despite_cached_orm(db):
    cached = _seed_team(db)
    newer = datetime(2026, 10, 14, 1, 2, 3)
    peer = sessionmaker(bind=db.get_bind(), expire_on_commit=False)()
    try:
        peer.execute(update(AgentTeam).where(AgentTeam.id == cached.id).values(status="completed", update_time=newer))
        peer.execute(update(AgentTeamTask).where(
            AgentTeamTask.team_id == cached.id, AgentTeamTask.status == "running",
        ).values(status="completed"))
        peer.commit()
    finally:
        peer.close()
    assert cached.status == "running"

    item = agent_team_service.list_teams(db, SimpleNamespace(id=7, role="user"))["items"][0]
    assert item["status"] == "completed"
    assert item["updated_at"] == newer.isoformat()
    assert item["counts"]["completed"] == 2
    assert item["counts"]["running"] == 0


def test_team_detail_retains_member_tasks_and_complete_result_readback(db):
    team = _seed_team(db)
    task = db.query(AgentTeamTask).filter_by(team_id=team.id, task_key="task-0").one()
    task.result_json = json.dumps({"title": "完整业务正文", "api_key": "sk-synthetic-hidden-value"})
    db.commit()
    detail = agent_team_service.get_team(db, SimpleNamespace(id=7, role="user"), team.id)
    assert detail["members"][0]["member_key"] == "reader"
    assert detail["tasks"][0]["result"]["title"] == "完整业务正文"
    assert "sk-synthetic-hidden-value" not in json.dumps(detail)
    assert {"tasks", "members", "events", "messages", "summary", "error"} <= detail.keys()
