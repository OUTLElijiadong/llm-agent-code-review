"""Forum membership and browser receipt races on a dedicated disposable MySQL server.

The fresh-chain fixture creates and drops a fixed ``code_review`` database. Never
share its MySQL server with other running test jobs or with non-test data.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _seed

from app.core.exceptions import ForbiddenError
from app.models.agent_browser_action import AgentBrowserAction
from app.models.agent_response_run import AgentResponseRun
from app.models.agent_team import AgentTeamTask
from app.models.enterprise_org import EnterpriseMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.user import User
from app.schemas.agent_browser import BrowserAckIn
from app.schemas.agent_team import AgentTeamCreateIn
from app.services import agent_browser_service as browser
from app.services import agent_supervisor_service, agent_team_service, site_agent_service
from app.services.ai_usage_context import run_attribution, usage_context

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _prepare(factory):
    company, _, _, users, _, _ = _seed(factory)
    actor_id = users[0]
    with factory.begin() as db:
        membership = db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=actor_id).one()
        membership.status = "inactive"
        role = db.query(Role).filter_by(code="user", status="active").one()
        permission = db.query(Permission).filter_by(code="agent:chat").one()
        if not db.query(UserRole).filter_by(user_id=actor_id, role_id=role.id).first():
            db.add(UserRole(user_id=actor_id, role_id=role.id))
        if not db.query(RolePermission).filter_by(role_id=role.id, permission_id=permission.id).first():
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    return company, actor_id


def test_forum_membership_current_read_rejects_old_repeatable_read_snapshot(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, actor_id = _prepare(factory)
    with factory() as stale:
        actor = stale.get(User, actor_id)
        # Establish a MySQL REPEATABLE READ snapshot with an inactive member.
        membership = stale.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=actor_id).one()
        assert membership.status == "inactive"
        with factory.begin() as writer:
            writer.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=actor_id).one().status = "active"
        with pytest.raises(ForbiddenError, match="企业成员"):
            browser.authorize_route(stale, actor, "/forum")
        stale.rollback()


def test_forum_shared_membership_check_serializes_enterprise_activation(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, actor_id = _prepare(factory)
    checked, allow_commit, write_started, write_done = Event(), Event(), Event(), Event()

    def check_forum():
        with factory.begin() as db:
            assert browser.authorize_route(db, db.get(User, actor_id), "/forum") == "/forum"
            checked.set()
            assert allow_commit.wait(15)

    def activate():
        with factory.begin() as db:
            row = db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=actor_id).one()
            row.status = "active"
            write_started.set()
            db.flush()
        write_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(check_forum)
        try:
            assert checked.wait(15)
            writer = pool.submit(activate)
            assert write_started.wait(15)
            assert not write_done.wait(.25), "membership activation bypassed the held current-read lock"
        finally:
            allow_commit.set()
        reader.result(timeout=15)
        writer.result(timeout=15)
    with factory() as db:
        with pytest.raises(ForbiddenError, match="企业成员"):
            browser.authorize_route(db, db.get(User, actor_id), "/forum")


def test_site_operator_forum_ack_rechecks_membership_after_claim(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, actor_id = _prepare(factory)
    run_id = f"forum-run-{uuid4().hex}"
    session_id = f"forum-session-{uuid4().hex}"
    with factory.begin() as db:
        db.add(AgentResponseRun(run_id=run_id, user_id=actor_id, surface="user", session_key=session_id,
                                status="running", checkpoint_json="{}"))
    plan = AgentTeamCreateIn.model_validate({
        "surface": "user", "session_id": session_id, "title": "观察论坛", "objective": "观察当前页面",
        "members": [
            {"member_key": "operator", "display_name": "页面观察", "address": "agent:site_operator", "role": "worker"},
            {"member_key": "verifier", "display_name": "观察核验", "address": "agent:site_operator",
             "role": "verifier"},
        ],
        "tasks": [
            {"task_key": "observe", "member_key": "operator", "title": "观察页面", "instructions": "读取页面",
             "input": {"operation": "operate_current_page", "action": "inspect", "args": {}}, "max_attempts": 1},
            {"task_key": "verify", "member_key": "verifier", "title": "核对回执", "instructions": "核验观察",
             "input": {"operation": "verify_results"}, "depends_on": ["observe"], "max_attempts": 1},
        ], "max_attempts": 1,
    })
    with factory() as db:
        actor = db.get(User, actor_id)
        review = agent_supervisor_service.review_agent_team_plan(plan.model_dump(mode="json"))
        with usage_context(actor_id, run_attribution(db, actor_id, run_id)):
            team = agent_team_service.create_team_from_xiaoling(
                db, actor, plan, supervisor_plan_sha256=review["plan_sha256"],
            )
        # MySQL DATETIME rounds fractional seconds; the freshly queued task may
        # become eligible in the next second, as in the normal polling dispatcher.
        deadline = monotonic() + 3
        lease = None
        while lease is None and monotonic() < deadline:
            lease = agent_team_service.claim_next_task(db, team["team_id"], lease_seconds=60)
            if lease is None:
                sleep(.05)
        if not lease:
            rows = db.query(AgentTeamTask).filter_by(team_id=team["team_id"]).all()
            assert lease, [(row.task_key, row.status, str(row.next_attempt_at), str(agent_team_service._now()))
                           for row in rows]
    assert lease["lease_token"]

    def worker():
        with factory() as db:
            actor = db.get(User, actor_id)
            return asyncio.run(site_agent_service.execute_site_task(
                db, actor, team_id=team["team_id"], task_id=lease["task_id"], lease_token=lease["lease_token"],
            ))

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(worker)
        try:
            with factory() as client:
                actor = client.get(User, actor_id)
                deadline = monotonic() + 15
                pending = []
                while not pending and monotonic() < deadline:
                    client.rollback()
                    pending = browser.pending_actions(client, actor, run_id=run_id,
                                                      session_key=session_id, surface="user")["actions"]
                    if not pending:
                        sleep(.05)
                assert pending, "site_operator did not publish a browser action"
                event = pending[0]
                claim = browser.claim_action(client, actor, event["action_id"], session_key=session_id,
                                             surface="user", nonce=event["client_nonce"])
                # Start an old snapshot before activation; ack must use the current membership read.
                assert client.query(EnterpriseMember).filter_by(
                    enterprise_id=company, user_id=actor_id,
                ).one().status == "inactive"
                with factory.begin() as writer:
                    writer.query(EnterpriseMember).filter_by(
                        enterprise_id=company, user_id=actor_id,
                    ).one().status = "active"
                receipt = BrowserAckIn.model_validate({
                    "session_id": session_id, "surface": "user", "client_nonce": event["client_nonce"],
                    "claim_token": claim["claim_token"], "outcome": "completed", "observation": {
                        "snapshot_id": event["observation_snapshot_id"], "route": "/forum", "title": "论坛",
                        "controls": [], "messages": ["论坛内容不得回传"],
                    },
                })
                with pytest.raises(ForbiddenError, match="企业成员"):
                    browser.acknowledge_action(client, actor, event["action_id"], receipt)
                client.rollback()
                row = client.get(AgentBrowserAction, event["action_id"], populate_existing=True)
                assert row.observation_json is None
                client.rollback()
        finally:
            with factory() as db:
                agent_team_service.cancel_team(db, db.get(User, actor_id), team["team_id"], reason="测试清理")
            result = task.result(timeout=15)
            assert result["status"] == "blocked"
