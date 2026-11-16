"""Real MySQL registered-agent send locks remain team scoped."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _install_live_snapshots, _ready, _seed

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_agent_team import EnterpriseAgentTeam, EnterpriseAgentTeamMember, EnterpriseAgentTeamTask
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
from app.services import billing_ledger_service as ledger
from app.services import billing_metered_call, billing_runtime_gate, enterprise_org_service
from app.services import enterprise_agent_team_dispatcher as dispatcher
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_agent_team_service as team_service
from app.services import enterprise_assistant_runtime_service as parent_runtime

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


def _add_team_in_another_group(factory, company, owner, user):
    suffix = uuid4().hex[:12]
    empty = {'messages': []}
    with factory.begin() as db:
        group = EnterpriseGroup(enterprise_id=company, name=f'other-{suffix}', status='active')
        db.add(group)
        db.flush()
        db.add(EnterpriseGroupMember(enterprise_id=company, group_id=group.id, user_id=user,
                                     role='member', status='active'))
        room = EnterpriseConversation(enterprise_id=company, kind='group', conversation_key=f'group:{group.id}',
                                      group_id=group.id, created_by=owner)
        db.add(room)
        db.flush()
        message = EnterpriseChatMessage(enterprise_id=company, conversation_id=room.id, sender_id=user,
                                        sender_type='human', client_message_id=f'other-{suffix}',
                                        content='@小菱 执行任务', sender_authority_rank=1)
        db.add(message)
        db.flush()
        run = EnterpriseAssistantRun(public_id=f'other-{suffix}', request_key=f'other-{suffix}',
            enterprise_id=company, conversation_id=room.id, group_id=group.id, initiator_user_id=user,
            source_message_id=message.id, status='completed', attempt_count=1, prompt_sha256='p' * 64)
        db.add(run)
        db.flush()
        db.add(EnterpriseAssistantRuntimeState(assistant_run_id=run.id, enterprise_id=company,
            conversation_id=room.id, group_id=group.id, initiator_user_id=user, source_message_id=message.id,
            status='completed', checkpoint_json='{}', permission_snapshot_json=runtime._canonical_json(empty),
            permission_snapshot_sha256=runtime._digest(empty), stop_requested=False, lease_generation=1,
            billing_invocation_count=0, version=1))
        team = EnterpriseAgentTeam(assistant_run_id=run.id, enterprise_id=company, conversation_id=room.id,
            group_id=group.id, initiator_user_id=user, source_message_id=message.id,
            source_tool_call_id=f'other-{suffix}', title='另组任务', objective='并发验证', status='queued',
            payer_owner_type='enterprise', payer_owner_id=company, trace_id=f'other-{suffix}',
            request_key_prefix=f'other-{suffix}', parent_lease_generation=1,
            plan_sha256=runtime._digest({}), plan_snapshot_json='{}', permission_snapshot_sha256=runtime._digest(empty),
            permission_snapshot_json=runtime._canonical_json(empty))
        db.add(team)
        db.flush()
        member = EnterpriseAgentTeamMember(team_id=team.id, member_key='worker', display_name='worker',
            address='agent:worker', kind='registered', role='worker', snapshot_sha256='a' * 64,
            capabilities_json='{}', status='created')
        db.add(member)
        db.flush()
        task = EnterpriseAgentTeamTask(team_id=team.id, member_id=member.id, task_key='work', title='任务',
            instructions='只读', status='queued', attempt_count=0, max_attempts=3, lease_generation=0)
        db.add(task)
        db.flush()
        return int(group.id), int(team.id), int(task.id)


def test_registered_teams_send_concurrently_without_losing_ledger_isolation(
    fresh_chain, verified_model_catalog, monkeypatch,
):
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    monkeypatch.setattr(parent_runtime, '_assert_no_newer_authority_directive', lambda *_a, **_kw: None)
    isolation_levels = []
    set_isolation = runtime._provider_transaction_read_committed

    def observe_isolation(db):
        set_isolation(db)
        isolation_levels.append(db.execute(text('SELECT @@transaction_isolation')).scalar_one())

    monkeypatch.setattr(runtime, '_provider_transaction_read_committed', observe_isolation)
    capabilities = {'agent_code': 'code_reviewer', 'execution_mode': 'model_only', 'tools': [],
                    'external_effects': False}
    monkeypatch.setattr(team_service, '_contract_snapshot', lambda _code: (None, capabilities))
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, owner, users, tasks, teams = _seed(factory)
    other_group, other_team, other_task = _add_team_in_another_group(factory, company, owner, users[1])
    assert group != other_group
    teams[1], tasks[1] = other_team, other_task
    _install_live_snapshots(factory, company, teams, legacy=True)
    plan = {'project_id': None, 'allowed_models': ['deepseek-flash'], 'max_calls_per_task': 3,
            'max_input_tokens_per_call': 200, 'max_output_tokens_per_call': 200,
            'max_team_credits_milli': 10_000, 'risk_level': 'low', 'requires_approval': False}
    with factory.begin() as db:
        wallet = ledger.get_or_create_wallet(db, 'enterprise', company)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f'registered-parallel:{uuid4().hex}',
                     kind='purchase', amount_milli=20_000)
        for team_id in teams:
            team = db.get(EnterpriseAgentTeam, team_id)
            team.plan_snapshot_json = runtime._canonical_json(plan)
            team.plan_sha256 = runtime._digest(plan)
            member = db.query(EnterpriseAgentTeamMember).filter_by(team_id=team_id).one()
            member.address = 'agent:code_reviewer'
            member.snapshot_sha256 = runtime._digest(capabilities)
            member.capabilities_json = runtime._canonical_json(capabilities)
    claims = [runtime.claim_task(factory, enterprise_id=company, task_id=task) for task in tasks]
    assert all(claim is not None for claim in claims)
    first_entered, release_first, second_entered, revoke_started = Event(), Event(), Event(), Event()
    provider = ledger.BillingProviderIdentity(identity_version='deepseek-official-v1', provider_name='deepseek',
                                              endpoint_host='api.deepseek.com', credential_source='system')

    def execute(index, *, call_index=1):
        with factory() as db:
            team = db.get(EnterpriseAgentTeam, teams[index])
            task = db.get(EnterpriseAgentTeamTask, tasks[index])
            member = db.get(EnterpriseAgentTeamMember, task.member_id)
        with dispatcher._task_billing_scope(factory, claims[index], team, task, member):
            scope = billing_runtime_gate._current_scope()
            def send():
                if index == 0:
                    first_entered.set()
                    assert release_first.wait(15)
                else:
                    second_entered.set()
                return 'done', {'input_tokens': 20, 'output_tokens': 10}
            return billing_metered_call.metered_call(
                session_factory=factory, request_key=f'ent-team:{teams[index]}:{tasks[index]}:1:{call_index}',
                model_name='deepseek-flash', user_id=scope['user_id'], max_input_tokens=200,
                max_output_tokens=200, send=send, provider_identity=provider,
                enterprise_wallet_id=scope['enterprise_wallet_id'], enterprise_id=company,
                group_id=scope['group_id'], attempt_guard=scope['attempt_guard'], send_guard=scope['send_guard'],
                on_reserved=scope['on_reserved'], on_unknown=scope['on_unknown'], on_settled=scope['on_settled'],
            )
    def revoke():
        revoke_started.set()
        with factory.begin() as db:
            enterprise_org_service.remove_group_member(db, company, group, owner, users[0])

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(execute, 0)
            assert first_entered.wait(15)
            second = pool.submit(execute, 1)
            assert second_entered.wait(5), 'independent registered team blocked before provider call'
            assert second.result(timeout=10) == 'done'
            revoked = pool.submit(revoke)
            assert revoke_started.wait(5)
            assert not revoked.done(), 'revocation bypassed the in-flight membership lock'
            release_first.set()
            assert first.result(timeout=10) == 'done'
            revoked.result(timeout=10)
    finally:
        release_first.set()
    with pytest.raises((ForbiddenError, NotFoundError)):
        execute(0, call_index=2)
    with factory() as db:
        rows = db.query(BillingReservation).filter(BillingReservation.enterprise_id == company).all()
        assert len(rows) == 2 and all(row.status == 'settled' for row in rows)
    assert isolation_levels == ['READ-COMMITTED', 'READ-COMMITTED']
