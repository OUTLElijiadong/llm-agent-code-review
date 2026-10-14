"""站点子 Agent 的真实契约、账号/租约、审批和不重复写入边界。"""

import asyncio
import json

import pytest

from app.core.permission_codes import PermissionCode
from app.models.agent_team import AgentTeamEvent, AgentTeamTask
from app.models.agent_response_run import AgentToolExecution
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.services import agent_supervisor_service, agent_team_service, site_agent_service


@pytest.fixture
def actor(db):
    user = User(username="site-agent-owner", password="unused", role="user", status=1)
    role = Role(name="普通用户", code="user", status="active")
    permission = Permission(code=PermissionCode.AGENT_CHAT, name="使用小菱", module="agent")
    db.add_all([user, role, permission])
    db.flush()
    db.add_all([UserRole(user_id=user.id, role_id=role.id),
                RolePermission(role_id=role.id, permission_id=permission.id)])
    db.commit()
    return user


def claimed_team(db, actor, *, capability="forum.posts.create", params=None, confirmed=False):
    payload = site_agent_service.build_capability_team_payload(
        scope="user", capability=capability, params=params or {"title": "实际公告", "content": "版本说明"},
        surface="user", session_key="site-session", trace_id="site-trace",
    )
    review = agent_supervisor_service.review_agent_team_plan(payload.model_dump(mode="json"))
    created = agent_team_service.create_team_from_xiaoling(
        db, actor, payload,
        supervisor_plan_sha256=review["plan_sha256"] if confirmed else "",
        supervisor_confirmed_by=actor.id if confirmed else None,
    )
    claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    return created, claim


def run(db, actor, created, claim):
    return asyncio.run(site_agent_service.execute_site_task(
        db, actor, team_id=created["team_id"], task_id=claim["task_id"],
        lease_token=claim["lease_token"],
    ))


def test_helper_binds_worker_verifier_and_single_mutation_attempt():
    payload = site_agent_service.build_capability_team_payload(
        scope="user", capability="forum.posts.create", params={"title": "公告", "content": "说明"},
        surface="user", session_key="current-account", trace_id="trace-current",
    )
    assert [item.address for item in payload.members] == ["agent:site_operator"] * 2
    assert payload.members[1].role == "verifier"
    assert payload.tasks[1].depends_on == ["execute"]
    assert payload.tasks[0].input["scope"] == "user"
    assert payload.max_attempts == payload.tasks[0].max_attempts == 1


def test_ordinary_write_executes_once_and_independent_verifier_reads_back(db, actor, monkeypatch):
    calls = []

    async def api(user, spec, params, **kwargs):
        calls.append((user.id, spec.code, params))
        data = {"id": 71} if spec.code == "forum.posts.create" else {
            "id": 71, "title": "实际公告", "content": "版本说明", "user_id": actor.id,
        }
        return {"capability": spec.code, "page": spec.page, "status_code": 200, "data": data}

    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", api)
    created, claim = claimed_team(db, actor)
    one = run(db, actor, created, claim)
    again = run(db, actor, created, claim)
    assert one["status"] == again["status"] == "completed"
    assert one["result_receipt"]["page_action"]["phase"] == "pending"
    assert sum(code == "forum.posts.create" for _, code, _ in calls) == 1
    agent_team_service.complete_task(db, created["team_id"], claim["task_id"],
                                    lease_token=claim["lease_token"], result=one, success=True)
    verifier = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    verified = run(db, actor, created, verifier)
    assert verified["status"] == "completed"
    assert verified["result_receipt"]["page_action"] == {
        "page": "/forum", "capability": "forum.posts.create", "phase": "completed",
        "mutation": True, "result_route": "/forum/71", "account_id": actor.id,
    }
    assert calls[-1][1:] == ("forum.posts.get", {"post_id": 71})


def test_cross_account_and_cancelled_lease_block_before_api(db, actor, monkeypatch):
    called = []
    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", lambda *_a, **_k: called.append(True))
    created, claim = claimed_team(db, actor)
    other = User(username="other-site-user", password="unused", role="user", status=1)
    db.add(other)
    db.commit()
    assert run(db, other, created, claim)["status"] == "blocked"
    agent_team_service.cancel_team(db, actor, created["team_id"])
    assert run(db, actor, created, claim)["status"] == "blocked"
    assert called == []


def test_revocation_after_dispatch_blocks_before_api(db, actor, monkeypatch):
    called = []
    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", lambda *_a, **_k: called.append(True))
    created, claim = claimed_team(db, actor)
    actor.status = 0
    db.commit()
    assert run(db, actor, created, claim)["status"] == "blocked"
    assert called == []


def test_high_risk_plan_requires_bound_current_account_confirmation(db, actor):
    with pytest.raises(agent_team_service.AgentTeamAccessError, match="确认"):
        claimed_team(db, actor, capability="forum.posts.delete", params={"post_id": 71})
    created, _ = claimed_team(db, actor, capability="forum.posts.delete", params={"post_id": 71}, confirmed=True)
    row = db.query(AgentTeamEvent).filter_by(team_id=created["team_id"], event_type="supervisor.plan_reviewed").one()
    assert json.loads(row.detail_json)["confirmed_by_user_id"] == actor.id


def test_failed_or_unknown_write_is_not_replayed(db, actor, monkeypatch):
    calls = []

    async def broken(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("response lost after write")

    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", broken)
    created, claim = claimed_team(db, actor)
    one, two = run(db, actor, created, claim), run(db, actor, created, claim)
    assert one["status"] == two["status"] == "failed"
    assert one["retryable"] is False and len(calls) == 1


def test_async_acceptance_is_not_business_completion(db, actor, monkeypatch):
    async def accepted(user, spec, params, **kwargs):
        return {"capability": spec.code, "status_code": 202, "data": {"id": 71, "status": "queued"}}

    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", accepted)
    created, claim = claimed_team(db, actor)
    result = run(db, actor, created, claim)
    assert result["status"] == "blocked" and result["retryable"] is False
    assert result["result_receipt"]["page_action"]["phase"] == "pending"


def test_unregistered_contracts_and_model_approval_flags_are_rejected():
    with pytest.raises(ValueError):
        site_agent_service.resolve_site_input({"operation": "execute_capability", "scope": "user",
                                               "capability": "any.http", "params": {}})
    with pytest.raises(ValueError):
        site_agent_service.resolve_site_input({"operation": "execute_capability", "scope": "user",
                                               "capability": "forum.posts.create", "params": {}, "approved": True})


@pytest.mark.parametrize("capability", ["users.reset_password", "beta_codes.generate", "llm.config.update"])
def test_sensitive_capabilities_stay_in_main_session(capability):
    with pytest.raises(ValueError, match="主会话"):
        site_agent_service.resolve_site_input({"operation": "execute_capability", "scope": "admin",
                                               "capability": capability, "params": {}})


def test_verifier_does_not_trust_payload_dependency_context(db, actor):
    created, claim = claimed_team(db, actor)
    task = db.get(AgentTeamTask, claim["task_id"])
    task.input_json = json.dumps({"operation": "verify_results"})
    task.dependency_keys_json = json.dumps(["fake-source"])
    db.commit()
    result = run(db, actor, created, claim)
    assert result["status"] == "blocked"
    assert db.query(AgentToolExecution).count() == 0


def test_small_business_receipt_retains_item_identity_in_team_projection(db, actor, monkeypatch):
    async def api(_user, spec, _params, **_kwargs):
        return {"capability": spec.code, "status_code": 200, "data": {
            "total": 3, "page": 1, "page_size": 3,
            "items": [{"id": i, "title": f"版本说明 {i}", "create_time": "2026-10-14T00:00:00",
                       "api_key": "sk-projection-secret-value"} for i in range(1, 4)],
        }}
    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", api)
    created, claim = claimed_team(db, actor, capability="forum.posts.list", params={"page": 1, "page_size": 3})
    result = run(db, actor, created, claim)
    agent_team_service.complete_task(db, created["team_id"], claim["task_id"],
                                    lease_token=claim["lease_token"], result=result, success=True)
    detail = agent_team_service.get_team(db, actor, created["team_id"])
    projected = next(task for task in detail["tasks"] if task["task_id"] == claim["task_id"])["result"]
    items = projected["result_receipt"]["output"]["data"]["items"]
    assert [item["title"] for item in items] == ["版本说明 1", "版本说明 2", "版本说明 3"]
    assert [item["id"] for item in items] == [1, 2, 3]
    assert all(item["create_time"] == "2026-10-14T00:00:00" for item in items)
    assert "sk-projection-secret-value" not in json.dumps(projected)
    verifier = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    verified = run(db, actor, created, verifier)
    agent_team_service.complete_task(db, created["team_id"], verifier["task_id"],
                                    lease_token=verifier["lease_token"], result=verified, success=True)
    final = agent_team_service.get_team(db, actor, created["team_id"])
    final_items = final["summary"]["final_result"]["result_receipt"]["output"]["data"]["items"]
    assert [item["title"] for item in final_items] == ["版本说明 1", "版本说明 2", "版本说明 3"]


def test_get_verification_keeps_query_filter_and_pagination(db, actor, monkeypatch):
    calls = []
    params = {"page": 2, "page_size": 3, "keyword": "版本", "category": "announce"}
    async def api(_user, spec, actual, **_kwargs):
        calls.append(dict(actual))
        return {"capability": spec.code, "status_code": 200, "data": {
            "total": 6, "page": actual.get("page", 1), "page_size": actual.get("page_size", 20),
            "items": [{"id": 4, "title": "版本说明"}],
        }}
    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", api)
    created, claim = claimed_team(db, actor, capability="forum.posts.list", params=params)
    result = run(db, actor, created, claim)
    agent_team_service.complete_task(db, created["team_id"], claim["task_id"],
                                    lease_token=claim["lease_token"], result=result, success=True)
    verifier = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    verified = run(db, actor, created, verifier)
    assert verified["status"] == "completed"
    assert calls == [params, params]


def test_get_verifier_blocks_changed_item_despite_same_total(db, actor, monkeypatch):
    calls = 0
    async def api(_user, spec, _params, **_kwargs):
        nonlocal calls
        calls += 1
        return {"capability": spec.code, "status_code": 200, "data": {
            "total": 3, "page": 1, "page_size": 3,
            "items": [{"id": 1, "title": "版本说明" if calls == 1 else "已被更改"}],
        }}
    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", api)
    created, claim = claimed_team(db, actor, capability="forum.posts.list", params={"page": 1, "page_size": 3})
    result = run(db, actor, created, claim)
    agent_team_service.complete_task(db, created["team_id"], claim["task_id"],
                                    lease_token=claim["lease_token"], result=result, success=True)
    verifier = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    verified = run(db, actor, created, verifier)
    assert verified["status"] == "blocked"
    assert "快照" in verified["summary"] or "条目" in verified["summary"]
