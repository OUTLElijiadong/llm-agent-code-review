import asyncio
import hashlib
import os
import threading
from concurrent.futures import Future
from tempfile import SpooledTemporaryFile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartParser

from app.api.v1 import wechat_fund_admin
from app.api.v1.wechat_fund_admin import router
from app.core.database import Base, get_db
from app.core.dependencies import require_super_admin
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models import load_all_models
from app.models.billing_ledger import BillingOrder, BillingWallet
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.models.wechat_fund_statement import WechatFundBatch, WechatFundGate
from app.services.billing_statement_gateway import DownloadedProviderStatement
from app.services.billing_statement_service import MAX_STATEMENT_FILE_BYTES
from app.utils.multipart_limits import parse_limited_multipart

RAW = (
    '记账时间,微信支付业务单号,资金流水单号,业务名称,业务类型,收支类型,收支金额(元),账户结余(元),资金变更提交申请人,备注,业务凭证号\n'
    '2026-11-05 08:00:00,wx,flow,交易,交易,收入,1.00,1.00,private,secret,voucher\n'
    '资金流水总笔数,收入笔数,收入金额,支出笔数,支出金额\n1,1,1.00,0,0.00\n'
).encode()


def test_unauthorized_fund_upload_does_not_parse_multipart(monkeypatch):
    calls = []
    original = MultiPartParser.parse

    async def trace_parse(self):
        calls.append('parse')
        return await original(self)

    def deny():
        calls.append('auth')
        raise HTTPException(status_code=403, detail='禁止')

    monkeypatch.setattr(MultiPartParser, 'parse', trace_parse)
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.dependency_overrides[require_super_admin] = deny
    with TestClient(app) as client:
        result = client.post('/api/billing/admin/fund-statements/import',
                             data={'bill_date': '2026-11-05', 'account_type': 'BASIC'},
                             files={'file': ('bill.csv', b'x' * (2 * 1024 * 1024), 'text/csv')})
    assert result.status_code == 403
    assert calls == ['auth']


def test_authenticated_fund_upload_rejects_oversized_file_during_parse():
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')
    app.dependency_overrides[require_super_admin] = lambda: object()
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as client:
        result = client.post('/api/billing/admin/fund-statements/import',
                             data={'bill_date': '2026-11-05', 'account_type': 'BASIC'},
                             files={'file': ('bill.csv', b'x' * (MAX_STATEMENT_FILE_BYTES + 1), 'text/csv')})
    assert result.status_code == 413


@pytest.mark.parametrize('headers,content', [
    ({}, b''),
    ({'content-type': 'application/json'}, b'{}'),
    ({'content-type': 'text/plain; boundary=abc'}, b'--abc--\r\n'),
    ({'content-type': 'multipart/form-data-extra; boundary=abc'}, b'--abc--\r\n'),
])
def test_authenticated_upload_rejects_non_multipart_content_type(headers, content):
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')
    app.dependency_overrides[require_super_admin] = lambda: object()
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.post('/api/billing/admin/fund-statements/import', headers=headers, content=content)
    assert result.status_code == 415


def test_authenticated_multipart_missing_boundary_is_a_client_error():
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')
    app.dependency_overrides[require_super_admin] = lambda: object()
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as client:
        result = client.post('/api/billing/admin/fund-statements/import',
                             headers={'content-type': 'multipart/form-data'}, content=b'')
    assert result.status_code == 400


@pytest.mark.parametrize('content_type,body', [
    ('multipart/form-data; boundary=abc', b'not-the-declared-boundary secret-user-content'),
    ('multipart/form-data; boundary=', b'bad'),
    ('multipart/form-data; boundary=""', b'bad'),
])
def test_malformed_multipart_is_safe_client_error(content_type, body):
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')
    app.dependency_overrides[require_super_admin] = lambda: object()
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.post('/api/billing/admin/fund-statements/import',
                             headers={'content-type': content_type}, content=body)
    assert result.status_code == 400
    assert 'secret-user-content' not in result.text and 'Traceback' not in result.text


def test_malformed_multipart_closes_opened_spool_file(monkeypatch):
    from starlette import formparsers

    opened = []
    original = formparsers.SpooledTemporaryFile
    def track_file(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(formparsers, 'SpooledTemporaryFile', track_file)
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')
    app.dependency_overrides[require_super_admin] = lambda: object()
    app.dependency_overrides[get_db] = lambda: None
    body = (b'--abc\r\nContent-Disposition: form-data; name="file"; filename="bill.csv"\r\n'
            b'Content-Type: text/csv\r\n\r\nprivate\r\n--abcXbroken\r\n')
    with TestClient(app, raise_server_exceptions=False) as client:
        result = client.post('/api/billing/admin/fund-statements/import',
                             headers={'content-type': 'multipart/form-data; boundary=abc'}, content=body)
    assert result.status_code == 400
    assert opened and all(item.closed for item in opened)


def test_authenticated_upload_retains_multipart_openapi_contract():
    app = FastAPI()
    app.include_router(router, prefix='/api')
    body = app.openapi()['paths']['/api/billing/admin/fund-statements/import']['post']['requestBody']
    schema = body['content']['multipart/form-data']['schema']
    assert body['required'] is True
    assert set(schema['required']) == {'bill_date', 'account_type', 'file'}
    assert schema['properties']['file'] == {'type': 'string', 'format': 'binary'}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [ConnectionError('client disconnected'), asyncio.CancelledError()])
async def test_interrupted_multipart_closes_temporary_file(monkeypatch, failure):
    from starlette import formparsers

    opened = []
    original = formparsers.SpooledTemporaryFile

    def track_file(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, 'SpooledTemporaryFile', track_file)

    async def stream():
        yield (b'--abc\r\nContent-Disposition: form-data; name="file"; filename="bill.csv"\r\n'
               b'Content-Type: text/csv\r\n\r\npartial content')
        raise failure

    with pytest.raises(type(failure)):
        await parse_limited_multipart(Headers({'content-type': 'multipart/form-data; boundary=abc'}),
                                      stream(), max_file_bytes=MAX_STATEMENT_FILE_BYTES, max_files=1)
    assert opened and all(file.closed for file in opened)


@pytest.mark.asyncio
async def test_upload_cancellation_waits_for_worker_before_releasing_resources(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def delayed_worker(*_args):
        started.set()
        assert release.wait(5)
        return {'result': 'finished'}

    monkeypatch.setattr(wechat_fund_admin, '_import_fund_file', delayed_worker)
    engine = create_engine('sqlite://')
    db = sessionmaker(bind=engine)()
    file = UploadFile(file=SpooledTemporaryFile(max_size=65536))
    admin = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=4_000_000_000)
    task = asyncio.create_task(wechat_fund_admin._run_import_safely(None, None, file, db, admin))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await file.close()
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_upload_cancellation_waits_for_real_worker_thread(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def worker(*_args):
        started.set()
        assert release.wait(3)
        finished.set()

    monkeypatch.setattr(wechat_fund_admin, '_import_fund_file', worker)
    engine = create_engine('sqlite://')
    db = sessionmaker(bind=engine)()
    file = UploadFile(file=SpooledTemporaryFile(max_size=65536))
    admin = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=4_000_000_000)
    task = asyncio.create_task(wechat_fund_admin._run_import_safely(None, None, file, db, admin))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    await file.close()
    db.close()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('rolled', [False, True])
async def test_child_task_cancellation_cannot_leave_worker_using_request_session_or_file(monkeypatch, tmp_path, rolled):
    """Even if shutdown cancels the child task, the thread owns its inputs and session."""
    from starlette.requests import Request

    load_all_models()
    engine = create_engine(f'sqlite:///{tmp_path / "fund.sqlite"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as setup:
        admin = User(username='admin', password='x', role='super_admin', status=1, token_version=0)
        role = Role(name='super', code='super_admin', status='active')
        setup.add_all([admin, role, WechatFundGate(id=1)])
        setup.flush()
        setup.add(UserRole(user_id=admin.id, role_id=role.id))
        user_id = admin.id
    db = factory()
    actor = db.get(User, user_id)
    actor._authenticated_token_version = 0
    actor._authenticated_token_expires_at = 4_000_000_000
    header, line, summary_header, _ = RAW.decode().splitlines()
    raw = (('\n'.join([header] + [line] * 1000 + [summary_header, '1000,1000,1000.00,0,0.00']) + '\n')
           .encode()) if rolled else RAW
    assert (len(raw) > 65536) is rolled
    body = (b'--abc\r\nContent-Disposition: form-data; name="bill_date"\r\n\r\n2026-11-05\r\n'
            b'--abc\r\nContent-Disposition: form-data; name="account_type"\r\n\r\nBASIC\r\n'
            b'--abc\r\nContent-Disposition: form-data; name="file"; filename="bill.csv"\r\n'
            b'Content-Type: text/csv\r\n\r\n' + raw + b'\r\n--abc--\r\n')
    delivered = False
    async def receive():
        nonlocal delivered
        if delivered:
            return {'type': 'http.disconnect'}
        delivered = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'query_string': b'',
                       'headers': [(b'content-type', b'multipart/form-data; boundary=abc')]}, receive)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    child = []
    observed = {}
    original_run = wechat_fund_admin._await_import_future
    original_import = wechat_fund_admin._import_fund_file
    async def capture(*args):
        child.append(asyncio.current_task())
        return await original_run(*args)
    def blocked(*args):
        observed['source'] = args[2]
        observed['engine'] = args[3]
        observed['actor'] = args[4]
        started.set()
        try:
            assert release.wait(5)
            return original_import(*args)
        except BaseException as exc:
            observed['error'] = exc
            raise
        finally:
            finished.set()
    monkeypatch.setattr(wechat_fund_admin, '_await_import_future', capture)
    monkeypatch.setattr(wechat_fund_admin, '_import_fund_file', blocked)
    task = asyncio.create_task(wechat_fund_admin.import_fund_statement(request=request, db=db, admin=actor))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        child[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        db.close()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert isinstance(observed['source'], int if rolled else bytes)
    assert observed['engine'] is engine and observed['actor'] is not actor
    assert 'error' not in observed
    with factory() as check:
        assert check.query(WechatFundBatch).count() == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_queued_worker_releases_duplicate_descriptor_and_slot(monkeypatch):
    class PendingExecutor:
        def submit(self, _func, *args):
            self.fd = args[2]
            self.future = Future()
            return self.future
    pending = PendingExecutor()
    monkeypatch.setattr(wechat_fund_admin, '_upload_executor', pending)
    engine = create_engine('sqlite://')
    db = sessionmaker(bind=engine)()
    spool = SpooledTemporaryFile(max_size=65536)
    spool.write(b'x' * 70000)
    spool.seek(0)
    file = UploadFile(file=spool)
    admin = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=4_000_000_000)
    child = []
    original = wechat_fund_admin._await_import_future
    async def capture(future):
        child.append(asyncio.current_task())
        return await original(future)
    monkeypatch.setattr(wechat_fund_admin, '_await_import_future', capture)
    task = asyncio.create_task(wechat_fund_admin._run_import_safely(None, None, file, db, admin))
    while not child:
        await asyncio.sleep(0)
    child[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if pending.future.cancelled():
            break
        await asyncio.sleep(.001)
    assert pending.future.cancelled()
    with pytest.raises(OSError):
        os.fstat(pending.fd)
    assert wechat_fund_admin._upload_slot.acquire(blocking=False)
    wechat_fund_admin._upload_slot.release()
    await file.close()
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_maximum_rolled_upload_read_does_not_block_event_loop(monkeypatch):
    started, release = threading.Event(), threading.Event()
    def worker(_date, _account, source, _engine, _actor):
        with os.fdopen(source, 'rb', closefd=False) as owned:
            assert len(owned.read(MAX_STATEMENT_FILE_BYTES + 1)) == MAX_STATEMENT_FILE_BYTES
        started.set()
        assert release.wait(5)
    monkeypatch.setattr(wechat_fund_admin, '_import_fund_file', worker)
    engine = create_engine('sqlite://')
    db = sessionmaker(bind=engine)()
    spool = SpooledTemporaryFile(max_size=65536)
    spool.write(b'x' * MAX_STATEMENT_FILE_BYTES)
    spool.seek(0)
    file = UploadFile(file=spool)
    admin = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=4_000_000_000)
    task = asyncio.create_task(wechat_fund_admin._run_import_safely(None, None, file, db, admin))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        beat = asyncio.Event()
        asyncio.get_running_loop().call_later(.02, beat.set)
        await asyncio.wait_for(beat.wait(), timeout=.3)
    finally:
        release.set()
        await task
        await file.close()
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_worker_exception_propagates_and_releases_fd_and_slot(monkeypatch):
    seen = {}
    def worker(_date, _account, source, _engine, _actor):
        seen['fd'] = source
        raise RuntimeError('synthetic worker failure')
    monkeypatch.setattr(wechat_fund_admin, '_import_fund_file', worker)
    engine = create_engine('sqlite://')
    db = sessionmaker(bind=engine)()
    spool = SpooledTemporaryFile(max_size=65536)
    spool.write(b'x' * 70000)
    spool.seek(0)
    file = UploadFile(file=spool)
    admin = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=4_000_000_000)
    try:
        with pytest.raises(RuntimeError, match='synthetic worker failure'):
            await wechat_fund_admin._run_import_safely(None, None, file, db, admin)
        with pytest.raises(OSError):
            os.fstat(seen['fd'])
        assert wechat_fund_admin._upload_slot.acquire(blocking=False)
        wechat_fund_admin._upload_slot.release()
    finally:
        await file.close()
        db.close()
        engine.dispose()


def test_fund_import_requires_real_super_admin_and_does_not_modify_payment_data(monkeypatch):
    parse_calls = []
    original_parse = MultiPartParser.parse

    async def trace_parse(self):
        parse_calls.append(True)
        return await original_parse(self)

    monkeypatch.setattr(MultiPartParser, 'parse', trace_parse)
    load_all_models()
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        admin = User(username='admin', password='x', role='super_admin', status=1, token_version=0)
        normal = User(username='normal', password='x', role='user', status=1, token_version=0)
        role = Role(name='super', code='super_admin', status='active')
        db.add_all([admin, normal, role, WechatFundGate(id=1)])
        db.flush()
        db.add(UserRole(user_id=admin.id, role_id=role.id))
        db.add(BillingWallet(owner_type='user', owner_id=normal.id))
        db.add(BillingOrder(order_no='untouched-order', user_id=normal.id, product='personal_credits',
                            amount_fen=100, quantity=1, channel='wechat', status='created'))
        admin_id, normal_id = admin.id, normal.id
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix='/api')
    def session():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = session
    normal_auth = {'Authorization': f'Bearer {create_access_token(normal_id, "user")}' }
    admin_auth = {'Authorization': f'Bearer {create_access_token(admin_id, "super_admin")}' }
    with TestClient(app) as client:
        def upload(headers):
            return client.post('/api/billing/admin/fund-statements/import', headers=headers,
                data={'bill_date': '2026-11-05', 'account_type': 'BASIC'},
                files={'file': ('bill.csv', RAW, 'text/csv')})
        assert upload(normal_auth).status_code == 403
        assert not parse_calls
        assert client.get('/api/billing/admin/fund-statements', headers=normal_auth).status_code == 403
        response = upload(admin_auth)
        assert response.status_code == 200
        assert len(parse_calls) == 1
        batch = response.json()['data']
        assert batch['source_origin'] == 'manual_upload' and batch['provider_hash_type'] is None
        assert batch['verification'] == 'file_totals_only'
        rows = client.get(f"/api/billing/admin/fund-statements/{batch['id']}/rows", headers=admin_auth)
        assert rows.status_code == 200 and rows.json()['data']['items'][0]['amount_fen'] == '100'
        assert 'secret' not in rows.text and 'private' not in rows.text
        batches = client.get('/api/billing/admin/fund-statements', headers=admin_auth)
        assert batches.status_code == 200 and batches.json()['data']['items'][0]['file_versions'] == 1
        # Conflicting active roles must fail closed, matching the platform's
        # single effective-role invariant.
        with factory.begin() as db:
            extra = Role(name='reviewer', code='reviewer', status='active')
            db.add(extra)
            db.flush()
            db.add(UserRole(user_id=admin_id, role_id=extra.id))
        assert upload(admin_auth).status_code == 403
        with factory.begin() as db:
            super_role = db.query(Role).filter_by(code='super_admin').one()
            super_role.status = 'disabled'
        assert upload(admin_auth).status_code == 403
    with factory() as db:
        assert db.query(WechatFundBatch).count() == 1
        assert db.query(BillingOrder).filter_by(order_no='untouched-order').one().status == 'created'
        assert db.query(BillingWallet).count() == 1
    engine.dispose()


def test_download_rechecks_original_token_after_network_before_persisting(monkeypatch):
    load_all_models()
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    admin = User(username='admin', password='x', role='super_admin', status=1, token_version=0)
    role = Role(name='super', code='super_admin', status='active')
    db.add_all([admin, role, WechatFundGate(id=1)])
    db.flush()
    db.add(UserRole(user_id=admin.id, role_id=role.id))
    db.commit()
    admin._authenticated_token_version = 0
    admin._authenticated_token_expires_at = 4_000_000_000
    monkeypatch.setattr(wechat_fund_admin.settings, 'wechat_pay_mchid', 'fake-merchant')
    monkeypatch.setattr(wechat_fund_admin.settings, 'wechat_pay_merchant_serial', 'fake-serial')
    monkeypatch.setattr(wechat_fund_admin.settings, 'wechat_pay_public_key_serial', 'fake-platform')
    monkeypatch.setattr('app.api.v1.billing._read_key', lambda *_args: b'test-key')
    def download(**_kwargs):
        admin.token_version = 1
        db.flush()
        return DownloadedProviderStatement(raw=RAW, file_name='bill.csv', source_origin='wechat_api',
                                           provider_hash_type='SHA1',
                                           provider_hash_value=hashlib.sha1(RAW, usedforsecurity=False).hexdigest())
    monkeypatch.setattr(wechat_fund_admin.billing_statement_gateway, 'download_wechat_fund_statement', download)
    with pytest.raises(wechat_fund_admin.HTTPException) as denied:
        wechat_fund_admin.download_fund_statement(
            wechat_fund_admin.FundDownload(bill_date='2026-11-05', account_type='BASIC'), db=db, admin=admin,
        )
    assert denied.value.status_code == 403
    assert db.query(WechatFundBatch).count() == 0
    db.close()
    engine.dispose()
