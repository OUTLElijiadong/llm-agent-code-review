"""Isolated MySQL: migration 118 and authority evidence commit ordering."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from test_fresh_database_chain import fresh_chain, fresh_chain_url  # disposable loopback server only
from app.models.user import User
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.enterprise_chat import EnterpriseConversation, EnterpriseChatMessage, EnterpriseAssistantRun
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.services import enterprise_assistant_clarification_service as clarity
from datetime import timedelta


def _setup(engine, key):
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker.begin() as db:
        uid = 910000 + key * 10
        eid = 920000 + key
        room_id = 930000 + key
        message_id = 940000 + key * 10
        db.add_all([User(id=uid+i, username=f'clarify-{key}-{i}', password='x', role='user', status=1)
                    for i in range(1,4)])
        db.add(Enterprise(id=eid, name=f'clarify-{key}', owner_id=uid+1, seat_limit=5, status='active', paid_order_id=f'test-{key}'))
        db.add_all([EnterpriseMember(enterprise_id=eid, user_id=uid+i, role=role, status='active')
                    for i,role in [(1,'owner'),(2,'admin'),(3,'member')]])
        db.add(EnterpriseConversation(id=room_id, enterprise_id=eid, kind='company', conversation_key='company', created_by=uid+1))
        db.add_all([EnterpriseChatMessage(id=message_id, enterprise_id=eid, conversation_id=room_id, sender_id=uid+2,
                                          sender_type='human', client_message_id='old', content='先停止'),
                    EnterpriseChatMessage(id=message_id+1, enterprise_id=eid, conversation_id=room_id, sender_id=uid+3,
                                          sender_type='human', client_message_id='new', content='@小菱 执行')])
        run = EnterpriseAssistantRun(public_id=f'clarity-{key}', request_key=f'clarity-{key}', enterprise_id=eid,
                                     conversation_id=room_id, initiator_user_id=uid+3, source_message_id=message_id+1,
                                     prompt_sha256='a'*64, status='running')
        db.add(run); db.flush()
        db.add(EnterpriseAssistantRuntimeState(assistant_run_id=run.id, enterprise_id=eid, conversation_id=room_id,
                                               initiator_user_id=uid+3, source_message_id=message_id+1, status='running',
                                               permission_snapshot_json='{}', permission_snapshot_sha256='b'*64))
        db.flush()
        run_id = run.id
    with maker.begin() as db:
        request = clarity.create_request(db, enterprise_id=eid, conversation_id=room_id, run_id=run_id, phase='tool',
                                         call_id='call', action='create', target={}, arguments={},
                                         expires_at=clarity._now()+timedelta(hours=1))
        clarity.decide(db, enterprise_id=eid, conversation_id=room_id, request_id=request.id,
                       source_message_id=message_id, user_id=uid+2, decision='allow')
        request_id = request.id
    return maker, eid, room_id, uid+2, request_id


def test_mysql_aba_and_same_value(fresh_chain, monkeypatch):
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, actor, request_id = _setup(fresh_chain, 1)
    with maker.begin() as db:
        db.execute(text('UPDATE enterprise_member SET role=role WHERE enterprise_id=:e AND user_id=:u'), {'e':eid,'u':actor})
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'recorded'
    for role in ('member','admin'):
        with maker.begin() as db:
            db.execute(text('UPDATE enterprise_member SET role=:r WHERE enterprise_id=:e AND user_id=:u'), {'r':role,'e':eid,'u':actor})
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'


def test_mysql_decision_lock_wins_before_revocation(fresh_chain, monkeypatch):
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, actor, request_id = _setup(fresh_chain, 2)
    blocked, finished = Event(), Event()
    def revoke():
        with maker.begin() as db:
            blocked.set()
            db.execute(text('UPDATE enterprise_member SET role="member" WHERE enterprise_id=:e AND user_id=:u'), {'e':eid,'u':actor})
        finished.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        db = maker()
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'recorded'
        future = executor.submit(revoke)
        assert blocked.wait(3) and not finished.wait(.3)
        db.commit(); db.close()
        future.result(timeout=5)
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'


def test_mysql_revocation_wins_before_status(fresh_chain, monkeypatch):
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, actor, request_id = _setup(fresh_chain, 3)
    started, finished = Event(), Event()
    def status():
        with maker.begin() as db:
            started.set()
            result = clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id)
        finished.set()
        return result
    with ThreadPoolExecutor(max_workers=1) as executor:
        db = maker()
        db.execute(text('UPDATE enterprise_member SET role="member" WHERE enterprise_id=:e AND user_id=:u'), {'e':eid,'u':actor})
        future = executor.submit(status)
        assert started.wait(3) and not finished.wait(.3)
        db.commit(); db.close()
        assert future.result(timeout=5) == 'stale'


def test_mysql_account_revocation_wins_before_status(fresh_chain, monkeypatch):
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, actor, request_id = _setup(fresh_chain, 4)
    started, finished = Event(), Event()
    def status():
        with maker.begin() as db:
            started.set()
            result = clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id)
        finished.set()
        return result
    with ThreadPoolExecutor(max_workers=1) as executor:
        db = maker()
        db.execute(text('UPDATE user SET status=0 WHERE id=:u'), {'u':actor})
        future = executor.submit(status)
        assert started.wait(3) and not finished.wait(.3)
        db.commit(); db.close()
        assert future.result(timeout=5) == 'stale'
    with maker.begin() as db:
        db.execute(text('UPDATE user SET status=1 WHERE id=:u'), {'u':actor})
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'


def test_mysql_equal_rank_author_revision_preserves_history(fresh_chain, monkeypatch):
    from app.core.exceptions import ForbiddenError
    from app.models.enterprise_assistant_clarification import EnterpriseAssistantClarificationReview
    import pytest
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, author, _old_request = _setup(fresh_chain, 5)
    with maker.begin() as db:
        db.execute(text('UPDATE enterprise_member SET role="member" WHERE enterprise_id=:e AND user_id=:u'), {'e':eid,'u':author})
        db.add(User(id=author+2, username='clarify-peer', password='x', role='user', status=1))
        db.add(EnterpriseMember(enterprise_id=eid, user_id=author+2, role='member', status='active'))
        db.add(EnterpriseChatMessage(id=950005, enterprise_id=eid, conversation_id=rid, sender_id=author+2,
                                     sender_type='human', client_message_id='peer', content='可以执行'))
    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        request = clarity.create_request(db, enterprise_id=eid, conversation_id=rid, run_id=run.id, phase='tool',
                                         call_id='same-rank', action='create', target={}, arguments={},
                                         expires_at=clarity._now()+timedelta(hours=1))
        request_id, first_message = request.id, run.source_message_id-1
        clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                       source_message_id=first_message, user_id=author, decision='deny')
        clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                       source_message_id=950005, user_id=author+2, decision='allow')
    with maker.begin() as db:
        with pytest.raises(ForbiddenError):
            clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                           source_message_id=first_message, user_id=author+2, decision='allow')
    for _ in range(2):
        with maker.begin() as db:
            clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                           source_message_id=first_message, user_id=author, decision='allow')
    with maker.begin() as db:
        assert clarity.decision_status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'complete'
        reviews = db.query(EnterpriseAssistantClarificationReview).filter_by(request_id=request_id).all()
        assert len(reviews) == 1 and reviews[0].review_kind == 'author_revision'


def test_mysql_group_role_and_membership_aba(fresh_chain, monkeypatch):
    from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, author, _old_request = _setup(fresh_chain, 6)
    gid = 960006
    with maker.begin() as db:
        db.add(EnterpriseGroup(id=gid, enterprise_id=eid, name='组', status='active'))
        db.flush()
        room = db.get(EnterpriseConversation, rid)
        room.kind, room.group_id = 'group', gid
        db.add_all([EnterpriseGroupMember(enterprise_id=eid, group_id=gid, user_id=user,
                                         role='leader' if user == author else 'member', status='active')
                    for user in (author-1, author, author+1)])
    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        request = clarity.create_request(db, enterprise_id=eid, conversation_id=rid, run_id=run.id, phase='tool',
                                         call_id='group', action='create', target={}, arguments={},
                                         expires_at=clarity._now()+timedelta(hours=1))
        request_id, message_id = request.id, run.source_message_id-1
        clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                       source_message_id=message_id, user_id=author, decision='allow')
    for role, status in (('member', 'active'), ('leader', 'active'), ('leader', 'removed'), ('leader', 'active')):
        with maker.begin() as db:
            db.execute(text('UPDATE enterprise_group_member SET role=:r, status=:s WHERE enterprise_id=:e AND group_id=:g AND user_id=:u'),
                       {'r':role, 's':status, 'e':eid, 'g':gid, 'u':author})
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'
        version = db.execute(text('SELECT clarification_authority_version FROM enterprise_group_member WHERE enterprise_id=:e AND group_id=:g AND user_id=:u'),
                             {'e':eid,'g':gid,'u':author}).scalar_one()
        assert version == 4


def test_mysql_owner_transfer_aba(fresh_chain, monkeypatch):
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, author, request_id = _setup(fresh_chain, 7)
    for owner in (author, author-1):
        with maker.begin() as db:
            db.execute(text('UPDATE enterprise SET owner_id=:u WHERE id=:e'), {'e':eid, 'u':owner})
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'
        assert db.execute(text('SELECT clarification_authority_version FROM enterprise WHERE id=:e'), {'e':eid}).scalar_one() == 2


def test_mysql_higher_review_and_immutable_history(fresh_chain, monkeypatch):
    from sqlalchemy.exc import DBAPIError
    import pytest
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, author, _old_request = _setup(fresh_chain, 8)
    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        request = clarity.create_request(db, enterprise_id=eid, conversation_id=rid, run_id=run.id, phase='tool',
                                         call_id='review', action='create', target={}, arguments={},
                                         expires_at=clarity._now()+timedelta(hours=1))
        request_id, message_id = request.id, run.source_message_id-1
        clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                       source_message_id=message_id, user_id=author, decision='deny')
    for _ in range(2):
        with maker.begin() as db:
            clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                           source_message_id=message_id, user_id=author-1, decision='allow')
    with maker.begin() as db:
        assert clarity.decision_status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'complete'
    for sql in ('UPDATE enterprise_assistant_clarification_decision SET decision="allow" WHERE request_id=:r',
                'DELETE FROM enterprise_assistant_clarification_review WHERE request_id=:r'):
        with maker() as db:
            with pytest.raises(DBAPIError):
                db.execute(text(sql), {'r':request_id})
            db.rollback()
    with maker.begin() as db:
        assert clarity.decision_status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'complete'


def test_mysql_authority_read_wins_before_account_disable(fresh_chain, monkeypatch):
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, actor, request_id = _setup(fresh_chain, 9)
    started, finished = Event(), Event()
    def disable():
        with maker.begin() as db:
            started.set()
            db.execute(text('UPDATE user SET status=0 WHERE id=:u'), {'u':actor})
        finished.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        db = maker()
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'recorded'
        future = executor.submit(disable)
        assert started.wait(3) and not finished.wait(.3)
        db.commit(); db.close()
        future.result(timeout=5)
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'


def test_mysql_model_send_requires_fresh_persisted_clearance(fresh_chain, monkeypatch):
    from app.core.exceptions import ForbiddenError
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services.deepseek_responses_runtime import RunCheckpoint

    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    key = 20
    maker, eid, rid, _admin, _old_request = _setup(fresh_chain, key)
    uid = 910000 + key * 10
    message_id = 940000 + key * 10

    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        room = db.query(EnterpriseConversation).filter_by(id=rid, enterprise_id=eid).one()
        messages = (db.query(EnterpriseChatMessage)
                    .filter_by(enterprise_id=eid, conversation_id=rid, sender_type='human')
                    .order_by(EnterpriseChatMessage.id).all())
        db.get(EnterpriseChatMessage, message_id).content = '@小菱 请不要继续这个操作'
        run.model_name = 'deepseek-flash'
        checkpoint = RunCheckpoint(run_id=run.public_id, model='deepseek-flash', transcript=[], tools=[],
                                   instructions='企业助手指令')
        snapshot = runtime._role_snapshot(db, enterprise_id=eid, room=room, messages=messages,
                                          initiator_id=run.initiator_user_id)
        state.permission_snapshot_json = runtime._json(snapshot)
        state.permission_snapshot_sha256 = runtime._digest(snapshot)
        state.checkpoint_json = runtime._json(checkpoint.to_dict())

    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        evidence = clarity.model_send_evidence(db, run)
        assert evidence['candidate_message_ids'] == [message_id]
        call_id = f'{runtime._MODEL_SEND_CALL_PREFIX}{run.public_id}'
        target = runtime._model_send_target(eid, rid)
        arguments = runtime._model_send_arguments(run, state)
        request = clarity.create_request(db, enterprise_id=eid, conversation_id=rid, run_id=run.id,
                                         phase='model_send', call_id=call_id,
                                         action=runtime._MODEL_SEND_ACTION, target=target, arguments=arguments,
                                         expires_at=clarity._now()+timedelta(hours=1))
        clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request.id,
                       source_message_id=message_id, user_id=uid+2, decision='allow')
        marker = {'status':'cleared', 'request_id':int(request.id), 'version':int(request.version),
                  'fingerprint':request.authority_sha256,
                  'authority_version_sha256':request.authority_version_sha256,
                  'candidate_message_ids':[message_id]}
        checkpoint.context_metadata['model_send_clearance'] = marker
        runtime._require_model_send_clearance(db, run=run, state=state, checkpoint=checkpoint)

    with maker.begin() as db:
        db.get(EnterpriseChatMessage, message_id).content = '@小菱 现在改成允许继续'

    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        with pytest.raises(ForbiddenError, match='权限澄清已过期'):
            runtime._require_model_send_clearance(db, run=run, state=state,
                                                  checkpoint=RunCheckpoint(
                                                      run_id=run.public_id, model='deepseek-flash', transcript=[],
                                                      tools=[], instructions='企业助手指令',
                                                      context_metadata={'model_send_clearance': marker}))


@pytest.mark.parametrize('first', ['clarification', 'revocation'])
def test_mysql_group_revocation_serialization(fresh_chain, monkeypatch, first):
    from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    key = 10 if first == 'clarification' else 11
    maker, eid, rid, author, _old_request = _setup(fresh_chain, key)
    gid = 960000 + key
    with maker.begin() as db:
        db.add(EnterpriseGroup(id=gid, enterprise_id=eid, name='组', status='active'))
        db.flush()
        room = db.get(EnterpriseConversation, rid)
        room.kind, room.group_id = 'group', gid
        db.add_all([EnterpriseGroupMember(enterprise_id=eid, group_id=gid, user_id=user,
                                         role='leader' if user == author else 'member', status='active')
                    for user in (author-1, author, author+1)])
    with maker.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(enterprise_id=eid, conversation_id=rid).one()
        request = clarity.create_request(db, enterprise_id=eid, conversation_id=rid, run_id=run.id, phase='tool',
                                         call_id='group-race', action='create', target={}, arguments={},
                                         expires_at=clarity._now()+timedelta(hours=1))
        request_id, message_id = request.id, run.source_message_id-1
        clarity.decide(db, enterprise_id=eid, conversation_id=rid, request_id=request_id,
                       source_message_id=message_id, user_id=author, decision='allow')
    started, finished = Event(), Event()
    def competing():
        with maker.begin() as db:
            started.set()
            if first == 'clarification':
                db.execute(text('UPDATE enterprise_group_member SET status="removed" WHERE enterprise_id=:e AND group_id=:g AND user_id=:u'),
                           {'e':eid, 'g':gid, 'u':author})
                result = None
            else:
                result = clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id)
        finished.set()
        return result
    with ThreadPoolExecutor(max_workers=1) as executor:
        db = maker()
        if first == 'clarification':
            assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'recorded'
        else:
            db.execute(text('UPDATE enterprise_group_member SET status="removed" WHERE enterprise_id=:e AND group_id=:g AND user_id=:u'),
                       {'e':eid, 'g':gid, 'u':author})
        future = executor.submit(competing)
        assert started.wait(3) and not finished.wait(.3)
        db.commit(); db.close()
        result = future.result(timeout=5)
        if first == 'revocation':
            assert result == 'stale'
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'


def test_mysql_version_explicit_rewind_or_override_never_replays_authority(fresh_chain, monkeypatch):
    from app.models.enterprise_org import EnterpriseGroup, EnterpriseGroupMember
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, actor, request_id = _setup(fresh_chain, 12)
    gid = 960012
    with maker.begin() as db:
        db.add(EnterpriseGroup(id=gid, enterprise_id=eid, name='组', status='active'))
        db.add(EnterpriseGroupMember(enterprise_id=eid, group_id=gid, user_id=actor, role='leader', status='active'))
    cases = [
        ('user', 'id=:u', 'status', '0'),
        ('enterprise', 'id=:e', 'owner_id', ':u'),
        ('enterprise_member', 'enterprise_id=:e AND user_id=:u', 'role', "'member'"),
        ('enterprise_group_member', 'enterprise_id=:e AND group_id=:g AND user_id=:u', 'role', "'member'"),
    ]
    params = {'e':eid, 'g':gid, 'u':actor}
    for table, where, field, value in cases:
        query = text(f'SELECT clarification_authority_version FROM `{table}` WHERE {where}')
        with maker.begin() as db:
            db.execute(text(f'UPDATE `{table}` SET {field}={value}, clarification_authority_version=999 WHERE {where}'), params)
            assert db.execute(query, params).scalar_one() == 1
            db.execute(text(f'UPDATE `{table}` SET clarification_authority_version=0 WHERE {where}'), params)
            assert db.execute(query, params).scalar_one() == 1
    with maker.begin() as db:
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'


def test_mysql_delete_and_replace_cannot_reset_authority_epoch(fresh_chain, monkeypatch):
    monkeypatch.setattr('app.services.enterprise_org_service._billing_access_suspended', lambda *a, **k: False)
    maker, eid, rid, actor, request_id = _setup(fresh_chain, 13)
    with maker.begin() as db:
        db.execute(text("UPDATE enterprise_member SET role='member' WHERE enterprise_id=:e AND user_id=:u"),
                   {'e': eid, 'u': actor})
        db.execute(text("UPDATE enterprise_member SET role='admin' WHERE enterprise_id=:e AND user_id=:u"),
                   {'e': eid, 'u': actor})
        assert db.execute(text("""
            SELECT clarification_authority_version FROM enterprise_member
            WHERE enterprise_id=:e AND user_id=:u
        """), {'e': eid, 'u': actor}).scalar_one() == 2

    with maker.begin() as db:
        row = dict(db.execute(text("""
            SELECT * FROM enterprise_member WHERE enterprise_id=:e AND user_id=:u
        """), {'e': eid, 'u': actor}).mappings().one())
    row['clarification_authority_version'] = 0
    columns = ', '.join(f'`{column}`' for column in row)
    values = ', '.join(f':{column}' for column in row)
    with pytest.raises(OperationalError) as replace_error:
        with maker.begin() as db:
            db.execute(text(f"REPLACE INTO enterprise_member ({columns}) VALUES ({values})"), row)
    assert replace_error.value.orig.args[0] == 1644
    with pytest.raises(OperationalError) as delete_error:
        with maker.begin() as db:
            db.execute(text("DELETE FROM enterprise_member WHERE enterprise_id=:e AND user_id=:u"),
                       {'e': eid, 'u': actor})
    assert delete_error.value.orig.args[0] == 1644
    with maker.begin() as db:
        assert db.execute(text("""
            SELECT clarification_authority_version FROM enterprise_member
            WHERE enterprise_id=:e AND user_id=:u
        """), {'e': eid, 'u': actor}).scalar_one() == 2
        assert clarity.status(db, enterprise_id=eid, conversation_id=rid, request_id=request_id) == 'stale'
