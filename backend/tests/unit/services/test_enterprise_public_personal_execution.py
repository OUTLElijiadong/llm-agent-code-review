"""Enterprise publications are usable by a personal-wallet account."""
import json

import httpx
import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_asset import EnterpriseAssetDraft, EnterpriseAssetDraftVersion, EnterpriseAssetPublication
from app.models.personal_agent import PersonalAgentRun
from app.services import enterprise_asset_service as assets
from app.services import personal_agent_execution_service as execution
from app.services import personal_agent_history_service as history
from tests.unit.services.test_personal_agent_execution import context as _personal_context


@pytest.fixture(name="context")
def fixture_context(tmp_path, monkeypatch):
    yield from _personal_context.__wrapped__(tmp_path, monkeypatch)


def make_public(factory, *, kind='agent', scope='global'):
    definition = ({'schema_version': 1, 'runtime': 'prompt', 'system_prompt': '系统指令'} if kind == 'agent' else
                  {'schema_version': 1, 'skill_type': 'llm_transform', 'prompt': '系统指令'})
    body = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with factory.begin() as db:
        draft = EnterpriseAssetDraft(enterprise_id=7, creator_id=99, kind=kind, title='共享资产',
                                     definition_json=body, current_version=1, request_key='d',
                                     request_digest='x', status='active')
        db.add(draft)
        db.flush()
        version = EnterpriseAssetDraftVersion(draft_id=draft.id, version_number=1, title=draft.title,
                                              definition_json=body, request_key='v', request_digest='x')
        db.add(version)
        db.flush()
        digest = assets._content_hash(kind, draft.title, body)
        row = EnterpriseAssetPublication(enterprise_id=7, draft_id=draft.id, version_id=version.id, version_number=1,
              requested_by=99, kind=kind, scope=scope, status='approved', title=draft.title, definition_json=body,
              content_sha256=digest, request_key='p', request_digest=assets._digest(scope, 7, 99, draft.id,
                                                                                   version.id, 1, digest))
        db.add(row)
        db.flush()
        return row.id


@pytest.mark.parametrize('kind', ['agent', 'skill'])
def test_global_personal_execution_and_history(context, kind):
    factory, _, monkeypatch = context
    pub_id = make_public(factory, kind=kind)
    sent = []
    def send(_config, payload):
        sent.append(payload)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '完成'}}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 7}}
    monkeypatch.setattr(execution, '_send_once', send)
    result = execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                       request_key='one', input_text='内容')
    assert result['status'] == 'succeeded' and result['output'] == '完成'
    assert result['enterprise_publication_id'] == pub_id and 'prompt' not in result
    assert execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                    request_key='one', input_text='内容')['id'] == result['id']
    assert len(sent) == 1 and not sent[0].get('tools')
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == 'settled' and reservation.user_id == 1 and reservation.enterprise_id is None
        rows = history.list_runs(db, owner_id=1, source='enterprise_public', before_id=None, limit=20)
        assert len(rows) == 1 and rows[0]['enterprise_publication_id'] == pub_id and 'prompt' not in rows[0]
        assert history.list_runs(db, owner_id=2, source='enterprise_public', before_id=None, limit=20) == []
    with pytest.raises(NotFoundError):
        execution.get_enterprise_publication_run(factory, owner_id=2, publication_id=pub_id, run_id=result['id'])
    with pytest.raises(ConflictError):
        execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                  request_key='one', input_text='不同')


def test_enterprise_only_asset_is_rejected_before_provider(context):
    factory, _, monkeypatch = context
    pub_id = make_public(factory, scope='enterprise')
    monkeypatch.setattr(execution, '_send_once', lambda *_: pytest.fail('must not send'))
    with pytest.raises(NotFoundError):
        execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                  request_key='private', input_text='内容')
    with factory() as db:
        assert db.query(BillingReservation).count() == 0


def test_revocation_suppresses_in_flight_result(context):
    factory, _, monkeypatch = context
    pub_id = make_public(factory)
    def send(*_args):
        with factory.begin() as db:
            db.query(EnterpriseAssetPublication).filter_by(id=pub_id).one().status = 'revoked'
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '不得展示'}}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 7}}
    monkeypatch.setattr(execution, '_send_once', send)
    result = execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                       request_key='revoked', input_text='内容')
    assert result['status'] == 'result_unavailable' and result['output'] is None
    assert result['result_reason'] == 'access_changed' and result['actual_milli'] is not None
    with pytest.raises(NotFoundError):
        execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                  request_key='new', input_text='内容')


def test_uncertain_provider_outcome_freezes_and_never_repeats(context):
    factory, _, monkeypatch = context
    pub_id = make_public(factory)
    attempts = []
    def send(*_args):
        attempts.append(1)
        raise httpx.ReadTimeout('uncertain')
    monkeypatch.setattr(execution, '_send_once', send)
    first = execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                       request_key='unknown', input_text='内容')
    second = execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                        request_key='unknown', input_text='内容')
    assert first['id'] == second['id'] and first['status'] == 'awaiting_reconciliation'
    assert attempts == [1]
    with factory() as db:
        assert db.query(BillingReservation).one().status == 'unknown'


def test_incompatible_version_rejected_before_hold(context):
    factory, _, monkeypatch = context
    pub_id = make_public(factory)
    with factory.begin() as db:
        row = db.query(EnterpriseAssetPublication).filter_by(id=pub_id).one()
        row.definition_json = '{}'
    monkeypatch.setattr(execution, '_send_once', lambda *_: pytest.fail('must not send'))
    with pytest.raises(ConflictError):
        execution.execute_enterprise_publication(factory, owner_id=1, publication_id=pub_id,
                                                  request_key='invalid', input_text='内容')
    with factory() as db:
        assert db.query(BillingReservation).count() == 0


def test_database_rejects_mixed_or_missing_source(context):
    from sqlalchemy.exc import IntegrityError

    factory, agent_id, _ = context
    pub_id = make_public(factory)
    for agent_value, version_value, enterprise_value in ((None, None, None), (agent_id, 1, pub_id)):
        with factory() as db:
            db.add(PersonalAgentRun(owner_id=1, agent_id=agent_value, version_id=version_value,
                                    enterprise_publication_id=enterprise_value, version_number=1,
                                    version_hash='x', request_key='bad', request_hash='x',
                                    reservation_key='bad', status='running'))
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()
