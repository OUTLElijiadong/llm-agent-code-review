import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker
from test_mysql_billing_group_version import _seed

from app.core.exceptions import ConflictError
from app.models.enterprise_org import EnterpriseInvitation, EnterpriseMember
from app.models.user import User
from app.services import enterprise_org_service as org

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url

def setup_invite(factory):
    company, owner, _, _, _ = _seed(factory)
    with factory.begin() as db:
        person = User(username='invite-audit-' + uuid4().hex[:10], password='x', role='user', status=1)
        db.add(person)
        db.flush()
        uid = person.id
        token = org.create_invitation(db, company, owner)
        invitation = db.query(EnterpriseInvitation).filter_by(
            token_hash=hashlib.sha256(token.encode()).hexdigest()
        ).one()
        invite_id = invitation.id
    return company, owner, uid, token, invite_id

def test_stale_invitation_revoke_commits_before_accept(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, token, invite_id = setup_invite(factory)
    with factory() as stale:
        old = stale.query(EnterpriseInvitation).filter_by(id=invite_id).one()
        assert old.revoked_at is None
        with factory.begin() as writer:
            org.revoke_invitation(writer, company, owner, invite_id)
        with pytest.raises(ConflictError):
            org.accept_invitation(stale, token, user)
        stale.rollback()
    with factory() as check:
        assert check.query(EnterpriseMember).filter_by(
            enterprise_id=company, user_id=user, status='active'
        ).count() == 0

def test_stale_invitation_cannot_be_used_after_first_user_joins(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, first_user, token, invite_id = setup_invite(factory)
    with factory.begin() as db:
        from app.models.enterprise_org import Enterprise
        db.get(Enterprise, company).seat_limit = 4
        person = User(username='invite-second-' + uuid4().hex[:10], password='x', role='user', status=1)
        db.add(person)
        db.flush()
        second_user = person.id
    with factory() as stale:
        old = stale.query(EnterpriseInvitation).filter_by(id=invite_id).one()
        assert old.use_count == 0
        with factory.begin() as writer:
            org.accept_invitation(writer, token, first_user)
        with pytest.raises(ConflictError):
            org.accept_invitation(stale, token, second_user)
        stale.rollback()
    with factory() as check:
        assert check.query(EnterpriseMember).filter_by(
            enterprise_id=company, user_id=second_user, status='active'
        ).count() == 0
        assert check.get(EnterpriseInvitation, invite_id).use_count == 1


def test_local_dirty_invitation_does_not_mask_concurrent_revoke(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, token, invite_id = setup_invite(factory)
    with factory() as stale:
        old = stale.get(EnterpriseInvitation, invite_id)
        original_expiry = old.expires_at
        old.expires_at += timedelta(hours=1)
        with factory.begin() as writer:
            org.revoke_invitation(writer, company, owner, invite_id)
        statements = []

        def capture_update(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lower().startswith('update enterprise_invitation'):
                statements.append(statement.lower())

        event.listen(fresh_chain, 'before_cursor_execute', capture_update)
        try:
            with pytest.raises(ConflictError):
                org.accept_invitation(stale, token, user)
        finally:
            event.remove(fresh_chain, 'before_cursor_execute', capture_update)
        assert len(statements) == 1
        assert 'expires_at' in statements[0] and 'revoked_at' not in statements[0]
        assert old.revoked_at is not None  # ORM identity is refreshed to the locked database row.
        stale.rollback()
    with factory() as check:
        assert check.get(EnterpriseInvitation, invite_id).revoked_at is not None
        assert check.get(EnterpriseInvitation, invite_id).expires_at == original_expiry

def test_accept_first_serializes_revoke(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, token, invite_id = setup_invite(factory)
    held, release, attempted = Event(), Event(), Event()
    connections = []
    def accept():
        with factory.begin() as db:
            member = org.accept_invitation(db, token, user)
            assert member.status == 'active'
            held.set()
            assert release.wait(20)
    def revoke():
        with factory.begin() as db:
            connections.append(db.execute(text('SELECT CONNECTION_ID()')).scalar_one())
            attempted.set()
            org.revoke_invitation(db, company, owner, invite_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(accept)
        assert held.wait(10)
        b = pool.submit(revoke)
        try:
            assert attempted.wait(10)
            row = None
            deadline = monotonic() + 8
            while monotonic() < deadline and row is None:
                with fresh_chain.connect() as observer:
                    row = observer.execute(text(
                        'SELECT l.OBJECT_NAME FROM performance_schema.data_lock_waits w '
                        'JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID '
                        'JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID '
                        'WHERE t.PROCESSLIST_ID=:cid LIMIT 1'), {'cid': connections[0]}).first()
                if row is None:
                    sleep(.05)
            assert row is not None and row[0] == 'enterprise', row
        finally:
            release.set()
        a.result(timeout=20)
        b.result(timeout=20)
    with factory() as check:
        assert check.query(EnterpriseMember).filter_by(
            enterprise_id=company, user_id=user, status='active'
        ).count() == 1
        assert check.get(EnterpriseInvitation, invite_id).revoked_at is not None
