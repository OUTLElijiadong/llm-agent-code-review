"""独立只读复核：真实 SQLite 团队流程后的轮次及 JSON 类型完整性。"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.models.agent_team import AgentTeamMember
from app.services import agent_responses_service as service
from app.services import agent_team_service


REPO = Path(__file__).resolve().parents[4]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _load("independent_site_fixtures", REPO / "backend/tests/conftest.py")
db = fixtures.db
scenario = _load(
    "independent_site_scenario", REPO / "backend/tests/unit/services/test_site_completion_guard_recheck.py",
).scenario


@pytest.mark.asyncio
async def test_admin_new_user_read_turn_does_not_inherit_old_pending_write(db, monkeypatch):
    case = await scenario(db, monkeypatch, surface="admin", capability="forum.posts.pin")
    assert await case.validator._validate_site_completion(case.checkpoint, "帖子已置顶成功。") is None
    case.checkpoint.transcript.append({"role": "user", "content": "更正：本轮只读取刚才团队的状态，不进行写入。"})
    detail = agent_team_service.get_team(db, case.actor, case.team.id)
    case.checkpoint.transcript.extend([
        {"type": "function_call", "call_id": "new-turn-read", "name": "get_agent_team",
         "arguments": json.dumps({"team_id": case.team.id})},
        {"type": "function_call_output", "call_id": "new-turn-read",
         "output": json.dumps({"status": "success", "output": detail})},
    ])
    verified = service._verified_site_submission_calls(
        db, user_id=case.actor.id, surface="admin", session_key=case.validator._session_key,
        checkpoint=case.checkpoint,
    )
    assert verified == frozenset()  # 当前轮次不能借用旧轮次提交。
    assert await case.validator._validate_site_completion(case.checkpoint, "本轮只读查询已完成。") is None
    assert len(case.calls) == 2


@pytest.mark.parametrize("changed_page", [True, 1.0])
@pytest.mark.asyncio
async def test_fixed_execute_input_preserves_exact_json_parameter_type(db, monkeypatch, changed_page):
    case = await scenario(db, monkeypatch)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is None
    execute = case.tasks["execute"]
    raw = json.loads(execute.input_json)
    assert raw["params"]["page"] == 1 and type(raw["params"]["page"]) is int
    raw["params"]["page"] = changed_page
    execute.input_json = json.dumps(raw)
    db.commit()
    # 不修改原执行账本和回执；持久任务输入已经与它们的 JSON 类型不一致。
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is not None


@pytest.mark.parametrize("field", ["page", "item_id"])
@pytest.mark.asyncio
async def test_readback_snapshot_does_not_equate_boolean_with_integer(db, monkeypatch, field):
    case = await scenario(db, monkeypatch)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is None
    verify = case.tasks["verify"]
    final = json.loads(verify.result_json)
    snapshot = final["result_receipt"]["readback"]["data"]
    if field == "page":
        snapshot["page"] = True
    else:
        assert snapshot["items"][-1]["id"] == 1
        snapshot["items"][-1]["id"] = True
    verify.result_json = json.dumps(final)
    db.commit()
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is not None


@pytest.mark.asyncio
async def test_execute_member_must_be_worker_even_if_public_team_is_completed(db, monkeypatch):
    case = await scenario(db, monkeypatch)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is None
    member = db.get(AgentTeamMember, case.tasks["execute"].member_id)
    member.role = "verifier"
    db.commit()
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is not None


@pytest.mark.asyncio
async def test_public_completed_submission_cannot_override_queued_persisted_team(db, monkeypatch):
    case = await scenario(db, monkeypatch)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is None
    case.team.status = "queued"
    db.commit()
    # 验证边界是“不一致公开 checkpoint”，不是声称生产模型可注入平台工具输出。
    public = copy.deepcopy(case.checkpoint.transcript[2])
    value = json.loads(public["output"])
    value["output"].update(completed=True, verification_status="completed", status="completed")
    public["output"] = json.dumps(value)
    case.checkpoint.transcript[2] = public
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is not None
