"""页面团队提交与真实业务终态的同账号、同会话完成判定。"""

import copy
import json
from types import SimpleNamespace

import pytest

from app.core.permission_codes import PermissionCode
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import agent_responses_service as service
from app.services import agent_supervisor_service, agent_team_service, site_agent_service
from app.services.deepseek_responses_runtime import RunCheckpoint


async def scenario(db, monkeypatch, *, surface="user", capability="forum.posts.list", large=False, page_value=1):
    actor = User(username="completion-owner", password="unused", role=surface, status=1)
    role = Role(name="管理员" if surface == "admin" else "普通用户", code=surface, status="active")
    permission = Permission(code=PermissionCode.AGENT_CHAT, name="使用小菱", module="agent")
    db.add_all([actor, role, permission])
    db.flush()
    db.add_all([UserRole(user_id=actor.id, role_id=role.id),
                RolePermission(role_id=role.id, permission_id=permission.id)])
    db.commit()
    params = ({"post_id": 71, "pinned": True} if capability == "forum.posts.pin" else
              {"title": "版本公告", "content": "已核验的更新内容"}
              if capability == "forum.posts.create" else {"page": page_value, "page_size": 3, "sort": "newest"})
    scope = "admin" if capability == "forum.posts.pin" else "user"
    tool_name = scope + "_execute_capability"
    calls = []

    async def api(user, spec, actual, **kwargs):
        calls.append((user.id, spec.code, dict(actual)))
        data = ({"id": 71, "is_pinned": True, "user_id": actor.id} if capability == "forum.posts.pin" else
                {"id": 71, "title": params["title"], "content": params["content"], "user_id": actor.id}
                if capability == "forum.posts.create" else {
                    "items": [{"id": i, "title": "说明" + ("完整结果" * 10000 if large else "")}
                              for i in (3, 2, 1)], "total": 3, "page": 1, "page_size": 3,
                })
        return {"capability": spec.code, "page": spec.page, "status_code": 200, "data": data}

    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", api)
    run_id, session_key, call_id = "completion-run", "completion-session", "original-submission"
    # 两个端都可以调用用户论坛能力；scope 与 surface 是不同的边界。
    payload = site_agent_service.build_capability_team_payload(
        scope=scope, capability=capability, params=params,
        surface=surface, session_key=session_key, trace_id=run_id,
    )
    review = agent_supervisor_service.review_agent_team_plan(payload.model_dump(mode="json"))
    created = agent_team_service.create_team_from_xiaoling(
        db, actor, payload, supervisor_plan_sha256=review["plan_sha256"],
        supervisor_confirmed_by=actor.id if review["needs_confirmation"] else None,
    )
    output = {"team_id": created["team_id"], "completed": False, "verification_status": "pending"}
    arguments = {"capability": capability, "params": params}
    submit = AgentToolExecution(
        request_id=service._request_id(run_id, call_id), run_id=run_id, call_id=call_id,
        user_id=actor.id, tool_name=tool_name, status="success",
        arguments_json=json.dumps(service._persisted_tool_arguments(tool_name, arguments)),
        result_json=json.dumps({"status": "success", "output": output}),
    )
    db.add(submit)
    db.commit()
    checkpoint = RunCheckpoint(run_id=run_id, model="test", tools=[], transcript=[
        {"role": "user", "content": "查询论坛最新三条"},
        {"type": "function_call", "call_id": call_id, "name": tool_name,
         "arguments": json.dumps(arguments)},
        {"type": "function_call_output", "call_id": call_id,
         "output": json.dumps({"status": "success", "output": output})},
    ])
    for _ in range(2):
        claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
        result = await site_agent_service.execute_site_task(
            db, actor, team_id=created["team_id"], task_id=claim["task_id"], lease_token=claim["lease_token"],
        )
        assert result["status"] == "completed"
        agent_team_service.complete_task(db, created["team_id"], claim["task_id"],
                                        lease_token=claim["lease_token"], result=result, success=True)
    detail = agent_team_service.get_team(db, actor, created["team_id"])
    assert detail["status"] == "completed"
    checkpoint.transcript.extend([
        {"type": "function_call", "call_id": "read-team", "name": "get_agent_team",
         "arguments": json.dumps({"team_id": created["team_id"]})},
        {"type": "function_call_output", "call_id": "read-team",
         "output": json.dumps({"status": "success", "output": detail})},
    ])
    validator = service.AgentResponsesService(db, actor, surface=surface, session_key=session_key)
    tasks = db.query(AgentTeamTask).filter_by(team_id=created["team_id"]).all()
    return SimpleNamespace(actor=actor, validator=validator, checkpoint=checkpoint,
                           team=db.get(AgentTeam, created["team_id"]), tasks={t.task_key: t for t in tasks},
                           submit=submit, calls=calls)


@pytest.mark.parametrize("surface", ["user", "admin"])
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.asyncio
async def test_verified_original_team_releases_stale_submission_pending(db, monkeypatch, surface, large):
    case = await scenario(db, monkeypatch, surface=surface, large=large)
    original_ledger = case.submit.result_json
    for _ in range(3):
        assert await case.validator._validate_site_completion(case.checkpoint, "已查询论坛最新三条，执行完成。") is None
    assert case.submit.result_json == original_ledger  # 不改写原提交的历史事实。
    assert len(case.calls) == 2  # 完成判定只读持久证据，不再调用业务 API。


@pytest.mark.parametrize("surface", ["user", "admin"])
@pytest.mark.asyncio
async def test_verified_write_also_releases_admin_per_call_pending(db, monkeypatch, surface):
    case = await scenario(db, monkeypatch, surface=surface, capability="forum.posts.create")
    assert await case.validator._validate_site_completion(case.checkpoint, "帖子已发布成功。") is None
    assert case.calls[-1][1:] == ("forum.posts.get", {"post_id": 71})


@pytest.mark.asyncio
async def test_admin_capability_ledger_pending_uses_verified_original_team_without_rewriting(db, monkeypatch):
    case = await scenario(db, monkeypatch, surface="admin", capability="forum.posts.pin")
    original = case.submit.result_json
    assert service._ledger_admin_write_evidence(
        db, user_id=case.actor.id, checkpoint=case.checkpoint,
    ) == {"original-submission": ("forum.posts.pin", "pending")}
    for _ in range(3):
        assert await case.validator._validate_site_completion(case.checkpoint, "帖子已置顶成功。") is None
    assert case.submit.result_json == original
    case.team.status = "queued"
    db.commit()
    assert await case.validator._validate_site_completion(case.checkpoint, "帖子已置顶成功。")


@pytest.mark.parametrize("alteration", [
    "foreign_owner", "other_surface", "other_session", "other_run", "other_team",
    "queued", "failed", "partial", "execute_failed", "verifier_failed", "missing_verifier",
    "not_verifier", "untrusted_member", "wrong_operation", "wrong_dependency",
    "wrong_capability", "wrong_params", "forged_submit_args", "forged_submit_tool", "missing_submit_ledger",
    "missing_execution_ledger", "wrong_execution_owner", "wrong_execution_call", "changed_execution_result",
    "missing_receipt", "wrong_receipt_capability", "wrong_receipt_params", "wrong_receipt_request",
    "wrong_receipt_output", "pending_page_action", "wrong_page_account", "missing_readback",
    "failed_readback", "wrong_readback_capability", "changed_readback_items", "changed_readback_pagination",
    "client_observed_only",
])
@pytest.mark.asyncio
async def test_public_completed_team_does_not_override_server_evidence_boundaries(db, monkeypatch, alteration):
    case = await scenario(db, monkeypatch)
    execute, verify = case.tasks["execute"], case.tasks["verify"]
    verifier_result = json.loads(verify.result_json)
    receipt = verifier_result["result_receipt"]
    ledger = db.query(AgentToolExecution).filter_by(run_id=f"site-team-{case.team.id}").one()
    if alteration in {"foreign_owner", "other_surface", "other_session"}:
        key, value = {"foreign_owner": ("user_id", case.actor.id + 100), "other_surface": ("surface", "admin"),
                      "other_session": ("session_key", "another-session")}[alteration]
        setattr(case.team, key, value)
    elif alteration == "other_run":
        case.submit.run_id = "other-run"
    elif alteration == "other_team":
        stored = json.loads(case.submit.result_json)
        stored["output"]["team_id"] += 100
        case.submit.result_json = json.dumps(stored)
    elif alteration in {"queued", "failed", "partial"}:
        case.team.status = alteration
    elif alteration in {"execute_failed", "verifier_failed"}:
        (execute if alteration == "execute_failed" else verify).status = "failed"
    elif alteration == "missing_verifier":
        db.delete(verify)
    elif alteration in {"not_verifier", "untrusted_member"}:
        member = db.get(AgentTeamMember, verify.member_id)
        setattr(member, "role" if alteration == "not_verifier" else "address",
                "worker" if alteration == "not_verifier" else "agent:dashboard")
    elif alteration == "wrong_operation":
        verify.input_json = json.dumps({"operation": "execute_capability"})
    elif alteration == "wrong_dependency":
        verify.dependency_keys_json = json.dumps(["different-execution"])
    elif alteration in {"wrong_capability", "wrong_params"}:
        raw = json.loads(execute.input_json)
        if alteration == "wrong_capability":
            raw["capability"] = "forum.posts.get"
        else:
            raw["params"]["page_size"] = 20
        execute.input_json = json.dumps(raw)
    elif alteration == "forged_submit_args":
        case.submit.arguments_json = json.dumps({"capability": "forum.posts.list", "params": {}})
    elif alteration == "forged_submit_tool":
        case.submit.tool_name = "get_agent_team"
    elif alteration == "missing_submit_ledger":
        db.delete(case.submit)
    elif alteration == "missing_execution_ledger":
        db.delete(ledger)
    elif alteration == "wrong_execution_owner":
        ledger.user_id += 100
    elif alteration == "wrong_execution_call":
        ledger.call_id = "another-task"
    elif alteration == "changed_execution_result":
        ledger.result_json = "{}"
    elif alteration == "missing_receipt":
        verifier_result.pop("result_receipt")
        verifier_result.pop("result_receipts")
    elif alteration.startswith("wrong_receipt_"):
        field = alteration.removeprefix("wrong_receipt_")
        receipt[{"params": "request_params", "request": "request_id"}.get(field, field)] = "different"
    elif alteration == "pending_page_action":
        receipt["page_action"]["phase"] = "pending"
    elif alteration == "wrong_page_account":
        receipt["page_action"]["account_id"] += 100
    elif alteration == "missing_readback":
        receipt.pop("readback")
    elif alteration == "failed_readback":
        receipt["readback"]["status_code"] = 403
    elif alteration == "wrong_readback_capability":
        receipt["readback"]["capability"] = "forum.posts.get"
    elif alteration == "changed_readback_items":
        receipt["readback"]["data"]["items"][0]["id"] = 999
    elif alteration == "changed_readback_pagination":
        receipt["readback"]["data"]["page_size"] = 20
    elif alteration == "client_observed_only":
        verifier_result = {"status": "completed", "completed": True,
                           "verification_status": "client_observed", "server_verified": False}
    if alteration != "missing_verifier":
        verify.result_json = json.dumps(verifier_result)
    db.commit()
    # 公共 get_team 输出故意保留正常 completed 快照；只能读取服务端当前绑定证据。
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询论坛最新三条，执行完成。")


@pytest.mark.asyncio
async def test_unrelated_or_multiple_pending_calls_cannot_borrow_verified_team(db, monkeypatch):
    case = await scenario(db, monkeypatch)
    unrelated = copy.deepcopy(case.checkpoint.transcript[1:3])
    for item in unrelated:
        item["call_id"] = "unverified-second-submission"
    case.checkpoint.transcript.extend(unrelated)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，操作已完成。")


@pytest.mark.asyncio
async def test_conflicting_duplicate_submission_output_cannot_borrow_verified_call(db, monkeypatch):
    case = await scenario(db, monkeypatch)
    repeated = copy.deepcopy(case.checkpoint.transcript[2])
    output = json.loads(repeated["output"])
    output["output"]["team_id"] += 100
    repeated["output"] = json.dumps(output)
    case.checkpoint.transcript.append(repeated)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，操作已完成。")


@pytest.mark.parametrize("alteration", [
    "submit_list", "submit_null", "submit_malformed", "execution_result_list", "execution_input_list",
    "verifier_result_list", "verifier_input_null", "verifier_dependency_object", "readback_data_null",
    "receipt_output_list", "verifier_page_list", "changed_target_route", "source_pending",
    "source_failed", "source_arguments_malformed", "wrong_request_id", "team_id_bool", "team_id_string",
    "duplicate_call_different_input",
])
@pytest.mark.asyncio
async def test_malformed_or_unconfirmed_persisted_receipts_fail_closed_without_exception(db, monkeypatch, alteration):
    case = await scenario(db, monkeypatch)
    execute, verify = case.tasks["execute"], case.tasks["verify"]
    final = json.loads(verify.result_json)
    receipt = final["result_receipt"]
    ledger = db.query(AgentToolExecution).filter_by(run_id=f"site-team-{case.team.id}").one()
    if alteration in {"submit_list", "submit_null", "submit_malformed"}:
        invalid_output = {"submit_list": "[]", "submit_null": "null", "submit_malformed": "{invalid"}
        case.submit.result_json = invalid_output[alteration]
    elif alteration == "execution_result_list":
        execute.result_json = "[]"
    elif alteration == "execution_input_list":
        execute.input_json = "[]"
    elif alteration == "verifier_result_list":
        verify.result_json = "[]"
    elif alteration == "verifier_input_null":
        verify.input_json = "null"
    elif alteration == "verifier_dependency_object":
        verify.dependency_keys_json = '{"execute": true}'
    elif alteration == "readback_data_null":
        receipt["readback"]["data"] = None
    elif alteration == "receipt_output_list":
        receipt["output"] = []
    elif alteration == "verifier_page_list":
        receipt["page_action"] = []
    elif alteration == "changed_target_route":
        receipt["page_action"]["result_route"] = "/admin/security-center"
    elif alteration in {"source_pending", "source_failed"}:
        ledger.status = "executing" if alteration == "source_pending" else "failed"
    elif alteration == "source_arguments_malformed":
        ledger.arguments_json = "{invalid"
    elif alteration == "wrong_request_id":
        case.submit.request_id = "unbound-request-id"
    elif alteration in {"team_id_bool", "team_id_string"}:
        original = json.loads(case.submit.result_json)
        original["output"]["team_id"] = True if alteration == "team_id_bool" else str(case.team.id)
        case.submit.result_json = json.dumps(original)
        case.checkpoint.transcript[2]["output"] = case.submit.result_json
    elif alteration == "duplicate_call_different_input":
        repeated = copy.deepcopy(case.checkpoint.transcript[1])
        repeated["arguments"] = json.dumps({"capability": "forum.posts.list", "params": {"page_size": 20}})
        case.checkpoint.transcript.append(repeated)
    if alteration not in {"verifier_result_list", "verifier_input_null"}:
        verify.result_json = json.dumps(final)
    db.commit()
    for _ in range(3):
        assert await case.validator._validate_site_completion(case.checkpoint, "已查询论坛最新三条，执行完成。")


@pytest.mark.parametrize("surface", ["user", "admin"])
def test_business_answer_and_link_instructions_use_actual_server_result(surface):
    text = service._instructions(surface, SimpleNamespace(username="member", role=surface))
    assert "detail_route" in text
    assert "不得凭对象 ID 拼接" in text
    assert "自然业务答复默认只给结果和页面链接" in text
    assert "除非用户明确索要技术证据" in text
    assert "newest 按发布时间严格排序并忽略置顶" in text
    assert "default 才置顶优先" in text


@pytest.mark.parametrize("surface", ["user", "admin"])
@pytest.mark.asyncio
async def test_consistent_original_float_query_parameter_keeps_legitimate_http_normalization(db, monkeypatch, surface):
    case = await scenario(db, monkeypatch, surface=surface, page_value=1.0)
    raw = json.loads(case.tasks["execute"].input_json)
    assert type(raw["params"]["page"]) is float
    result = json.loads(case.tasks["verify"].result_json)
    assert type(result["result_receipt"]["output"]["data"]["page"]) is int
    assert type(result["result_receipt"]["readback"]["data"]["page"]) is int
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is None


@pytest.mark.asyncio
async def test_admin_old_pending_write_does_not_constrain_new_read_only_user_turn(db, monkeypatch):
    case = await scenario(db, monkeypatch, surface="admin", capability="forum.posts.pin")
    assert await case.validator._validate_site_completion(case.checkpoint, "帖子已置顶成功。") is None
    case.checkpoint.transcript.append({"role": "user", "content": "更正：本轮只读取刚才团队的状态，不进行写入。"})
    assert service._verified_site_submission_calls(
        db, user_id=case.actor.id, surface="admin", session_key=case.validator._session_key,
        checkpoint=case.checkpoint,
    ) == frozenset()
    assert service._admin_write_calls(case.checkpoint.transcript) == {}
    assert await case.validator._validate_site_completion(case.checkpoint, "本轮只读查询已完成。") is None


@pytest.mark.parametrize("field,value", [("execute_page", True), ("execute_page", 1.0),
                                        ("readback_page", True), ("readback_item", True)])
@pytest.mark.asyncio
async def test_exact_json_types_cannot_drift_from_original_task_or_readback(db, monkeypatch, field, value):
    case = await scenario(db, monkeypatch)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。") is None
    if field == "execute_page":
        execute = case.tasks["execute"]
        raw = json.loads(execute.input_json)
        raw["params"]["page"] = value
        execute.input_json = json.dumps(raw)
    else:
        verify = case.tasks["verify"]
        result = json.loads(verify.result_json)
        snapshot = result["result_receipt"]["readback"]["data"]
        if field == "readback_page":
            snapshot["page"] = value
        else:
            snapshot["items"][-1]["id"] = value
        verify.result_json = json.dumps(result)
    db.commit()
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。")


@pytest.mark.asyncio
async def test_public_completed_projection_does_not_override_original_pending_server_ledger(db, monkeypatch):
    case = await scenario(db, monkeypatch)
    case.team.status = "queued"
    db.commit()
    public = json.loads(case.checkpoint.transcript[2]["output"])
    public["output"].update(completed=True, verification_status="completed", status="completed")
    case.checkpoint.transcript[2]["output"] = json.dumps(public)
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。")


def test_negated_write_clause_does_not_hide_other_requested_mutation():
    assert not service._requests_admin_mutation([{"role": "user", "content": "只读取团队状态，不进行写入。"}])
    assert service._requests_admin_mutation([{"role": "user", "content": "先查询，不进行写入，但请删除帖子。"}])


@pytest.mark.parametrize("text", ["任务没有失败，帖子已发布成功。", "不能说操作失败，帖子已经发布。",
                                  "本次未失败，操作已完成。"])
@pytest.mark.asyncio
async def test_negated_failure_phrase_cannot_suppress_real_completion_proof(db, monkeypatch, text):
    case = await scenario(db, monkeypatch, capability="forum.posts.create")
    assert service._claims_mutation_failure(text) is False
    assert service._claims_mutation_success(text) is True
    assert await case.validator._validate_site_completion(case.checkpoint, text) is None
    case.team.status = "queued"
    db.commit()
    assert await case.validator._validate_site_completion(case.checkpoint, text)


@pytest.mark.parametrize("boundary", ["valid", "no_run", "foreign_owner", "other_surface", "other_session"])
@pytest.mark.asyncio
async def test_direct_synchronous_capability_receipt_keeps_owned_run_context(db, boundary):
    user = User(username="direct-receipt-owner", password="unused", role="user", status=1)
    db.add(user)
    db.flush()
    checkpoint = RunCheckpoint(run_id="direct-run", model="test", tools=[], transcript=[
        {"role": "user", "content": "查询我的个人画像"},
        {"type": "function_call", "name": "user_execute_capability", "call_id": "direct-read",
         "arguments": json.dumps({"capability": "profile.get", "params": {}})},
        {"type": "function_call_output", "call_id": "direct-read", "output": json.dumps({
            "status": "success", "output": {"capability": "profile.get", "status_code": 200,
                                               "data": {"display_name": "当前账号"}},
        })},
    ])
    db.add(AgentToolExecution(
        request_id=service._request_id(checkpoint.run_id, "direct-read"), user_id=user.id,
        run_id=checkpoint.run_id, call_id="direct-read", tool_name="user_execute_capability", status="success",
        arguments_json=checkpoint.transcript[1]["arguments"], result_json=checkpoint.transcript[2]["output"],
    ))
    if boundary != "no_run":
        db.add(AgentResponseRun(
            run_id=checkpoint.run_id, user_id=user.id + 100 if boundary == "foreign_owner" else user.id,
            surface="admin" if boundary == "other_surface" else "user",
            session_key="different-session" if boundary == "other_session" else "direct-session",
            checkpoint_json="{}",
        ))
    db.commit()
    validator = service.AgentResponsesService(db, user, surface="user", session_key="direct-session")
    reason = await validator._validate_site_completion(checkpoint, "已查询，执行完成。")
    assert (reason is None) is (boundary == "valid")


@pytest.mark.asyncio
async def test_non_string_public_capability_is_rejected_without_lookup_exception(db, monkeypatch):
    case = await scenario(db, monkeypatch)
    case.checkpoint.transcript[1]["arguments"] = json.dumps({"capability": [], "params": {}})
    assert await case.validator._validate_site_completion(case.checkpoint, "已查询，执行完成。")
