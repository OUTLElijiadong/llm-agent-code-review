"""Fund evidence locking and authentication against a disposable MySQL only."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Event

import pytest
import test_fresh_database_chain as chain
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from starlette.formparsers import MultiPartParser

from app.api.v1 import wechat_fund_admin
from app.core.database import get_db
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models.user import User
from app.models.wechat_fund_statement import WechatFundBatch, WechatFundGate
from app.services import wechat_fund_batch_service
from app.services.billing_statement_gateway import DownloadedProviderStatement

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url
RAW = (
    '记账时间,微信支付业务单号,资金流水单号,业务名称,业务类型,收支类型,收支金额(元),账户结余(元),资金变更提交申请人,备注,业务凭证号\n'
    '2026-11-05 08:00:00,wx,flow,交易,交易,收入,1.00,1.00,private,secret,voucher\n'
    '资金流水总笔数,收入笔数,收入金额,支出笔数,支出金额\n1,1,1.00,0,0.00\n'
).encode()


def test_cross_connection_import_gate_is_nowait_and_recovers(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    first, second = factory(), factory()
    try:
        assert first.query(WechatFundGate).filter_by(id=1).with_for_update().one()
        with pytest.raises(HTTPException) as busy:
            with wechat_fund_admin._serialized_import(second):
                pass
        assert busy.value.status_code == 409
        second.rollback()
        first.rollback()
        with wechat_fund_admin._serialized_import(second):
            pass
        second.rollback()
    finally:
        first.close()
        second.close()


def test_old_snapshot_after_logout_cannot_authorize_fund_import(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    old, concurrent = factory(), factory()
    try:
        actor = old.query(User).filter_by(username='admin').one()
        version = int(actor.token_version or 0)
        actor._authenticated_token_version = version
        actor._authenticated_token_expires_at = int(datetime.now(timezone.utc).timestamp()) + 600
        live = concurrent.query(User).filter_by(id=actor.id).one()
        live.token_version = version + 1
        concurrent.commit()
        with pytest.raises(HTTPException) as denied:
            wechat_fund_admin._authorize_write(old, actor)
        assert denied.value.status_code == 403
    finally:
        old.rollback()
        concurrent.rollback()
        old.close()
        concurrent.close()


def test_old_snapshot_same_file_import_returns_committed_batch(fresh_chain):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    old, concurrent = factory(), factory()
    try:
        assert old.query(WechatFundBatch).count() == 0
        first = wechat_fund_batch_service.import_fund_statement(
            concurrent, actor_id=1, account_type='BASIC', bill_date=date(2026, 11, 5),
            raw=RAW, source_origin='manual_upload',
        )
        first_id = first.id
        concurrent.commit()
        found = wechat_fund_batch_service.import_fund_statement(
            old, actor_id=1, account_type='BASIC', bill_date=date(2026, 11, 5),
            raw=RAW, source_origin='manual_upload',
        )
        assert found.id == first_id
        old.rollback()
    finally:
        old.close()
        concurrent.close()


def test_real_jwt_download_discards_result_if_login_revoked_during_network(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        user = setup.query(User).filter_by(username='admin').one()
        user_id, version = user.id, int(user.token_version or 0)
    app = FastAPI()
    register_handlers(app)
    app.include_router(wechat_fund_admin.router, prefix='/api')
    def session():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(wechat_fund_admin.settings, 'wechat_pay_mchid', 'fake-merchant')
    monkeypatch.setattr(wechat_fund_admin.settings, 'wechat_pay_merchant_serial', 'fake-serial')
    monkeypatch.setattr(wechat_fund_admin.settings, 'wechat_pay_public_key_serial', 'fake-platform')
    monkeypatch.setattr('app.api.v1.billing._read_key', lambda *_args: b'test-key')
    def network(**_kwargs):
        with factory.begin() as db:
            current = db.get(User, user_id)
            current.token_version = version + 1
        return DownloadedProviderStatement(raw=RAW, file_name='fund.csv', source_origin='wechat_api',
                                           provider_hash_type='SHA1',
                                           provider_hash_value=hashlib.sha1(RAW, usedforsecurity=False).hexdigest())
    monkeypatch.setattr(wechat_fund_admin.billing_statement_gateway, 'download_wechat_fund_statement', network)
    token = create_access_token(user_id, 'super_admin', token_version=version)
    with TestClient(app) as client:
        response = client.post('/api/billing/admin/fund-statements/download',
                               headers={'Authorization': f'Bearer {token}'},
                               json={'bill_date': '2026-11-05', 'account_type': 'BASIC'})
    assert response.status_code == 403
    with factory() as db:
        assert db.query(WechatFundBatch).filter_by(source_origin='wechat_api').count() == 0


def test_real_jwt_upload_refuses_logout_while_worker_is_delayed(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        user = setup.query(User).filter_by(username='admin').one()
        user_id, version = user.id, int(user.token_version or 0)
    app = FastAPI()
    register_handlers(app)
    app.include_router(wechat_fund_admin.router, prefix='/api')
    def session():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = session
    started, release = Event(), Event()
    original = wechat_fund_admin._import_fund_file
    def delayed(*args):
        started.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(wechat_fund_admin, '_import_fund_file', delayed)
    token = create_access_token(user_id, 'super_admin', token_version=version)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, '/api/billing/admin/fund-statements/import',
            headers={'Authorization': f'Bearer {token}'},
            data={'bill_date': '2026-11-05', 'account_type': 'FEES'},
            files={'file': ('fund.csv', RAW, 'text/csv')})
        try:
            assert started.wait(5)
            with factory.begin() as db:
                db.get(User, user_id).token_version = version + 1
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 403
    with factory() as db:
        assert db.query(WechatFundBatch).filter_by(source_origin='manual_upload', account_type='FEES').count() == 0


def test_real_jwt_upload_authenticates_before_parsing(fresh_chain, monkeypatch):
    factory = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with factory() as db:
        user = db.query(User).filter_by(username='admin').one()
        user_id, version = user.id, int(user.token_version or 0)
    app = FastAPI()
    register_handlers(app)
    app.include_router(wechat_fund_admin.router, prefix='/api')

    def session():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    calls = []
    original = MultiPartParser.parse

    async def trace_parse(self):
        calls.append(True)
        return await original(self)

    monkeypatch.setattr(MultiPartParser, 'parse', trace_parse)
    token = create_access_token(user_id, 'super_admin', token_version=version)
    with TestClient(app) as client:
        def upload(token_value):
            return client.post('/api/billing/admin/fund-statements/import',
                               headers={'Authorization': f'Bearer {token_value}'},
                               data={'bill_date': '2026-11-05', 'account_type': 'BASIC'},
                               files={'file': ('fund.csv', RAW, 'text/csv')})
        assert upload('invalid').status_code == 401
        assert not calls
        response = upload(token)
        assert response.status_code == 200
        assert len(calls) == 1
        assert response.json()['data']['verification'] == 'file_totals_only'
