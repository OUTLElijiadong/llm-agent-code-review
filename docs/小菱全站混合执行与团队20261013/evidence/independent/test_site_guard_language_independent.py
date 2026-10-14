"""有限语料独立增样：否定失败不得掩盖真实完成声明。"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.services import agent_responses_service as service


path = Path(__file__).with_name("test_site_guard_independent.py")
spec = importlib.util.spec_from_file_location("independent_site_guard_base", path)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
db, scenario = base.db, base.scenario

SUCCESS = "任务没有失败，操作已完成。"
FAILURE = "任务执行失败，未能完成业务操作。"
WAITING = "已提交任务，尚未完成，等待业务核验。"


def browser_checkpoint(case):
    checkpoint = copy.deepcopy(case.checkpoint)
    checkpoint.transcript = [
        {"role": "user", "content": "点击页面发布按钮后如实说明实际观察。"},
        {"type": "function_call", "call_id": "observe-click", "name": "operate_current_page",
         "arguments": json.dumps({"action": "click", "args": {"control_handle": "local-fixture"}})},
        {"type": "function_call_output", "call_id": "observe-click", "output": json.dumps({
            "status": "success", "output": {"status": "completed", "completed": True,
            "verification_status": "client_observed", "server_verified": False},
        })},
    ]
    return checkpoint


@pytest.mark.parametrize("branch", ["queued", "client_observed"])
@pytest.mark.asyncio
async def test_negated_failure_still_denies_unverified_completion(db, monkeypatch, branch):
    case = await scenario(db, monkeypatch, capability="forum.posts.create")
    assert await case.validator._validate_site_completion(case.checkpoint, "帖子已发布成功。") is None
    checkpoint = case.checkpoint
    if branch == "queued":
        case.team.status = "queued"
        db.commit()
    else:
        checkpoint = browser_checkpoint(case)
    assert await case.validator._validate_site_completion(checkpoint, SUCCESS) is not None


@pytest.mark.asyncio
async def test_negated_failure_does_not_block_verified_admin_completion(db, monkeypatch):
    case = await scenario(db, monkeypatch, surface="admin", capability="forum.posts.pin")
    assert await case.validator._validate_site_completion(case.checkpoint, "帖子已置顶成功。") is None
    assert await case.validator._validate_site_completion(case.checkpoint, SUCCESS) is None


@pytest.mark.parametrize("text,success,failure", [
    (SUCCESS, True, False), (FAILURE, False, True), (WAITING, False, False),
])
def test_three_language_categories_are_distinguished(text, success, failure):
    assert service._claims_mutation_success(text) is success
    assert service._claims_mutation_failure(text) is failure


@pytest.mark.parametrize("branch", ["queued", "client_observed"])
@pytest.mark.asyncio
async def test_honest_waiting_and_observation_remain_allowed(db, monkeypatch, branch):
    case = await scenario(db, monkeypatch, capability="forum.posts.create")
    checkpoint = case.checkpoint
    if branch == "queued":
        case.team.status = "queued"
        db.commit()
    else:
        checkpoint = browser_checkpoint(case)
    assert await case.validator._validate_site_completion(checkpoint, WAITING) is None


@pytest.mark.asyncio
async def test_consistent_persisted_admin_failure_remains_allowed(db, monkeypatch):
    case = await scenario(db, monkeypatch, surface="admin", capability="forum.posts.pin")
    case.team.status = "failed"
    for task in case.tasks.values():
        task.status = "failed"
    failed = {"status": "error", "message": "本地失败对照", "output": {"status": "failed"}}
    case.submit.status = "failed"
    case.submit.result_json = json.dumps(failed)
    case.checkpoint.transcript[2]["output"] = json.dumps(failed)
    db.commit()
    assert await case.validator._validate_site_completion(case.checkpoint, FAILURE) is None
