"""Overlapping authority snapshots must not deadlock on lock upgrades."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _ready, _seed

from app.models.enterprise_agent_team import EnterpriseAgentTeam, EnterpriseAgentTeamApproval, EnterpriseAgentTeamTask
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_assistant_runtime_service as parent

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize('billing_version', [0, 1])
@pytest.mark.parametrize('requires_approval', [False, True])
def test_team_overlapping_real_snapshots_do_not_deadlock_on_membership_upgrade(
    fresh_chain, monkeypatch, billing_version, requires_approval,
):
    real_snapshot = parent._live_role_snapshot
    _ready(monkeypatch)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, _group, _owner, users, tasks, teams = _seed(factory, first_user_enterprise_role='admin')
    # A is admin (rank 3), B is member (rank 1): B's later message is not a
    # same/higher directive that should invalidate A's run.
    with factory.begin() as db:
        team_rows = [db.get(EnterpriseAgentTeam, item) for item in teams]
        runs = [db.get(EnterpriseAssistantRun, team.assistant_run_id) for team in team_rows]
        room = db.get(EnterpriseConversation, runs[0].conversation_id)
        messages = [db.get(EnterpriseChatMessage, run.source_message_id) for run in runs]
        assert messages[0].id < messages[1].id
        for team, run in zip(team_rows, runs):
            if billing_version == 0:
                run.billing_attribution_version = 0
                run.billing_group_id = None
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
            snapshot = parent._role_snapshot(db, enterprise_id=company, room=room,
                                             messages=messages, initiator_id=run.initiator_user_id)
            state.permission_snapshot_json = parent._json(snapshot)
            state.permission_snapshot_sha256 = parent._digest(snapshot)
            team.permission_snapshot_json = state.permission_snapshot_json
            team.permission_snapshot_sha256 = state.permission_snapshot_sha256
        if requires_approval:
            team = team_rows[1]
            task = db.get(EnterpriseAgentTeamTask, tasks[1])
            plan = json.loads(team.plan_snapshot_json)
            plan.update({'risk_level': 'high', 'requires_approval': True})
            team.plan_snapshot_json = runtime._canonical_json(plan)
            team.plan_sha256 = runtime._digest(plan)
            task.approval_sha256 = team.plan_sha256
            task.approved_by_user_id = users[0]
            db.add(EnterpriseAgentTeamApproval(
                team_id=team.id, task_id=task.id, enterprise_id=company, conversation_id=team.conversation_id,
                group_id=team.group_id, initiator_user_id=team.initiator_user_id, approval_key='overlap-approval',
                plan_sha256=team.plan_sha256, required_permission_code='group_manager', status='approved',
                plan_json=team.plan_snapshot_json, requested_by_user_id=users[1], decided_by_user_id=users[0],
            ))
    barrier = Barrier(2, timeout=10)
    approved_barrier = Barrier(2, timeout=10)

    def synchronized_snapshot(db, state, **kwargs):
        result = real_snapshot(db, state, **kwargs)
        barrier.wait()
        return result

    monkeypatch.setattr(parent, '_live_role_snapshot', synchronized_snapshot)

    def worker(task_id):
        with factory.begin() as db:
            runtime._provider_transaction_read_committed(db)
            context = runtime._lock_live_context(db, enterprise_id=company, task_id=task_id, lock_company=False)
            runtime._verify_approval_for_live_context(db, context)
            approved_barrier.wait()
            return context['task'].id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, tasks))
    assert results == tasks
