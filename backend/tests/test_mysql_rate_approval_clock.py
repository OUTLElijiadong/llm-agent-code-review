"""An approval must recheck its lead time after acquiring the catalog lock."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from time import monotonic, sleep

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingRateCatalog
from app.services import billing_rate_catalog_service as rates

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("case", ["lead", "source_expiry"])
def test_mysql_approval_rechecks_clock_after_catalog_lock_wait(fresh_chain, case):
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    effective = before + timedelta(hours=1, seconds=1)
    source = (before - timedelta(minutes=1) if case == "lead"
              else before - timedelta(days=30) + timedelta(hours=2))
    with sessions.begin() as db:
        draft = rates.create_draft(
            db, actor_id=77101, effective_at=effective,
            source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            source_checked_at=source,
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=source,
            fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            fx_source_checked_at=source, fx_cny_per_usd_scaled=67_168,
            change_note="隔离 MySQL 审批等待时钟", items=rates.baseline_items(), now=before,
        )
        catalog_id = int(draft.id)

    # Once the waiting approval has read its initial clock, move the injected
    # clock past the one-hour lead while its SELECT FOR UPDATE is blocked.
    later = Event()
    def clock():
        elapsed = timedelta(seconds=2) if case == "lead" else timedelta(hours=2, seconds=1)
        return before + (elapsed if later.is_set() else timedelta())
    started, thread_id, connection_id = Event(), [], []

    def observe(conn, _cursor, statement, _params, _context, _many):
        if thread_id and get_ident() == thread_id[0] and "FOR UPDATE" in statement.upper():
            connection_id.append(conn.connection.driver_connection.thread_id())
            started.set()

    def approve():
        thread_id.append(get_ident())
        with sessions.begin() as db:
            return rates.approve_draft(db, catalog_id=catalog_id, actor_id=77102, clock=clock)

    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with sessions() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.query(BillingRateCatalog).filter_by(id=catalog_id).with_for_update().one()
            future = pool.submit(approve)
            try:
                assert started.wait(10)
                deadline = monotonic() + 10
                while monotonic() < deadline:
                    with fresh_chain.connect() as observer:
                        waiting = observer.execute(text(
                            "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w "
                            "JOIN performance_schema.threads AS t ON t.thread_id=w.requesting_thread_id "
                            "WHERE t.processlist_id=:id"
                        ), {"id": connection_id[0]}).scalar_one()
                    if waiting:
                        break
                    sleep(0.02)
                else:
                    pytest.fail("未观察到审批事务等待目录锁")
                later.set()
            finally:
                blocker.commit()
            expected = "距离生效不足一小时" if case == "lead" else "来源已过期"
            with pytest.raises(rates.RateCatalogError, match=expected):
                future.result(timeout=10)
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe)
    with sessions() as db:
        assert db.query(BillingRateCatalog).filter_by(id=catalog_id).one().status == "draft"
