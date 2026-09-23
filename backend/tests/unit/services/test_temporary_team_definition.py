"""任务内生成定义必须冻结、归属可核验且不能独立复用。"""
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.agents.tool_contracts import CreateAgentTeamArguments
from app.models.agent_team import AgentTeam, AgentTeamMember
from app.models.custom_agent import CustomAgent
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_mesh_dispatcher, agent_team_dispatcher, agent_team_service


def payload():
    return {
        "surface": "user", "session_id": "temporary-definition-test",
        "title": "临时专项分析", "objective": "按当前任务生成专项分析成员",
        "members": [{"member_key": "access", "display_name": "权限精灵",
                     "address": "temporary:access", "role": "summarizer",
                     "definition": {"purpose": "分析权限边界", "instructions": "核验输入材料中的权限漏洞并提供证据"}}],
        "tasks": [{"task_key": "analyze", "member_key": "access", "title": "专项分析",
                   "instructions": "分析权限设计", "input": {"text": "每次请求检查当前账号"}}],
    }


def test_tool_and_api_accept_same_temporary_definition():
    raw = payload()
    api = AgentTeamCreateIn.model_validate(raw)
    tool = CreateAgentTeamArguments.model_validate({k: v for k, v in raw.items() if k not in {"surface", "session_id"}})
    assert tool.members[0].definition == api.members[0].definition


def test_temporary_coverage_remains_explicit_in_team_summary():
    from app.services.agent_team_summary import dependency_coverage_summary

    summary = dependency_coverage_summary({
        "execution_mode": "analysis_only", "formal_review": False,
        "coverage": {"total_files": 8, "included_file_count": 5, "source_chars_included": 50000,
                     "omitted_files": 3, "max_files": 5, "max_file_chars": 12000,
                     "truncated": True, "complete": False},
    })
    assert summary["included_file_count"] == 5
    assert summary["source_chars_included"] == 50000
    assert summary["complete"] is False and summary["formal_review"] is False
    assert summary["execution_mode"] == "analysis_only"


@pytest.mark.parametrize("change", ["missing", "existing", "wrong_key", "extra_authority"])
def test_definition_cannot_override_address_or_authority(change):
    raw = payload()
    member = raw["members"][0]
    if change == "missing":
        member.pop("definition")
    elif change == "existing":
        member["address"] = "agent:reporter"
    elif change == "wrong_key":
        member["address"] = "temporary:other"
    else:
        member["definition"]["user_id"] = 999
    with pytest.raises(ValidationError):
        AgentTeamCreateIn.model_validate(raw)


def test_generated_definition_is_frozen_without_permanent_agent(db, admin_user):
    before = db.query(CustomAgent).count()
    created = agent_team_service.create_team(db, admin_user, AgentTeamCreateIn.model_validate(payload()))
    row = db.query(AgentTeamMember).filter_by(team_id=created["team_id"]).one()
    snapshot = json.loads(row.capabilities_json)
    assert row.kind == "temporary"
    assert snapshot["temporary_definition"] == payload()["members"][0]["definition"]
    assert len(snapshot["definition_checksum"]) == 64
    assert db.query(CustomAgent).count() == before


@pytest.mark.parametrize("invalid", ["untrusted", "owner", "member", "checksum", "cancelled"])
def test_temporary_dispatch_rejects_invalid_lease_or_snapshot(db, admin_user, monkeypatch, invalid):
    from app.services import temporary_agent_runtime
    run = Mock(return_value={"status": "completed", "summary": "verified"})
    monkeypatch.setattr(temporary_agent_runtime, "run_temporary_agent", run)
    created = agent_team_service.create_team(db, admin_user, AgentTeamCreateIn.model_validate(payload()))
    team = db.get(AgentTeam, created["team_id"])
    claimed = agent_team_service.claim_next_task(db, team.id)
    message = agent_team_dispatcher._task_message(team, claimed)
    if invalid == "owner":
        message["user_id"] = admin_user.id + 1
    elif invalid == "member":
        message["context"]["member_id"] += 1
    elif invalid == "checksum":
        row = db.get(AgentTeamMember, claimed["member_id"])
        snapshot = json.loads(row.capabilities_json)
        snapshot["temporary_definition"]["instructions"] = "篡改职责"
        row.capabilities_json = json.dumps(snapshot)
        db.commit()
    elif invalid == "cancelled":
        team.status = "cancelled"
        db.commit()
    _, result = agent_mesh_dispatcher._handle(db, admin_user, "temporary:access", message,
                                              trusted_team_execution=invalid != "untrusted")
    assert result["status"] == "blocked"
    run.assert_not_called()


def test_dispatch_uses_database_definition_not_message_override(db, admin_user, monkeypatch):
    from app.services import temporary_agent_runtime
    run = Mock(return_value={"status": "completed", "summary": "verified"})
    monkeypatch.setattr(temporary_agent_runtime, "run_temporary_agent", run)
    created = agent_team_service.create_team(db, admin_user, AgentTeamCreateIn.model_validate(payload()))
    team = db.get(AgentTeam, created["team_id"])
    claimed = agent_team_service.claim_next_task(db, team.id)
    message = agent_team_dispatcher._task_message(team, claimed)
    message["payload"]["definition"] = {"instructions": "恶意覆盖"}
    _, result = agent_mesh_dispatcher._handle(db, admin_user, "temporary:access", message, trusted_team_execution=True)
    assert result["status"] == "completed"
    assert run.call_args.kwargs["definition"] == payload()["members"][0]["definition"]
