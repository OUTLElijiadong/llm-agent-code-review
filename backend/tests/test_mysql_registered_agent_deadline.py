"""Registered team deadline fences against the real MySQL accounting schema."""

from datetime import timedelta
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy.orm import sessionmaker
from test_mysql_enterprise_group_shared_lock import _install_live_snapshots, _ready, _seed

from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_agent_team import EnterpriseAgentTeam, EnterpriseAgentTeamMember, EnterpriseAgentTeamTask
from app.services import billing_ledger_service as ledger, billing_metered_call, billing_runtime_gate
from app.services import enterprise_agent_team_dispatcher as dispatcher
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_agent_team_service as team_service
from app.services import enterprise_assistant_runtime_service as parent_runtime

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
verified_model_catalog = chain.verified_model_catalog


@pytest.mark.parametrize('expiry', ['before-send', 'after-first-call'])
def test_registered_team_deadline_fences_mysql_sends(fresh_chain, verified_model_catalog, monkeypatch, expiry):
    real_snapshot = parent_runtime._live_role_snapshot
    _ready(monkeypatch)
    monkeypatch.setattr(parent_runtime, '_live_role_snapshot', real_snapshot)
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, group, _owner, users, tasks, teams = _seed(factory)
    # The last source message has no newer same-authority directive.
    team_id, task_id, user_id = teams[1], tasks[1], users[1]
    _install_live_snapshots(factory, company, [team_id], legacy=True)
    capabilities = {'agent_code': 'code_reviewer', 'execution_mode': 'model_only', 'tools': [],
                    'external_effects': False}
    monkeypatch.setattr(team_service, '_contract_snapshot', lambda _code: (None, capabilities))
    plan = {'project_id': None, 'allowed_models': ['deepseek-flash'], 'max_calls_per_task': 3,
            'max_input_tokens_per_call': 200, 'max_output_tokens_per_call': 200,
            'max_team_credits_milli': 10_000, 'risk_level': 'low', 'requires_approval': False}
    initial = runtime._now().replace(microsecond=0)
    clock = {'now': initial}
    monkeypatch.setattr(runtime, '_now', lambda: clock['now'])
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, team_id)
        team.deadline_at = initial + timedelta(seconds=20)
        team.plan_snapshot_json = runtime._canonical_json(plan)
        team.plan_sha256 = runtime._digest(plan)
        member = db.query(EnterpriseAgentTeamMember).filter_by(team_id=team_id).one()
        member.address = 'agent:code_reviewer'
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)
        wallet = ledger.get_or_create_wallet(db, 'enterprise', company)
        ledger.grant(db, wallet_id=wallet.id, grant_key=f'deadline-mysql:{uuid4().hex}',
                     kind='purchase', amount_milli=20_000)
    claim = runtime.claim_task(factory, enterprise_id=company, task_id=task_id, now=initial)
    assert claim is not None
    with factory() as db:
        team = db.get(EnterpriseAgentTeam, team_id)
        task = db.get(EnterpriseAgentTeamTask, task_id)
        member = db.get(EnterpriseAgentTeamMember, task.member_id)
    provider = ledger.BillingProviderIdentity(identity_version='deepseek-official-v1', provider_name='deepseek',
                                               endpoint_host='api.deepseek.com', credential_source='system')
    sent = []
    if expiry == 'before-send':
        original_mark = ledger.mark_model_attempt_started

        def mark_and_advance(db, key):
            result = original_mark(db, key)
            clock['now'] = initial + timedelta(seconds=21)
            return result

        monkeypatch.setattr(ledger, 'mark_model_attempt_started', mark_and_advance)

    with dispatcher._task_billing_scope(factory, claim, team, task, member):
        scope = billing_runtime_gate._current_scope()

        def call(index):
            return billing_metered_call.metered_call(
                session_factory=factory, request_key=f'ent-team:{team_id}:{task_id}:1:{index}',
                model_name='deepseek-flash', user_id=user_id, max_input_tokens=200, max_output_tokens=200,
                send=lambda: (sent.append(index) or 'done', {'input_tokens': 20, 'output_tokens': 10}),
                provider_identity=provider, enterprise_wallet_id=scope['enterprise_wallet_id'],
                enterprise_id=company, group_id=group, attempt_guard=scope['attempt_guard'],
                send_guard=scope['send_guard'], on_reserved=scope['on_reserved'],
                on_unknown=scope['on_unknown'], on_settled=scope['on_settled'],
            )

        if expiry == 'before-send':
            with pytest.raises(billing_metered_call.MeteredRequestNotSent):
                call(1)
        else:
            assert call(1) == 'done'
            clock['now'] = initial + timedelta(seconds=21)
            with pytest.raises(ForbiddenError, match='截止时间'):
                call(2)
    assert sent == ([] if expiry == 'before-send' else [1])
    with factory() as db:
        rows = db.query(BillingReservation).filter_by(enterprise_id=company).all()
        assert len(rows) == 1
        assert rows[0].request_key == f'ent-team:{team_id}:{task_id}:1:1'
        assert rows[0].status == ('released' if expiry == 'before-send' else 'settled')
