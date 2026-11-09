"""MySQL RR tests for transaction-scoped enterprise billing attribution versions."""

from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import test_fresh_database_chain as chain
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.enterprise_org import EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_org_service as org

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


class CrossEnterpriseGapWait(AssertionError):
    """A confirmed unrelated-enterprise row or gap wait."""


def test_mysql_116_migration_preserves_existing_rows(fresh_chain, monkeypatch):
    migration_path = Path(__file__).resolve().parents[1] / "alembic/versions/116_ent_billing_group.py"
    spec = spec_from_file_location("migration_mysql_billing_group", migration_path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with fresh_chain.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TEMPORARY TABLE enterprise_member (id BIGINT PRIMARY KEY, default_group_id BIGINT)"
        )
        conn.exec_driver_sql("CREATE TEMPORARY TABLE enterprise_assistant_run (id BIGINT PRIMARY KEY, group_id BIGINT)")
        conn.exec_driver_sql("INSERT INTO enterprise_member VALUES (1, 7)")
        conn.exec_driver_sql("INSERT INTO enterprise_assistant_run VALUES (1, NULL), (2, 7)")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        assert conn.exec_driver_sql(
            "SELECT default_group_id, billing_group_version FROM enterprise_member WHERE id=1"
        ).one() == (7, 0)
        assert conn.exec_driver_sql(
            "SELECT group_id, billing_group_id, billing_attribution_version, billing_membership_version, "
            "billing_user_token_version FROM enterprise_assistant_run ORDER BY id"
        ).all() == [(None, None, 0, None, None), (7, None, 0, None, None)]
        conn.exec_driver_sql("DROP TEMPORARY TABLE enterprise_assistant_run")
        conn.exec_driver_sql("DROP TEMPORARY TABLE enterprise_member")


def _seed(factory):
    suffix = uuid4().hex[:10]
    with factory.begin() as db:
        owner = User(username=f"bg-owner-{suffix}", password="x", role="user", status=1)
        user = User(username=f"bg-user-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, user])
        db.flush()
        company = org.create_enterprise_from_paid_order(
            db, owner_id=owner.id, name="归属版本", seat_limit=3, paid_order_id=f"bg-{suffix}"
        )
        owner_id, user_id, company_id = owner.id, user.id, company.id
    with factory.begin() as db:
        token = org.create_invitation(db, company_id, owner_id)
    with factory.begin() as db:
        org.accept_invitation(db, token, user_id)
    with factory.begin() as db:
        first = org.create_group(db, company_id, owner_id, f"甲-{suffix}")
        second = org.create_group(db, company_id, owner_id, f"乙-{suffix}")
        first_id, second_id = first.id, second.id
    return company_id, owner_id, user_id, first_id, second_id


def test_stale_mysql_snapshot_cannot_reuse_old_group_version(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, first, second = _seed(factory)
    with factory.begin() as db:
        org.set_group_member(db, company, first, owner, user, "member")
        org.set_group_member(db, company, second, owner, user, "member")
        org.set_default_group(db, company, owner, user, first)
    with factory() as stale:
        old = stale.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        assert (old.default_group_id, old.billing_group_version) == (first, 3)
        with factory.begin() as writer:
            org.set_default_group(writer, company, owner, user, second)
        with factory.begin() as writer:
            org.set_default_group(writer, company, owner, user, first)
        current = org.set_default_group(stale, company, owner, user, second)
        assert current.billing_group_version == 6
        stale.commit()
    with factory() as verify:
        member = verify.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        assert (member.default_group_id, member.billing_group_version) == (second, 6)


def test_rejoin_after_removal_refreshes_older_member_snapshot(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, _, _ = _seed(factory)
    with factory.begin() as db:
        token = org.create_invitation(db, company, owner)
    with factory() as stale:
        old = stale.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        assert (old.status, old.billing_group_version) == ("active", 0)
        with factory.begin() as writer:
            org.remove_enterprise_member(writer, company, owner, user)
        reinstated = org.accept_invitation(stale, token, user)
        assert (reinstated.id, reinstated.status, reinstated.billing_group_version) == (old.id, "active", 2)
        stale.commit()
    with factory() as db:
        member = db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        assert (member.status, member.billing_group_version) == ("active", 2)


def test_concurrent_group_reactivation_increments_once_and_rollback_is_atomic(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    company, owner, user, first, _ = _seed(factory)
    with factory.begin() as db:
        org.set_group_member(db, company, first, owner, user, "member")
        org.remove_group_member(db, company, first, owner, user)
    held, release, started = Event(), Event(), Event()
    waiting_cid = []

    def first_writer():
        with factory.begin() as db:
            org.set_group_member(db, company, first, owner, user, "member")
            held.set()
            assert release.wait(15)

    def second_writer():
        with factory.begin() as db:
            waiting_cid.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            started.set()
            org.set_group_member(db, company, first, owner, user, "member")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first_writer)
        assert held.wait(10)
        second_result = pool.submit(second_writer)
        try:
            assert started.wait(10)
            lock = None
            deadline = monotonic() + 8
            while monotonic() < deadline and lock is None:
                with fresh_chain.connect() as observer:
                    lock = observer.execute(text(
                        "SELECT l.OBJECT_NAME, l.INDEX_NAME FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                        "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                    ), {"cid": waiting_cid[0]}).first()
                if lock is None:
                    sleep(0.05)
            assert lock is not None and lock[0] == "enterprise", lock
        finally:
            release.set()
        first_result.result(timeout=15)
        second_result.result(timeout=15)
    with factory() as db:
        member = db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        assert member.billing_group_version == 3
        org.remove_group_member(db, company, first, owner, user)
        assert member.billing_group_version == 4
        db.rollback()
    with factory() as db:
        member = db.query(EnterpriseMember).filter_by(enterprise_id=company, user_id=user).one()
        row = db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=company, group_id=first, user_id=user
        ).one()
        assert (member.billing_group_version, row.status) == (3, "active")


def test_unrelated_enterprises_can_accept_new_members_without_waiting(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    first = _seed(factory)
    second = _seed(factory)
    suffix = uuid4().hex[:10]
    with factory.begin() as db:
        people = [User(username=f"bg-new-{suffix}-{i}", password="x", role="user", status=1) for i in (1, 2)]
        db.add_all(people)
        db.flush()
        user_ids = [person.id for person in people]
    with factory.begin() as db:
        first_token = org.create_invitation(db, first[0], first[1])
    with factory.begin() as db:
        second_token = org.create_invitation(db, second[0], second[1])
    held, release, attempted, done = Event(), Event(), Event(), Event()
    waiting_cid = []

    def first_writer():
        with factory.begin() as db:
            org.accept_invitation(db, first_token, user_ids[0])
            held.set()
            assert release.wait(15)

    def second_writer():
        with factory.begin() as db:
            waiting_cid.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            attempted.set()
            org.accept_invitation(db, second_token, user_ids[1])
        done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first_writer)
        assert held.wait(10)
        second_result = pool.submit(second_writer)
        try:
            assert attempted.wait(10)
            lock = None
            deadline = monotonic() + 5
            while monotonic() < deadline and not done.is_set() and lock is None:
                with fresh_chain.connect() as observer:
                    lock = observer.execute(text(
                        "SELECT l.OBJECT_NAME,l.INDEX_NAME,l.LOCK_MODE,l.LOCK_DATA "
                        "FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                        "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                        "WHERE t.PROCESSLIST_ID=:cid LIMIT 1"
                    ), {"cid": waiting_cid[0]}).first()
                if not done.is_set() and lock is None:
                    sleep(0.05)
            if lock is not None:
                raise CrossEnterpriseGapWait(f"另一企业的新增成员等待行锁/间隙: {tuple(lock)!r}")
            assert done.is_set(), "未观测到锁，但另一企业操作在限定时间内未完成"
        finally:
            release.set()
        first_result.result(timeout=15)
        second_result.result(timeout=15)
