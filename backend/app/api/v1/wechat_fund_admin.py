"""Platform-only fund-statement evidence; does not reconcile or settle orders."""

import asyncio
import hashlib
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError as PydanticValidationError
from python_multipart.exceptions import MultipartParseError
from sqlalchemy import func, tuple_, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_super_admin
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.models.wechat_fund_statement import WechatFundBatch, WechatFundGate
from app.schemas.common import Resp, StrictInputModel
from app.services import billing_statement_gateway, wechat_fund_batch_service
from app.services.billing_payment_verification import PaymentVerificationError
from app.services.billing_statement_service import MAX_STATEMENT_FILE_BYTES, MAX_STATEMENT_OFFSET, StatementImportError
from app.utils.multipart_limits import FILE_SPOOL_MEMORY_BYTES, parse_limited_multipart

router = APIRouter(prefix='/billing/admin/fund-statements', tags=['微信资金账单'])
_parse_lock = threading.Lock()
_upload_slot = threading.BoundedSemaphore(1)
_upload_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wechat-fund-import')


@dataclass(frozen=True)
class _ActorSnapshot:
    id: int
    _authenticated_token_version: int
    _authenticated_token_expires_at: int


def _snapshot_actor(user: User) -> _ActorSnapshot:
    version = getattr(user, '_authenticated_token_version', None)
    expires_at = getattr(user, '_authenticated_token_expires_at', None)
    if type(user.id) is not int or type(version) is not int or type(expires_at) is not int:
        raise HTTPException(status_code=403, detail='登录状态已失效，请重新登录')
    snapshot = _ActorSnapshot(user.id, version, expires_at)
    _check_expiry(snapshot)
    return snapshot


class FundDownload(StrictInputModel):
    bill_date: date
    account_type: Literal['BASIC', 'OPERATION', 'FEES']


def _check_expiry(user: User | _ActorSnapshot) -> None:
    expires_at = getattr(user, '_authenticated_token_expires_at', None)
    if type(expires_at) is not int or expires_at <= int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=403, detail='登录状态已过期，请重新登录')


def _authorize_write(db: Session, user: User | _ActorSnapshot) -> None:
    version = getattr(user, '_authenticated_token_version', None)
    _check_expiry(user)
    if type(version) is not int:
        raise HTTPException(status_code=403, detail='登录状态已失效，请重新登录')
    current = db.query(User).filter_by(id=user.id).populate_existing().with_for_update().one_or_none()
    roles = (db.query(Role.code, Role.status).select_from(UserRole).join(Role, Role.id == UserRole.role_id)
             .filter(UserRole.user_id == user.id).with_for_update().all())
    if (current is None or current.status != 1 or current.username != 'admin' or current.role != 'super_admin'
            or int(current.token_version or 0) != version
            or not roles or {code for code, status in roles if status == 'active'} != {'super_admin'}):
        raise HTTPException(status_code=403, detail='平台最高管理员身份或登录状态已失效')


@contextmanager
def _serialized_import(db: Session):
    if not _parse_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='已有资金账单正在处理，请稍后重试')
    try:
        dialect = db.get_bind().dialect.name
        try:
            if dialect == 'sqlite':
                # Obtain a database write lock, not merely the in-process lock.
                result = db.execute(update(WechatFundGate).where(WechatFundGate.id == 1).values(id=1))
                if result.rowcount != 1:
                    raise HTTPException(status_code=503, detail='资金账单处理锁未初始化')
            elif dialect == 'mysql':
                gate = db.query(WechatFundGate).filter_by(id=1).with_for_update(nowait=True).one_or_none()
                if gate is None:
                    raise HTTPException(status_code=503, detail='资金账单处理锁未初始化')
            else:
                raise HTTPException(status_code=503, detail='当前数据库不支持安全的账单处理锁')
        except OperationalError as exc:
            code = getattr(exc.orig, 'args', (None,))[0]
            if code in (3572, 1205) or (dialect == 'sqlite' and 'locked' in str(exc.orig).lower()):
                raise HTTPException(status_code=409, detail='已有资金账单正在处理，请稍后重试') from exc
            raise
        yield
    finally:
        _parse_lock.release()


def _view(batch: WechatFundBatch) -> dict:
    return {
        'id': batch.id, 'bill_date': batch.bill_date, 'account_type': batch.account_type,
        'source_origin': batch.source_origin, 'file_sha256': batch.file_sha256, 'file_size': batch.file_size,
        'provider_hash_type': batch.provider_hash_type, 'row_count': batch.row_count,
        'income_count': batch.income_count, 'income_fen': str(batch.income_fen),
        'expense_count': batch.expense_count, 'expense_fen': str(batch.expense_fen),
        'fee_debit_fen': str(batch.fee_debit_fen), 'fee_refund_fen': str(batch.fee_refund_fen),
        'imported_by': batch.imported_by, 'imported_at': batch.imported_at,
        'verification': 'file_totals_only',
    }


def _import_fund_file(bill_date: date, account_type: str, source: bytes | int,
                      engine: Engine, actor: _ActorSnapshot):
    # The worker owns its session and immutable identity; it never accesses
    # the request-scoped session, form, UploadFile, or ORM user.
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as db:
        try:
            with _serialized_import(db):
                if isinstance(source, bytes):
                    raw = source
                else:
                    with os.fdopen(source, 'rb', closefd=False) as owned_file:
                        owned_file.seek(0)
                        raw = owned_file.read(MAX_STATEMENT_FILE_BYTES + 1)
                if len(raw) > MAX_STATEMENT_FILE_BYTES:
                    raise HTTPException(status_code=413, detail='资金账单文件超过 12 MiB')
                _authorize_write(db, actor)
                batch = wechat_fund_batch_service.import_fund_statement(
                    db, actor_id=actor.id, account_type=account_type, bill_date=bill_date,
                    raw=raw, source_origin='manual_upload',
                )
                _check_expiry(actor)
                result = _view(batch)
                db.commit()
                return Resp(data=result)
        except StatementImportError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            db.rollback()
            raise


async def _await_import_future(future: Future):
    return await asyncio.wrap_future(future)


async def _run_import_safely(bill_date: date, account_type: str, file: UploadFile, db: Session, admin: User):
    """Submit independently owned inputs; cleanup remains safe if either task is cancelled."""
    actor = _snapshot_actor(admin)
    engine = db.get_bind()
    if not isinstance(engine, Engine):
        raise HTTPException(status_code=503, detail='资金账单需要独立数据库连接')
    if not _upload_slot.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='已有资金账单正在处理，请稍后重试')
    source: bytes | int | None = None
    try:
        if not getattr(file.file, '_rolled', True):
            # The in-memory spool is capped at 64 KiB by LimitedMultiPartParser.
            source = file.file.read(FILE_SPOOL_MEMORY_BYTES + 1)
            if len(source) > FILE_SPOOL_MEMORY_BYTES:
                raise HTTPException(status_code=413, detail='资金账单内存文件超过限制')
        else:
            # A duplicate descriptor keeps the already bounded temporary file
            # accessible after the request-owned form closes.
            source = os.dup(file.file.fileno())
        future = _upload_executor.submit(_import_fund_file, bill_date, account_type, source, engine, actor)
    except BaseException:
        if isinstance(source, int):
            os.close(source)
        _upload_slot.release()
        raise

    def cleanup(_future: Future) -> None:
        try:
            if isinstance(source, int):
                os.close(source)
        finally:
            _upload_slot.release()

    # The concurrent future owns the fd even if its asyncio waiter is
    # cancelled before the thread starts. The callback runs in both cases.
    future.add_done_callback(cleanup)
    task = asyncio.create_task(_await_import_future(future))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Waiting avoids abandoning a normal request cancellation. If the
        # event loop also cancels the child task, the worker still owns every
        # resource it needs and its concurrent-future callback cleans up.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()  # Consume a worker error when the request was cancelled.
        raise


@router.post('/import', openapi_extra={
    'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': {
        'type': 'object', 'required': ['bill_date', 'account_type', 'file'], 'properties': {
            'bill_date': {'type': 'string', 'format': 'date'},
            'account_type': {'type': 'string', 'enum': ['BASIC', 'OPERATION', 'FEES']},
            'file': {'type': 'string', 'format': 'binary'},
        },
    }}}},
})
async def import_fund_statement(request: Request, db: Session = Depends(get_db),
                                admin: User = Depends(require_super_admin)):
    # Manual streaming parse deliberately follows authentication. FastAPI's
    # File/Form parameter handling parses the entire body before dependencies.
    content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if content_type != 'multipart/form-data':
        raise HTTPException(status_code=415, detail='资金账单仅接受 multipart/form-data')
    try:
        form = await parse_limited_multipart(
            request.headers, request.stream(), max_file_bytes=MAX_STATEMENT_FILE_BYTES, max_files=1,
        )
    except MultipartParseError as exc:
        # The parser has already closed pending spools; do not echo input or
        # log a stack containing request data for a malformed client body.
        raise HTTPException(status_code=400, detail='资金账单 multipart 内容无效') from exc
    try:
        if any(len(form.getlist(name)) != 1 for name in ('bill_date', 'account_type', 'file')):
            raise HTTPException(status_code=422, detail='资金账单日期、账户和文件必须各提交一次')
        file = form.get('file')
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=422, detail='资金账单文件无效')
        try:
            payload = FundDownload.model_validate({
                'bill_date': form.get('bill_date'), 'account_type': form.get('account_type'),
            })
        except PydanticValidationError as exc:
            raise HTTPException(status_code=422, detail='资金账单日期或账户无效') from exc
        # Database and file work stays in the worker pool; no SQL lock is held
        # while the event loop receives the multipart body.
        return await _run_import_safely(payload.bill_date, payload.account_type, file, db, admin)
    finally:
        # Parsing has completed, so no writer remains; close the request's
        # spool synchronously so repeated cancellation cannot interrupt it.
        for _, item in form.multi_items():
            if isinstance(item, UploadFile):
                item.file.close()


@router.post('/download')
def download_fund_statement(payload: FundDownload, db: Session = Depends(get_db),
                            admin: User = Depends(require_super_admin)):
    from app.api.v1.billing import _read_key
    try:
        with _serialized_import(db):
            if not all((settings.wechat_pay_mchid, settings.wechat_pay_merchant_serial,
                        settings.wechat_pay_public_key_serial)):
                raise PaymentVerificationError('微信支付商户身份尚未配置')
            downloaded = billing_statement_gateway.download_wechat_fund_statement(
                mchid=settings.wechat_pay_mchid, merchant_serial=settings.wechat_pay_merchant_serial,
                merchant_private_key_pem=_read_key(settings.wechat_pay_merchant_private_key_path, '微信商户私钥'),
                platform_public_key_pem=_read_key(settings.wechat_pay_public_key_path, '微信支付公钥'),
                platform_serial=settings.wechat_pay_public_key_serial,
                bill_date=payload.bill_date.isoformat(), account_type=payload.account_type,
            )
            _authorize_write(db, admin)
            merchant_fingerprint = hashlib.sha256(settings.wechat_pay_mchid.encode()).hexdigest()
            batch = wechat_fund_batch_service.import_fund_statement(
                db, actor_id=int(admin.id), account_type=payload.account_type, bill_date=payload.bill_date,
                raw=downloaded.raw, source_origin='wechat_api', provider_hash_type=downloaded.provider_hash_type,
                provider_hash_value=downloaded.provider_hash_value, merchant_fingerprint=merchant_fingerprint,
            )
            _check_expiry(admin)
            result = _view(batch)
            db.commit()
            return Resp(data=result)
    except PaymentVerificationError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except billing_statement_gateway.StatementGatewayInputError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except billing_statement_gateway.StatementGatewayError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except StatementImportError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.get('')
def list_fund_batches(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=MAX_STATEMENT_OFFSET),
                      db: Session = Depends(get_db), _admin: User = Depends(require_super_admin)):
    query = db.query(WechatFundBatch)
    total = query.count()
    rows = query.order_by(WechatFundBatch.bill_date.desc(), WechatFundBatch.id.desc()).offset(offset).limit(limit).all()
    keys = {(row.bill_date, row.account_type) for row in rows}
    versions = {}
    if keys:
        versions = {(day, account): count for day, account, count in db.query(
            WechatFundBatch.bill_date, WechatFundBatch.account_type,
            func.count(func.distinct(WechatFundBatch.file_sha256)),
        ).filter(tuple_(WechatFundBatch.bill_date, WechatFundBatch.account_type).in_(keys))
            .group_by(WechatFundBatch.bill_date, WechatFundBatch.account_type).all()}
    return Resp(data={'items': [{**_view(row), 'file_versions': versions.get((row.bill_date, row.account_type), 1)}
                                for row in rows], 'total': total, 'limit': limit, 'offset': offset})


@router.get('/{batch_id}/rows')
def list_fund_rows(batch_id: int, limit: int = Query(100, ge=1, le=500),
                   offset: int = Query(0, ge=0, le=MAX_STATEMENT_OFFSET), db: Session = Depends(get_db),
                   _admin: User = Depends(require_super_admin)):
    if db.get(WechatFundBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail='资金账单批次不存在')
    rows = wechat_fund_batch_service.list_fund_rows(db, batch_id, limit=limit, offset=offset)
    return Resp(data={'items': [{
        'id': row.id, 'source_line_number': row.source_line_number, 'booked_at': row.booked_at,
        'provider_business_no': row.provider_business_no, 'funding_flow_no': row.funding_flow_no,
        'business_name': row.business_name, 'business_type': row.business_type, 'direction': row.direction,
        'amount_fen': str(row.amount_fen), 'balance_fen': str(row.balance_fen),
        'business_voucher_no': row.business_voucher_no,
    } for row in rows], 'limit': limit, 'offset': offset})
