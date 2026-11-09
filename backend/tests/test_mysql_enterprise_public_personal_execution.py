"""MySQL migration and lock-order checks for enterprise global personal runs.

Only run with PRISM_FRESH_CHAIN_MYSQL_URL pointing to a disposable empty server.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from uuid import uuid4

import pymysql
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event
from test_mysql_personal_agent_execution import _observe_wait, _setup, _wait_for_lock

from app.core.exceptions import NotFoundError
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_asset import EnterpriseAssetDraft, EnterpriseAssetDraftVersion, EnterpriseAssetPublication
from app.services import enterprise_asset_service as assets
from app.services import personal_agent_execution_service as execution

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def test_108_existing_personal_runs_survive_110_and_source_constraint(fresh_chain_url):
    """Seed old private and public rows *before* upgrading, never on a nonempty server."""
    conn = chain._connect(fresh_chain_url)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s", (chain.DATABASE,))
            assert cursor.fetchone()[0] == 0, "requires an empty isolated MySQL server"
        chain._execute_sql_file(conn, chain.INIT_SQL)
        chain._execute_sql_file(conn, chain.SEED_SQL)
        chain._alembic_upgrade(fresh_chain_url, "108_personal_agent_publication")
        db = chain._connect(fresh_chain_url, database=chain.DATABASE)
        try:
            with db.cursor() as c:
                c.execute(
                    "INSERT INTO personal_agent (owner_id,create_key,create_hash,current_version,status,"
                    "create_time,update_time) VALUES (1,'legacy','h',1,'active',UTC_TIMESTAMP(),UTC_TIMESTAMP())"
                )
                agent_id = c.lastrowid
                c.execute(
                    "INSERT INTO personal_agent_version (agent_id,version_number,title,prompt,request_key,"
                    "request_hash,create_time,update_time) VALUES (%s,1,'old','prompt','v','h',"
                    "UTC_TIMESTAMP(),UTC_TIMESTAMP())",
                    (agent_id,),
                )
                version_id = c.lastrowid
                c.execute(
                    "INSERT INTO personal_agent_publication (owner_id,agent_id,version_id,version_number,title,"
                    "prompt,content_hash,request_key,status,create_time,update_time) VALUES "
                    "(1,%s,%s,1,'old','prompt','h','p','approved',UTC_TIMESTAMP(),UTC_TIMESTAMP())",
                    (agent_id, version_id),
                )
                pub_id = c.lastrowid
                sql = (
                    "INSERT INTO personal_agent_run (owner_id,agent_id,version_id,version_number,version_hash,"
                    "publication_id,request_key,request_hash,reservation_key,status,create_time,update_time) "
                    "VALUES (1,%s,%s,1,'h',%s,%s,'h',%s,'succeeded',UTC_TIMESTAMP(),UTC_TIMESTAMP())"
                )
                c.execute(sql, (agent_id, version_id, None, "private-old", "private-res"))
                c.execute(sql, (agent_id, version_id, pub_id, "public-old", "public-res"))
            chain._alembic_upgrade(fresh_chain_url, "head")
            with db.cursor() as c:
                c.execute(
                    "SELECT request_key,agent_id,version_id,publication_id,enterprise_publication_id "
                    "FROM personal_agent_run ORDER BY request_key"
                )
                assert c.fetchall() == (
                    ("private-old", agent_id, version_id, None, None),
                    ("public-old", agent_id, version_id, pub_id, None),
                )
                with pytest.raises(pymysql.err.MySQLError) as invalid:
                    c.execute(
                        "INSERT INTO personal_agent_run (owner_id,agent_id,version_id,version_number,version_hash,"
                        "request_key,request_hash,reservation_key,status,create_time,update_time) "
                        "VALUES (1,NULL,NULL,1,'h','invalid','h','invalid-res','running',"
                        "UTC_TIMESTAMP(),UTC_TIMESTAMP())"
                    )
                assert invalid.value.args[0] == 3819  # MySQL CHECK constraint, driver reports OperationalError
        finally:
            db.close()
    finally:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {chain.DATABASE}")
        conn.close()


def _make_public(factory, kind):
    suffix = uuid4().hex
    definition = (
        {"schema_version": 1, "runtime": "prompt", "system_prompt": "企业共享提示"}
        if kind == "agent"
        else {"schema_version": 1, "skill_type": "llm_transform", "prompt": "企业共享提示"}
    )
    body = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with factory.begin() as db:
        draft = EnterpriseAssetDraft(
            enterprise_id=7,
            creator_id=99,
            kind=kind,
            title="公共资产",
            definition_json=body,
            current_version=1,
            request_key=suffix,
            request_digest="x",
            status="active",
        )
        db.add(draft)
        db.flush()
        version = EnterpriseAssetDraftVersion(
            draft_id=draft.id,
            version_number=1,
            title=draft.title,
            definition_json=body,
            request_key=suffix,
            request_digest="x",
        )
        db.add(version)
        db.flush()
        content = assets._content_hash(kind, draft.title, body)
        row = EnterpriseAssetPublication(
            enterprise_id=7,
            draft_id=draft.id,
            version_id=version.id,
            version_number=1,
            requested_by=99,
            kind=kind,
            scope="global",
            status="approved",
            title=draft.title,
            definition_json=body,
            content_sha256=content,
            request_key=suffix,
            request_digest=assets._digest("global", 7, 99, draft.id, version.id, 1, content),
        )
        db.add(row)
        db.flush()
        return int(row.id)


@pytest.mark.parametrize("kind", ["agent", "skill"])
@pytest.mark.parametrize("winner", ["revoke", "run"])
def test_revoke_and_personal_send_are_serialized(fresh_chain, monkeypatch, winner, kind):
    factory, caller_id, _agent, sends = _setup(fresh_chain, monkeypatch)
    pub_id = _make_public(factory, kind)
    with factory.begin() as db:
        from app.models.user import User

        admin_id = int(db.query(User).filter_by(username="admin", role="super_admin").one().id)
    locked, release, hit, revoked = Event(), Event(), Event(), Event()
    first_thread, second_thread = [], []
    before, observed = _observe_wait(fresh_chain, second_thread, "enterprise_asset_publication", hit)

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if (
            first_thread
            and get_ident() == first_thread[0]
            and "enterprise_asset_publication" in statement.lower()
            and "FOR UPDATE" in statement.upper()
            and not locked.is_set()
        ):
            locked.set()
            assert release.wait(10)

    if winner == "run":

        def send_after_revoke(_config, _payload):
            sends.append(1)
            assert revoked.wait(10)
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": "迟到"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            }

        monkeypatch.setattr(execution, "_send_once", send_after_revoke)

    def run():
        try:
            return execution.execute_enterprise_publication(
                factory, owner_id=caller_id, publication_id=pub_id, request_key="call", input_text="你好"
            )
        except NotFoundError:
            return "not-available"

    def revoke():
        with factory() as db:
            assets.revoke_global_publication(db, pub_id, admin_id, reason="remove")
        revoked.set()
        return "revoked"

    first_fn, second_fn = (revoke, run) if winner == "revoke" else (run, revoke)

    def first():
        first_thread.append(get_ident())
        return first_fn()

    def second():
        second_thread.append(get_ident())
        return second_fn()

    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            assert locked.wait(10)
            try:
                second_future = pool.submit(second)
                assert hit.wait(10)
                _wait_for_lock(fresh_chain, observed)
            finally:
                release.set()
            first_result = first_future.result(timeout=20)
            second_result = second_future.result(timeout=20)
    finally:
        release.set()
        revoked.set()
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    with factory() as db:
        reservations = db.query(BillingReservation).filter_by(user_id=caller_id).all()
        if winner == "revoke":
            assert first_result == "revoked" and second_result == "not-available"
            assert not sends and not reservations
        else:
            assert second_result == "revoked" and first_result["status"] == "result_unavailable"
            assert first_result["output"] is None and len(sends) == len(reservations) == 1
            assert reservations[0].status == "settled"


def test_same_key_concurrent_global_runs_charge_once(fresh_chain, monkeypatch):
    factory, caller_id, _agent, sends = _setup(fresh_chain, monkeypatch)
    pub_id = _make_public(factory, "agent")
    locked, release, hit, sent, finish = Event(), Event(), Event(), Event(), Event()
    first_thread, second_thread = [], []
    # The initial replay check locks the caller before the configuration row.
    before, observed = _observe_wait(fresh_chain, second_thread, "user", hit)

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if (
            first_thread
            and get_ident() == first_thread[0]
            and "enterprise_asset_publication" in statement.lower()
            and "FOR UPDATE" in statement.upper()
            and not locked.is_set()
        ):
            locked.set()
            assert release.wait(10)

    def provider(_config, _payload):
        sends.append(1)
        sent.set()
        assert finish.wait(10)
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "好"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        }

    monkeypatch.setattr(execution, "_send_once", provider)

    def run(ident):
        (first_thread if ident == 1 else second_thread).append(get_ident())
        return execution.execute_enterprise_publication(
            factory, owner_id=caller_id, publication_id=pub_id, request_key="same", input_text="你好"
        )

    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, 1)
            assert locked.wait(10)
            second = pool.submit(run, 2)
            try:
                assert hit.wait(10)
                _wait_for_lock(fresh_chain, observed)
            finally:
                release.set()
            try:
                assert sent.wait(10)
                second_result = second.result(timeout=20)
            finally:
                finish.set()
            first_result = first.result(timeout=20)
    finally:
        release.set()
        finish.set()
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    assert first_result["status"] == "succeeded"
    assert second_result["id"] == first_result["id"] and second_result["status"] == "running"
    assert len(sends) == 1
    with factory() as db:
        reservations = db.query(BillingReservation).filter_by(user_id=caller_id).all()
        assert len(reservations) == 1 and reservations[0].status == "settled"


def test_two_accounts_same_global_asset_serialize_publication_without_deadlock(fresh_chain, monkeypatch):
    factory, user_a, _agent, _ = _setup(fresh_chain, monkeypatch)
    _, user_b, _agent_b, _ = _setup(fresh_chain, monkeypatch)
    pub_id = _make_public(factory, "agent")
    locked, release, hit = Event(), Event(), Event()
    first_thread, second_thread = [], []
    # Configuration is locked before either run locks the publication.
    before, observed = _observe_wait(fresh_chain, second_thread, "system_config", hit)
    sends = []

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if (
            first_thread
            and get_ident() == first_thread[0]
            and "enterprise_asset_publication" in statement.lower()
            and "FOR UPDATE" in statement.upper()
            and not locked.is_set()
        ):
            locked.set()
            assert release.wait(10)

    def provider(_config, _payload):
        sends.append(1)
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "好"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        }

    def run(user, workers):
        workers.append(get_ident())
        return execution.execute_enterprise_publication(
            factory, owner_id=user, publication_id=pub_id, request_key="same-key-distinct-owners", input_text="你好"
        )

    monkeypatch.setattr(execution, "_send_once", provider)
    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, user_a, first_thread)
            assert locked.wait(10)
            second = pool.submit(run, user_b, second_thread)
            try:
                assert hit.wait(10)
                _wait_for_lock(fresh_chain, observed)
            finally:
                release.set()
            first_result, second_result = first.result(timeout=20), second.result(timeout=20)
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    assert first_result["status"] == second_result["status"] == "succeeded"
    assert first_result["id"] != second_result["id"] and len(sends) == 2
    with factory() as db:
        reservations = db.query(BillingReservation).filter(BillingReservation.user_id.in_([user_a, user_b])).all()
        assert len(reservations) == 2 and {r.user_id for r in reservations} == {user_a, user_b}
        assert all(r.status == "settled" and r.enterprise_id is None for r in reservations)


def _make_managed_public(factory):
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User

    suffix = uuid4().hex
    with factory.begin() as db:
        author = User(username=f"asset-author-{suffix}", password="x", role="user", status=1)
        db.add(author)
        db.flush()
        company = Enterprise(
            name=f"资产企业-{suffix}",
            owner_id=author.id,
            seat_limit=1,
            status="active",
            paid_order_id=f"asset-order-{suffix}",
        )
        db.add(company)
        db.flush()
        db.add(EnterpriseMember(enterprise_id=company.id, user_id=author.id, role="owner", status="active"))
        admin = db.query(User).filter_by(username="admin", role="super_admin").one()
        company_id, author_id, admin_id = int(company.id), int(author.id), int(admin.id)
    definition = {"schema_version": 1, "runtime": "prompt", "system_prompt": "旧版已审核指令"}
    with factory() as db:
        draft = assets.create_draft(
            db, company_id, author_id, kind="agent", title="原始版本", definition=definition, request_key=f"d-{suffix}"
        )
        draft_id = int(draft.id)
        pub = assets.request_publication(
            db, company_id, author_id, draft_id, scope="global", expected_version=1, request_key=f"p-{suffix}"
        )
        pub_id = int(pub.id)
    with factory() as db:
        assets.decide_global_publication(db, pub_id, admin_id, approve=True, reason="approved")
    return company_id, author_id, draft_id, pub_id


@pytest.mark.parametrize("action", ["edit", "publish"])
@pytest.mark.parametrize("winner", ["author", "run"])
def test_author_draft_and_publication_operations_serialize_with_personal_run(fresh_chain, monkeypatch, action, winner):
    factory, caller_id, _agent, sends = _setup(fresh_chain, monkeypatch)
    company_id, author_id, draft_id, pub_id = _make_managed_public(factory)
    locked, release, hit = Event(), Event(), Event()
    first_thread, second_thread, observed, sql_seen = [], [], [], []

    def is_draft_lock(statement):
        upper = statement.upper()
        return "enterprise_asset_draft" in statement.lower() and (
            "FOR UPDATE" in upper or "LOCK IN SHARE MODE" in upper or "FOR SHARE" in upper
        )

    def before(conn, _cursor, statement, _params, _ctx, _many):
        if 'enterprise_asset_draft' in statement.lower() and get_ident() in first_thread + second_thread:
            sql_seen.append(statement)
        if second_thread and get_ident() == second_thread[0] and is_draft_lock(statement) and not observed:
            observed.append(conn.connection.driver_connection.thread_id())
            hit.set()

    def after(_conn, _cursor, statement, _params, _ctx, _many):
        if first_thread and get_ident() == first_thread[0] and is_draft_lock(statement) and not locked.is_set():
            locked.set()
            assert release.wait(10)

    def author():
        with factory() as db:
            if action == "edit":
                row = assets.revise_draft(
                    db,
                    company_id,
                    author_id,
                    draft_id,
                    title="新版本",
                    definition={"schema_version": 1, "runtime": "prompt", "system_prompt": "新内容"},
                    expected_version=1,
                    request_key="new-version",
                )
                return row.version_number
            row = assets.request_publication(
                db,
                company_id,
                author_id,
                draft_id,
                scope="global",
                expected_version=1,
                request_key="repeat-publication",
            )
            return row.id

    def run():
        return execution.execute_enterprise_publication(
            factory, owner_id=caller_id, publication_id=pub_id, request_key="run", input_text="你好"
        )

    first_fn, second_fn = (author, run) if winner == "author" else (run, author)

    def first():
        first_thread.append(get_ident())
        return first_fn()

    def second():
        second_thread.append(get_ident())
        return second_fn()

    event.listen(fresh_chain, "before_cursor_execute", before)
    event.listen(fresh_chain, "after_cursor_execute", after)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            if not locked.wait(10):
                first_future.result(timeout=1)
                pytest.fail(f"未观察到先行方读取草稿锁；SQL: {sql_seen}")
            second_future = pool.submit(second)
            try:
                if not hit.wait(10):
                    second_future.result(timeout=1)
                    pytest.fail(f"未观察到后行方尝试草稿锁；SQL: {sql_seen}")
                _wait_for_lock(fresh_chain, observed)
            finally:
                release.set()
            first_result, second_result = first_future.result(timeout=20), second_future.result(timeout=20)
    finally:
        release.set()
        event.remove(fresh_chain, "before_cursor_execute", before)
        event.remove(fresh_chain, "after_cursor_execute", after)
    run_result = first_result if winner == "run" else second_result
    author_result = first_result if winner == "author" else second_result
    assert run_result["status"] == "succeeded" and len(sends) == 1
    assert author_result == (2 if action == "edit" else pub_id)
