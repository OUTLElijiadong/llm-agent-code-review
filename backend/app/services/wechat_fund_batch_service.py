"""Store minimized fund-flow evidence, without changing payment or wallet state."""

import hashlib
import hmac
import re
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.wechat_fund_statement import WechatFundBatch, WechatFundRow
from app.services.billing_statement_service import MAX_STATEMENT_FILE_BYTES, StatementImportError
from app.services.wechat_fund_statement import parse_wechat_fund_statement


def import_fund_statement(db: Session, *, actor_id: int, account_type: str, bill_date: str | date,
                          raw: bytes, source_origin: str, provider_hash_type: str | None = None,
                          provider_hash_value: str | None = None,
                          merchant_fingerprint: str = 'manual') -> WechatFundBatch:
    """Caller serializes imports and reauthorizes the actor before this write."""
    if account_type not in {'BASIC', 'OPERATION', 'FEES'}:
        raise StatementImportError('资金账户类型无效')
    if source_origin not in {'manual_upload', 'wechat_api'}:
        raise StatementImportError('资金账单来源无效')
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_STATEMENT_FILE_BYTES:
        raise StatementImportError('资金账单为空或超过 12 MiB')
    if source_origin == 'wechat_api':
        if (re.fullmatch(r'[0-9a-f]{64}', merchant_fingerprint) is None
                or provider_hash_type != 'SHA1' or not isinstance(provider_hash_value, str)
                or re.fullmatch(r'[0-9a-fA-F]{40}', provider_hash_value) is None
                or not hmac.compare_digest(hashlib.sha1(raw, usedforsecurity=False).hexdigest(),
                                           provider_hash_value.lower())):
            raise StatementImportError('微信资金账单摘要校验失败')
    elif provider_hash_type is not None or provider_hash_value is not None or merchant_fingerprint != 'manual':
        raise StatementImportError('手工导入不能标记为渠道摘要已验证')
    parsed = parse_wechat_fund_statement(raw=raw, bill_date=bill_date)
    for record in parsed.records:
        if any(len(value) > 128 for value in (record.provider_business_no, record.funding_flow_no,
                                               record.business_name, record.business_type,
                                               record.business_voucher_no)):
            raise StatementImportError('资金账单业务字段超过保存长度')
    existing = db.query(WechatFundBatch).filter_by(
        bill_date=parsed.bill_date, account_type=account_type,
        source_origin=source_origin, merchant_fingerprint=merchant_fingerprint, file_sha256=parsed.file_sha256,
    ).populate_existing().with_for_update().first()
    if existing is not None:
        db.add(AuditLog(actor_id=actor_id, action='wechat_fund_import_repeat',
                        target_type='wechat_fund_batch', target_id=str(existing.id), status='success'))
        return existing
    batch = WechatFundBatch(
        bill_date=parsed.bill_date, account_type=account_type, source_origin=source_origin,
        merchant_fingerprint=merchant_fingerprint,
        file_sha256=parsed.file_sha256, file_size=len(raw), provider_hash_type=provider_hash_type,
        provider_hash_value=provider_hash_value.lower() if provider_hash_value else None,
        row_count=len(parsed.records), income_count=parsed.income_count, income_fen=parsed.income_fen,
        expense_count=parsed.expense_count, expense_fen=parsed.expense_fen,
        fee_debit_fen=parsed.fee_debit_fen, fee_refund_fen=parsed.fee_refund_fen,
        imported_by=actor_id, imported_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(batch)
    db.flush()
    # Keep the session from retaining 100,000 mapped objects for one file.
    for start in range(0, len(parsed.records), 1000):
        db.bulk_insert_mappings(WechatFundRow, [{
            'batch_id': batch.id, 'source_line_number': record.source_line_number, 'booked_at': record.booked_at,
            'provider_business_no': record.provider_business_no, 'funding_flow_no': record.funding_flow_no,
            'business_name': record.business_name, 'business_type': record.business_type,
            'direction': record.direction, 'amount_fen': record.amount_fen, 'balance_fen': record.balance_fen,
            'business_voucher_no': record.business_voucher_no,
        } for record in parsed.records[start:start + 1000]])
    db.add(AuditLog(actor_id=actor_id, action='wechat_fund_import', target_type='wechat_fund_batch',
                    target_id=str(batch.id), status='success'))
    db.flush()
    return batch


def count_fund_batches(db: Session) -> int:
    return db.query(WechatFundBatch).count()


def list_fund_rows(db: Session, batch_id: int, *, limit: int, offset: int) -> list[WechatFundRow]:
    return (db.query(WechatFundRow).filter_by(batch_id=batch_id)
            .order_by(WechatFundRow.source_line_number).limit(limit).offset(offset).all())
