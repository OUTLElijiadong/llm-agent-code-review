from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models import load_all_models
from app.services import wechat_fund_batch_service as funds
from app.services.billing_statement_service import StatementImportError


def sample(amount='1.00', voucher='order-1'):
    return (
        '记账时间,微信支付业务单号,资金流水单号,业务名称,业务类型,收支类型,'
        '收支金额(元),账户结余(元),资金变更提交申请人,备注,业务凭证号\n'
        f'2026-11-05 08:00:00,wx-1,flow-1,交易,交易,收入,{amount},{amount},secret-user,secret-note,{voucher}\n'
        f'资金流水总笔数,收入笔数,收入金额,支出笔数,支出金额\n1,1,{amount},0,0.00\n'
    ).encode()


@pytest.fixture
def db():
    load_all_models()
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_import_is_idempotent_preserves_changed_hash_and_discards_personal_fields(db):
    first = funds.import_fund_statement(db, actor_id=1, account_type='BASIC', bill_date=date(2026, 11, 5),
                                        raw=sample(), source_origin='manual_upload')
    same = funds.import_fund_statement(db, actor_id=1, account_type='BASIC', bill_date=date(2026, 11, 5),
                                       raw=sample(), source_origin='manual_upload')
    second = funds.import_fund_statement(db, actor_id=1, account_type='BASIC', bill_date=date(2026, 11, 5),
                                         raw=sample('2.00'), source_origin='manual_upload')
    db.flush()
    assert same.id == first.id and second.id != first.id
    assert first.provider_hash_type is None and first.provider_hash_value is None
    rows = funds.list_fund_rows(db, first.id, limit=10, offset=0)
    assert len(rows) == 1 and rows[0].amount_fen == 100
    assert 'secret-note' not in repr(rows[0].__dict__) and 'secret-user' not in repr(rows[0].__dict__)


def test_invalid_or_forged_provider_hash_never_creates_batch(db):
    with pytest.raises(StatementImportError):
        funds.import_fund_statement(db, actor_id=1, account_type='BASIC', bill_date=date(2026, 11, 5),
                                    raw=sample(), source_origin='wechat_api', provider_hash_type='SHA1',
                                    provider_hash_value='0' * 40)
    assert funds.count_fund_batches(db) == 0


def test_bad_summary_does_not_create_partial_batch(db):
    raw = sample().replace(b'1,1,1.00,0,0.00', b'1,1,2.00,0,0.00')
    with pytest.raises(StatementImportError):
        funds.import_fund_statement(db, actor_id=1, account_type='BASIC', bill_date=date(2026, 11, 5),
                                    raw=raw, source_origin='manual_upload')
    assert funds.count_fund_batches(db) == 0
