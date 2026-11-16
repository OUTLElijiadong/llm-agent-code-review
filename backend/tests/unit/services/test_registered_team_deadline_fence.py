"""Registered team calls must respect the same deadline at every send fence."""
from datetime import timedelta

import pytest

from test_enterprise_agent_team_runtime_service import runtime_factory  # noqa: F401
from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_agent_team import EnterpriseAgentTeam, EnterpriseAgentTeamMember, EnterpriseAgentTeamTask
from app.services import billing_ledger_service as ledger, billing_metered_call, billing_runtime_gate
from app.services import enterprise_agent_team_dispatcher as dispatcher
from app.services import enterprise_agent_team_runtime_service as runtime
from app.services import enterprise_agent_team_service as team_service


@pytest.mark.parametrize('expiry', ['before-reserve', 'before-send', 'after-first-call'])
def test_registered_team_deadline_blocks_new_call(runtime_factory, monkeypatch, expiry):
    factory, now = runtime_factory
    monkeypatch.setattr(runtime, '_now', lambda: now)
    capabilities = {'agent_code': 'code_reviewer', 'execution_mode': 'model_only', 'tools': [], 'external_effects': False}
    monkeypatch.setattr(team_service, '_contract_snapshot', lambda _code: (None, capabilities))
    with factory.begin() as db:
        member = db.get(EnterpriseAgentTeamMember, 500)
        member.address = 'agent:code_reviewer'
        member.snapshot_sha256 = runtime._digest(capabilities)
        member.capabilities_json = runtime._canonical_json(capabilities)
        wallet = ledger.get_or_create_wallet(db, 'enterprise', 10)
        ledger.grant(db, wallet_id=wallet.id, grant_key='registered-deadline', kind='purchase', amount_milli=20_000)
    claim = runtime.claim_task(factory, enterprise_id=10, task_id=600, now=now)
    with factory.begin() as db:
        team = db.get(EnterpriseAgentTeam, 400)
        team.deadline_at = now - timedelta(seconds=1) if expiry == 'before-reserve' else now + timedelta(seconds=10)
        task = db.get(EnterpriseAgentTeamTask, 600)
        member = db.get(EnterpriseAgentTeamMember, 500)
    provider = ledger.BillingProviderIdentity(identity_version='deepseek-official-v1', provider_name='deepseek',
                                               endpoint_host='api.deepseek.com', credential_source='system')
    sent = []
    with dispatcher._task_billing_scope(factory, claim, team, task, member):
        scope = billing_runtime_gate._current_scope()
        on_reserved = scope['on_reserved']

        def expire_after_reserve(db, key):
            on_reserved(db, key)
            db.get(EnterpriseAgentTeam, 400).deadline_at = now - timedelta(seconds=1)

        def call(index, callback):
            return billing_metered_call.metered_call(
                session_factory=factory, request_key=f'ent-team:400:600:1:{index}', model_name='deepseek-flash',
                user_id=2, max_input_tokens=100, max_output_tokens=100, provider_identity=provider,
                enterprise_wallet_id=scope['enterprise_wallet_id'], enterprise_id=10, group_id=scope['group_id'],
                attempt_guard=scope['attempt_guard'], send_guard=scope['send_guard'],
                on_reserved=callback,
                on_unknown=scope['on_unknown'], on_settled=scope['on_settled'],
                send=lambda: (sent.append(True) or 'done', {'input_tokens': 1, 'output_tokens': 1}),
            )
        if expiry == 'after-first-call':
            assert call(1, on_reserved) == 'done'
            with factory.begin() as db:
                db.get(EnterpriseAgentTeam, 400).deadline_at = now - timedelta(seconds=1)
        expected = billing_metered_call.MeteredRequestNotSent if expiry == 'before-send' else ForbiddenError
        with pytest.raises(expected):
            call(2 if expiry == 'after-first-call' else 1,
                 expire_after_reserve if expiry == 'before-send' else on_reserved)
    assert sent == ([True] if expiry == 'after-first-call' else [])
    with factory() as db:
        reservation = db.query(BillingReservation).one_or_none()
        if expiry == 'before-send':
            assert reservation is not None and reservation.status == 'released'
        elif expiry == 'after-first-call':
            assert reservation is not None and reservation.status == 'settled'
        else:
            assert reservation is None
