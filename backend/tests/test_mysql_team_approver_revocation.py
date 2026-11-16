"""Live high-risk team sends serialize with approver permission revocation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _install_live_snapshots, _ready, _seed

from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_agent_team import (
    EnterpriseAgentTeam, EnterpriseAgentTeamApproval, EnterpriseAgentTeamMember, EnterpriseAgentTeamTask,
)
from app.services import billing_ledger_service as ledger, billing_metered_call, billing_runtime_gate
from app.services import enterprise_agent_team_dispatcher as dispatcher
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_agent_team_service as team_service
from app.services import enterprise_assistant_runtime_service as parent_runtime
from app.services import enterprise_org_service as org, user_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


@pytest.mark.parametrize('change', ['demote', 'disable'])
@pytest.mark.parametrize('send_first', [True, False], ids=['send-first', 'revocation-first'])
def test_approver_revocation_serializes_with_team_send(
    fresh_chain, verified_model_catalog, monkeypatch, change, send_first,
):
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, owner, users, tasks, teams = _seed(factory, first_user_enterprise_role='admin')
    approver, user, task_id, team_id = users[0], users[1], tasks[1], teams[1]
    _install_live_snapshots(factory, company, [team_id], legacy=True)
    capabilities = {'agent_code': 'code_reviewer', 'execution_mode': 'model_only', 'tools': [],
                    'external_effects': False}
    monkeypatch.setattr(team_service, '_contract_snapshot', lambda _code: (None, capabilities))
    plan = {'project_id': None, 'allowed_models': ['deepseek-flash'], 'max_calls_per_task': 3,
            'max_input_tokens_per_call': 200, 'max_output_tokens_per_call': 200,
            'max_team_credits_milli': 10_000, 'risk_level': 'high', 'requires_approval': True}
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, team_id)
        task = db.get(EnterpriseAgentTeamTask, task_id)
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        task.approval_sha256 = team.plan_sha256
        task.approved_by_user_id = approver
        db.add(EnterpriseAgentTeamApproval(
            team_id=team_id, task_id=task_id, enterprise_id=company, conversation_id=team.conversation_id,
            group_id=group, initiator_user_id=user, approval_key=f'approval-{uuid4().hex}',
            plan_sha256=team.plan_sha256, required_permission_code='enterprise_admin', status='approved',
            plan_json=team.plan_snapshot_json, requested_by_user_id=user, decided_by_user_id=approver,
        ))
        member = db.query(EnterpriseAgentTeamMember).filter_by(team_id=team_id).one()
        member.address = 'agent:code_reviewer'
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)
        wallet = ledger.get_or_create_wallet(db, 'enterprise', company)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f'approver-race:{uuid4().hex}',
                     kind='purchase', amount_milli=20_000)
    claim = runtime.claim_task(factory, enterprise_id=company, task_id=task_id)
    assert claim is not None
    with factory() as db:
        team = db.get(EnterpriseAgentTeam, team_id)
        task = db.get(EnterpriseAgentTeamTask, task_id)
        member = db.get(EnterpriseAgentTeamMember, task.member_id)
    provider = ledger.BillingProviderIdentity(identity_version='deepseek-official-v1', provider_name='deepseek',
                                               endpoint_host='api.deepseek.com', credential_source='system')
    entered, release, writer_started, writer_finished = Event(), Event(), Event(), Event()
    sent, writer_cid = [], []

    def call(index):
        with dispatcher._task_billing_scope(factory, claim, team, task, member):
            scope = billing_runtime_gate._current_scope()

            def send():
                sent.append(index)
                entered.set()
                if send_first:
                    assert release.wait(15)
                return 'done', {'input_tokens': 20, 'output_tokens': 10}

            return billing_metered_call.metered_call(
                session_factory=factory, request_key=f'ent-team:{team_id}:{task_id}:1:{index}',
                model_name='deepseek-flash', user_id=user, max_input_tokens=200, max_output_tokens=200,
                send=send, provider_identity=provider, enterprise_wallet_id=scope['enterprise_wallet_id'],
                enterprise_id=company, group_id=group, attempt_guard=scope['attempt_guard'],
                send_guard=scope['send_guard'], on_reserved=scope['on_reserved'],
                on_unknown=scope['on_unknown'], on_settled=scope['on_settled'],
            )

    def revoke():
        with factory.begin() as db:
            writer_cid.append(db.execute(text('SELECT CONNECTION_ID()')).scalar_one())
            writer_started.set()
            if change == 'demote':
                org.set_enterprise_role(db, company, owner, approver, 'member')
            else:
                user_service.toggle_status(db, approver, 0)
        writer_finished.set()

    if send_first:
        with ThreadPoolExecutor(max_workers=2) as pool:
            active = pool.submit(call, 1)
            try:
                assert entered.wait(15)
                writer = pool.submit(revoke)
                assert writer_started.wait(10)
                deadline, waiting_table = monotonic() + 8, None
                while monotonic() < deadline:
                    with fresh_chain.connect() as observer:
                        waiting_table = observer.execute(text(
                            'SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w '
                            'JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID '
                            'JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID '
                            'WHERE t.PROCESSLIST_ID=:cid LIMIT 1'
                        ), {'cid': writer_cid[0]}).scalar_one_or_none()
                    if waiting_table or writer.done():
                        break
                    sleep(0.05)
                assert waiting_table in {'enterprise', 'user', 'enterprise_member'}
                assert not writer_finished.is_set()
            finally:
                release.set()
            assert active.result(timeout=20) == 'done'
            writer.result(timeout=20)
        with pytest.raises(ForbiddenError):
            call(2)
    else:
        revoke()
        with pytest.raises(ForbiddenError):
            call(1)
    assert sent == ([1] if send_first else [])
    with factory() as db:
        reservations = db.query(BillingReservation).filter_by(enterprise_id=company).all()
        assert len(reservations) == (1 if send_first else 0)
        if send_first:
            assert reservations[0].status == 'settled'
