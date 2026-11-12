"""Channel statement parsing and read-only reconciliation.

Imported source files are hashed and discarded. Only the small set of payment,
refund and fee fields needed for reconciliation is retained. This module never
changes an order, refund, wallet, credit batch, or entitlement.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import hmac
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import PurePath

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.request_size_limit import STATEMENT_FILE_MAX_BYTES
from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingRefund,
    BillingStatementBatch,
    BillingStatementReviewEvent,
    BillingStatementRow,
)

STATEMENT_PARSER_VERSION = "statement-v1"
MAX_STATEMENT_FILE_BYTES = STATEMENT_FILE_MAX_BYTES
MAX_STATEMENT_EXPANDED_BYTES = 48 * 1024 * 1024
MAX_STATEMENT_ROWS = 100_000
MAX_STATEMENT_OFFSET = 100_000
MAX_FIELD_CHARS = 4096
_CHINA = timezone(timedelta(hours=8))


class StatementImportError(ValueError):
    """The provider statement is malformed, unsupported or inconsistent."""


@dataclass(frozen=True)
class ParsedStatementRecord:
    source_line_number: int
    event_index: int
    record_type: str
    order_no: str
    provider_trade_id: str | None
    refund_no: str | None
    provider_refund_id: str | None
    provider_status: str
    currency: str
    amount_fen: int
    received_fen: int | None
    fee_micros: int | None
    fee_reported: bool = True


@dataclass(frozen=True)
class ParsedStatement:
    records: tuple[ParsedStatementRecord, ...]
    payment_total_fen: int
    refund_total_fen: int
    received_total_fen: int
    received_missing_count: int
    fee_total_micros: int
    fee_missing_count: int


_ALIASES = {
    "order_no": ("商户订单号", "商家订单号", "out_trade_no", "商户网站订单号"),
    "trade_id": ("微信订单号", "支付宝交易号", "交易号", "trade_no", "transaction_id"),
    "refund_no": ("商户退款单号", "退款单号", "out_refund_no", "out_request_no"),
    "provider_refund_id": ("微信退款单号", "支付宝退款单号", "refund_id"),
    "status": ("交易状态", "订单状态", "状态", "trade_status", "trade_state"),
    "refund_status": ("退款状态", "refund_status", "refund_status_0"),
    "currency": ("货币种类", "币种", "currency"),
    "order_amount": ("订单金额", "总金额", "交易金额", "total_amount", "amount"),
    "settlement_amount": ("应结订单金额", "应结算订单金额", "商家实收", "实收金额", "receipt_amount"),
    "refund_amount": ("申请退款金额", "退款金额", "refund_amount"),
    "coupon_refund": ("充值券退款金额", "免充值券退款金额", "coupon_refund_amount"),
    "fee": ("手续费", "服务费", "fee", "service_fee"),
    "time": ("交易时间", "付款时间", "支付时间", "交易创建时间", "gmt_payment", "time_end"),
}

_SUCCESS_STATES = {"SUCCESS", "TRADE_SUCCESS", "TRADE_FINISHED", "交易成功", "支付成功", "成功"}
_REFUND_STATES = {"REFUND", "退款", "退款成功", "已退款", "REVOKED", "已撤销", "退款中"}
_NON_PAYMENT_STATES = {"TRADE_CLOSED", "交易关闭", "等待买家付款", "未支付", "关闭"}


def _normalized_cell(value: str | None) -> str:
    if value is None:
        return ""
    text = value.strip().lstrip("\ufeff").strip()
    if text.startswith("`"):
        text = text[1:]
    return text.strip()


def _decode_payload(raw: bytes) -> bytes:
    if not isinstance(raw, bytes) or not raw:
        raise StatementImportError("账单文件为空")
    if len(raw) > MAX_STATEMENT_FILE_BYTES:
        raise StatementImportError("账单文件超过 12 MiB 限制")
    if raw.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                csv_files = [item for item in archive.infolist() if not item.is_dir()
                             and PurePath(item.filename).suffix.lower() == ".csv"]
                if len(csv_files) != 1:
                    raise StatementImportError("压缩包必须且只能包含一个 CSV 账单文件")
                entry = csv_files[0]
                if entry.file_size > MAX_STATEMENT_EXPANDED_BYTES:
                    raise StatementImportError("解压后的账单超过 48 MiB 限制")
                if entry.file_size > max(entry.compress_size, 1) * 200:
                    raise StatementImportError("压缩包压缩率异常")
                with archive.open(entry) as stream:
                    expanded = stream.read(MAX_STATEMENT_EXPANDED_BYTES + 1)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise StatementImportError("账单 ZIP 文件无效") from exc
        if len(expanded) > MAX_STATEMENT_EXPANDED_BYTES:
            raise StatementImportError("解压后的账单超过 48 MiB 限制")
        return expanded
    if raw.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                expanded = stream.read(MAX_STATEMENT_EXPANDED_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise StatementImportError("账单 GZIP 文件无效") from exc
        if len(expanded) > MAX_STATEMENT_EXPANDED_BYTES:
            raise StatementImportError("解压后的账单超过 48 MiB 限制")
        return expanded
    return raw


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            if "\x00" in text:
                raise StatementImportError("账单包含非法 NUL 字符")
            return text
        except UnicodeDecodeError:
            continue
    raise StatementImportError("账单编码不受支持；请使用 UTF-8 或 GB18030 CSV")


def _find_header(rows: list[list[str]], *, channel: str) -> tuple[int, dict[str, int]]:
    aliases = {key: {name.casefold() for name in values} for key, values in _ALIASES.items()}
    for row_index, row in enumerate(rows[:30]):
        if not row or len(row) > 256:
            continue
        normalized = [_normalized_cell(cell).casefold() for cell in row]
        indexes = {}
        for field, names in aliases.items():
            for index, cell in enumerate(normalized):
                if cell in names:
                    indexes[field] = index
                    break
        if {"order_no", "trade_id", "status"}.issubset(indexes) and (
            "order_amount" in indexes or "refund_amount" in indexes
        ):
            return row_index, indexes
    if channel == "alipay":
        raise StatementImportError("支付宝 CSV 表头不属于已支持的 trade 账单格式；请提供官方原始 CSV，不要手工改列名")
    raise StatementImportError("微信支付 CSV 表头不属于支持的交易账单格式")


def _cell(row: list[str], indexes: dict[str, int], field: str) -> str:
    index = indexes.get(field)
    if index is None or index >= len(row):
        return ""
    value = _normalized_cell(row[index])
    if len(value) > MAX_FIELD_CHARS:
        raise StatementImportError("账单字段长度超过限制")
    return value


def _money_fen(value: str, *, required: bool) -> int | None:
    text = value.strip().replace("¥", "").replace("￥", "").replace("元", "").replace(",", "")
    if not text:
        if required:
            raise StatementImportError("账单金额字段为空")
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise StatementImportError("账单金额格式无效") from exc
    if not amount.is_finite():
        raise StatementImportError("账单金额不能是非有限值")
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        raise StatementImportError("订单和退款金额必须精确到分")
    result = int(scaled)
    return -result if negative else result


def _fee_micros(value: str) -> int | None:
    text = value.strip().replace("¥", "").replace("￥", "").replace("元", "").replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise StatementImportError("手续费格式无效") from exc
    if not amount.is_finite():
        raise StatementImportError("手续费不能是非有限值")
    scaled = amount * 1_000_000
    if scaled != scaled.to_integral_value():
        raise StatementImportError("手续费精度超过百万分之一元")
    return int(scaled)


def _amount_for_record(row: list[str], indexes: dict[str, int], *, field: str) -> int | None:
    value = _cell(row, indexes, field)
    return _money_fen(value, required=False)


def _provider_rows_from_payload(payload: bytes) -> list[list[str]]:
    text = _decode_text(payload)
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except (csv.Error, UnicodeError) as exc:
        raise StatementImportError("CSV 账单格式无效") from exc
    if len(rows) > MAX_STATEMENT_ROWS + 40:
        raise StatementImportError("账单行数超过 100,000 条限制")
    if any(any(len(cell) > MAX_FIELD_CHARS for cell in row) for row in rows):
        raise StatementImportError("账单字段长度超过限制")
    return rows


def parse_statement(*, channel: str, bill_type: str, raw: bytes,
                    _decoded_payload: bytes | None = None) -> ParsedStatement:
    """Parse supported provider CSV formats without retaining raw rows or PII."""
    if channel not in {"alipay", "wechat"}:
        raise StatementImportError("支付渠道无效")
    if (channel == "alipay" and bill_type != "trade") or (
        channel == "wechat" and bill_type not in {"ALL", "SUCCESS", "REFUND"}
    ):
        raise StatementImportError("该账单类型不受支持")
    payload = _decoded_payload if _decoded_payload is not None else _decode_payload(raw)
    if not isinstance(payload, bytes) or not payload:
        raise StatementImportError("账单文件为空")
    rows = _provider_rows_from_payload(payload)
    header_index, indexes = _find_header(rows, channel=channel)
    records: list[ParsedStatementRecord] = []
    for line_number, source in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not source or not any(_normalized_cell(cell) for cell in source):
            continue
        first = _normalized_cell(source[0])
        # Provider summary has a different header and is not a transaction detail.
        if first.startswith("总交易单数") or first.startswith("总金额"):
            break
        order_no = _cell(source, indexes, "order_no")
        provider_trade_id = _cell(source, indexes, "trade_id") or None
        refund_no = _cell(source, indexes, "refund_no") or None
        provider_refund_id = _cell(source, indexes, "provider_refund_id") or None
        status = _cell(source, indexes, "status")
        currency = (_cell(source, indexes, "currency") or "CNY").upper()
        if not order_no and not provider_trade_id and not refund_no and not provider_refund_id:
            # Non-transaction metadata preceding the provider detail table.
            continue
        if not status:
            raise StatementImportError(f"第 {line_number} 行缺少交易状态")
        if len(order_no) > 64 or len(provider_trade_id or "") > 128 or len(refund_no or "") > 64:
            raise StatementImportError(f"第 {line_number} 行订单标识超过长度限制")

        order_amount = _amount_for_record(source, indexes, field="order_amount")
        refund_amount = _amount_for_record(source, indexes, field="refund_amount")
        fee = _fee_micros(_cell(source, indexes, "fee"))
        received = _money_fen(_cell(source, indexes, "settlement_amount"), required=False)
        normalized_status = status.strip()
        is_payment = normalized_status in _SUCCESS_STATES
        is_refund = normalized_status in _REFUND_STATES
        refund_aggregate = is_payment and refund_amount is not None and refund_amount > 0
        if not is_payment and not is_refund and normalized_status in _NON_PAYMENT_STATES:
            continue
        if not is_payment and not is_refund and refund_amount is not None and refund_amount > 0:
            # Some Alipay trade exports keep the original payment status on a row
            # that also reports refund totals. The refund is recorded separately.
            is_payment = normalized_status in _SUCCESS_STATES
            is_refund = True
        if not is_payment and not is_refund:
            raise StatementImportError(f"第 {line_number} 行交易状态不受支持")
        if not order_no:
            raise StatementImportError(f"第 {line_number} 行缺少商户订单号")
        if currency != "CNY":
            # Preserve the normalized row for human review, but never treat it as a match.
            pass

        event_index = 0
        if is_payment and (bill_type != "REFUND"):
            if not provider_trade_id:
                raise StatementImportError(f"第 {line_number} 行支付记录缺少渠道交易号")
            if order_amount is None or order_amount <= 0:
                raise StatementImportError(f"第 {line_number} 行支付金额无效")
            records.append(ParsedStatementRecord(
                source_line_number=line_number, event_index=event_index, record_type="payment", order_no=order_no,
                provider_trade_id=provider_trade_id, refund_no=None, provider_refund_id=None,
                provider_status=normalized_status[:32], currency=currency, amount_fen=order_amount,
                received_fen=received, fee_micros=fee, fee_reported=fee is not None,
            ))
            event_index += 1
        if (is_refund or refund_aggregate) and (bill_type != "SUCCESS"):
            if not provider_trade_id and not provider_refund_id:
                raise StatementImportError(f"第 {line_number} 行退款记录缺少渠道交易或退款号")
            amount = refund_amount
            if amount is None or amount <= 0:
                raise StatementImportError(f"第 {line_number} 行退款金额无效")
            refund_fee = fee if not is_payment else None
            refund_status = _cell(source, indexes, "refund_status")
            if refund_aggregate:
                refund_status = "UNKNOWN"
            records.append(ParsedStatementRecord(
                source_line_number=line_number, event_index=event_index, record_type="refund", order_no=order_no,
                provider_trade_id=provider_trade_id, refund_no=refund_no, provider_refund_id=provider_refund_id,
                provider_status=(refund_status or normalized_status)[:32],
                currency=currency, amount_fen=amount,
                received_fen=None, fee_micros=refund_fee, fee_reported=fee is not None,
            ))
    if not records:
        raise StatementImportError("账单中没有可识别的支付或退款明细")
    if len(records) > MAX_STATEMENT_ROWS:
        raise StatementImportError("账单交易记录超过 100,000 条限制")
    payment_total = sum(row.amount_fen for row in records if row.record_type == "payment")
    refund_total = sum(row.amount_fen for row in records if row.record_type == "refund")
    received_total = sum(row.received_fen or 0 for row in records if row.record_type == "payment")
    received_missing_count = sum(
        row.record_type == "payment" and row.received_fen is None for row in records
    )
    fee_total = sum(row.fee_micros or 0 for row in records)
    fee_missing_count = len({row.source_line_number for row in records if not row.fee_reported})
    return ParsedStatement(
        tuple(records), payment_total, refund_total, received_total,
        received_missing_count, fee_total, fee_missing_count,
    )


def _safe_file_name(filename: str | None) -> str:
    name = PurePath((filename or "statement.csv").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().replace("/", "_")
    return (name or "statement.csv")[:200]


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _statement_window_utc(bill_date: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(bill_date, time.min, tzinfo=_CHINA)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _match_payment(db: Session, record: ParsedStatementRecord, *, channel: str) -> dict:
    order = db.query(BillingOrder).filter_by(order_no=record.order_no).first()
    result = {
        "local_order_status": order.status if order else None,
        "local_amount_fen": order.amount_fen if order else None,
        "local_trade_id": None,
        "local_refund_no": None,
        "local_provider_refund_id": None,
        "local_refund_status": None,
        "local_refund_amount_fen": None,
    }
    if record.currency != "CNY":
        return {**result, "discrepancy_code": "unsupported_currency"}
    if order is None:
        return {**result, "discrepancy_code": "provider_payment_without_local_order"}
    if order.channel != channel:
        return {**result, "discrepancy_code": "payment_channel_mismatch"}
    event = db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).first()
    if event:
        result["local_trade_id"] = event.trade_id
    if order.status not in {"paid", "refunded"} or event is None:
        return {**result, "discrepancy_code": "provider_payment_not_applied"}
    if order.amount_fen != record.amount_fen:
        return {**result, "discrepancy_code": "payment_amount_mismatch"}
    if event.trade_id != record.provider_trade_id:
        return {**result, "discrepancy_code": "payment_trade_id_mismatch"}
    return {**result, "discrepancy_code": "matched"}


def _match_refund(db: Session, record: ParsedStatementRecord, *, channel: str) -> dict:
    order = db.query(BillingOrder).filter_by(order_no=record.order_no).first()
    result = {
        "local_order_status": order.status if order else None,
        "local_amount_fen": order.amount_fen if order else None,
        "local_trade_id": None,
        "local_refund_no": None,
        "local_refund_status": None,
        "local_refund_amount_fen": None,
    }
    if record.currency != "CNY":
        return {**result, "discrepancy_code": "unsupported_currency"}
    if order is None:
        return {**result, "discrepancy_code": "provider_refund_without_local_order"}
    if order.channel != channel:
        return {**result, "discrepancy_code": "refund_channel_mismatch"}
    event = db.query(BillingPaymentEvent).filter_by(order_no=order.order_no).first()
    if event:
        result["local_trade_id"] = event.trade_id
    refund = db.query(BillingRefund).filter_by(order_no=order.order_no).first()
    if refund:
        result["local_refund_no"] = refund.refund_no
        result["local_provider_refund_id"] = refund.provider_refund_id
        result["local_refund_status"] = refund.status
        result["local_refund_amount_fen"] = refund.amount_fen
    if refund is None:
        return {**result, "discrepancy_code": "provider_refund_without_local_refund"}
    if record.refund_no and record.refund_no != refund.refund_no:
        return {**result, "discrepancy_code": "refund_no_mismatch"}
    if record.provider_refund_id and record.provider_refund_id != refund.provider_refund_id:
        code = "refund_provider_id_mismatch" if refund.provider_refund_id else "local_refund_provider_id_missing"
        return {**result, "discrepancy_code": code}
    if record.amount_fen != refund.amount_fen:
        return {**result, "discrepancy_code": "refund_amount_mismatch"}
    if event is None:
        return {**result, "discrepancy_code": "local_payment_event_missing"}
    if record.provider_trade_id and record.provider_trade_id != event.trade_id:
        return {**result, "discrepancy_code": "refund_trade_id_mismatch"}
    provider_success = record.provider_status in {"SUCCESS", "退款成功"}
    provider_failed = record.provider_status in {"FAIL", "CHANGE", "退款失败"}
    if record.provider_status in {"PROCESSING", "退款中"}:
        if refund.status in {"processing", "unknown", "approved", "manual_review"}:
            return {**result, "discrepancy_code": "refund_processing"}
        return {**result, "discrepancy_code": "refund_status_mismatch"}
    if record.provider_status in {"UNKNOWN", "REFUND"}:
        return {**result, "discrepancy_code": "provider_refund_status_unknown"}
    if provider_success and refund.status != "completed":
        return {**result, "discrepancy_code": "provider_refund_succeeded_local_pending"}
    if provider_success and refund.status == "completed":
        return {**result, "discrepancy_code": "matched"}
    if provider_failed:
        if refund.status == "completed":
            return {**result, "discrepancy_code": "local_refund_completed_provider_not_success"}
        if refund.status in {"failed", "rejected"}:
            return {**result, "discrepancy_code": "matched"}
        return {**result, "discrepancy_code": "refund_status_mismatch"}
    return {**result, "discrepancy_code": "provider_refund_status_unknown"}


def _persist_record(db: Session, *, batch_id: int, record: ParsedStatementRecord, match: dict) -> BillingStatementRow:
    row = BillingStatementRow(
        batch_id=batch_id,
        source_line_number=record.source_line_number,
        event_index=record.event_index,
        record_type=record.record_type,
        order_no=record.order_no,
        provider_trade_id=record.provider_trade_id,
        refund_no=record.refund_no,
        provider_refund_id=record.provider_refund_id,
        provider_status=record.provider_status,
        currency=record.currency,
        amount_fen=record.amount_fen,
        received_fen=record.received_fen,
        fee_micros=record.fee_micros,
        discrepancy_code=match["discrepancy_code"],
        **{key: value for key, value in match.items() if key != "discrepancy_code"},
    )
    db.add(row)
    return row


def _local_missing_candidates(db: Session, *, batch: BillingStatementBatch, parsed: ParsedStatement) -> None:
    if batch.bill_type == "REFUND":
        include_payments = False
    else:
        include_payments = True
    if batch.bill_type == "SUCCESS":
        include_refunds = False
    else:
        include_refunds = True
    start, end = _statement_window_utc(batch.bill_date)
    seen_payment_orders = {record.order_no for record in parsed.records if record.record_type == "payment"}
    seen_refunds = {(record.order_no, record.refund_no) for record in parsed.records if record.record_type == "refund"}
    event_index = 0
    if include_payments:
        events = (db.query(BillingPaymentEvent).filter(
            BillingPaymentEvent.channel == batch.channel,
            BillingPaymentEvent.verified_at >= start,
            BillingPaymentEvent.verified_at < end,
        ).order_by(BillingPaymentEvent.id).all())
        for event in events:
            if event.order_no in seen_payment_orders:
                continue
            order = db.query(BillingOrder).filter_by(order_no=event.order_no).first()
            if order is None:
                continue
            _persist_record(db, batch_id=batch.id,
                            record=ParsedStatementRecord(
                                source_line_number=0, event_index=event_index, record_type="payment",
                                order_no=order.order_no, provider_trade_id=None, refund_no=None,
                                provider_refund_id=None, provider_status="local_verified", currency="CNY",
                                amount_fen=order.amount_fen, received_fen=None, fee_micros=None,
                            ),
                            match={"local_order_status": order.status, "local_amount_fen": order.amount_fen,
                                   "local_trade_id": event.trade_id, "local_refund_no": None,
                                   "local_provider_refund_id": None,
                                   "local_refund_status": None, "local_refund_amount_fen": None,
                                   "discrepancy_code": "local_payment_missing_from_statement_candidate"})
            event_index += 1
    if include_refunds:
        refunds = (db.query(BillingRefund).join(BillingOrder, BillingOrder.order_no == BillingRefund.order_no)
                   .filter(BillingOrder.channel == batch.channel,
                           BillingRefund.created_at >= start, BillingRefund.created_at < end,
                           BillingRefund.status.in_({"processing", "unknown", "completed", "manual_review"}))
                   .order_by(BillingRefund.id).all())
        for refund in refunds:
            key = (refund.order_no, refund.refund_no)
            if key in seen_refunds or (refund.order_no, None) in seen_refunds:
                continue
            order = db.query(BillingOrder).filter_by(order_no=refund.order_no).first()
            if order is None:
                continue
            _persist_record(db, batch_id=batch.id,
                            record=ParsedStatementRecord(
                                source_line_number=0, event_index=event_index, record_type="refund",
                                order_no=refund.order_no, provider_trade_id=None, refund_no=refund.refund_no,
                                provider_refund_id=None, provider_status="local_pending", currency="CNY",
                                amount_fen=refund.amount_fen, received_fen=None, fee_micros=None,
                            ),
                            match={"local_order_status": order.status, "local_amount_fen": order.amount_fen,
                                   "local_trade_id": None, "local_refund_no": refund.refund_no,
                                   "local_provider_refund_id": refund.provider_refund_id,
                                   "local_refund_status": refund.status,
                                   "local_refund_amount_fen": refund.amount_fen,
                                   "discrepancy_code": "local_refund_missing_from_statement_candidate"})
            event_index += 1


def import_statement(db: Session, *, actor_id: int, channel: str, bill_date: str | date, bill_type: str,
                     filename: str | None, raw: bytes, source_origin: str = "manual_upload",
                     provider_hash_type: str | None = None,
                     provider_hash_value: str | None = None) -> BillingStatementBatch:
    """Parse and snapshot one statement; failures leave no partial batch."""
    try:
        normalized_date = date.fromisoformat(bill_date) if isinstance(bill_date, str) else bill_date
    except (TypeError, ValueError) as exc:
        raise StatementImportError("账单日期必须为 YYYY-MM-DD") from exc
    if not isinstance(normalized_date, date) or isinstance(normalized_date, datetime):
        raise StatementImportError("账单日期必须为 YYYY-MM-DD")
    if source_origin not in {"manual_upload", "alipay_api", "wechat_api"}:
        raise StatementImportError("账单来源类型无效")
    if source_origin == "wechat_api":
        if (provider_hash_type != "SHA1" or not isinstance(provider_hash_value, str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", provider_hash_value) is None):
            raise StatementImportError("微信 API 来源必须提供已验证的 SHA1 摘要")
    elif provider_hash_type is not None or provider_hash_value is not None:
        raise StatementImportError("该账单来源不接受渠道摘要字段")
    if (source_origin == "alipay_api" and channel != "alipay") or (
        source_origin == "wechat_api" and channel != "wechat"
    ):
        raise StatementImportError("账单来源与支付渠道不一致")
    decoded_payload = _decode_payload(raw)
    if source_origin == "wechat_api":
        actual_provider_hash = hashlib.sha1(decoded_payload, usedforsecurity=False).hexdigest()
        if not hmac.compare_digest(actual_provider_hash, provider_hash_value.lower()):
            raise StatementImportError("微信 API 账单 SHA1 校验失败，未创建批次")
    parsed = parse_statement(
        channel=channel, bill_type=bill_type, raw=raw, _decoded_payload=decoded_payload,
    )
    source_hash = hashlib.sha256(raw).hexdigest()
    existing = (db.query(BillingStatementBatch).filter_by(
        channel=channel, bill_date=normalized_date, bill_type=bill_type,
        source_origin=source_origin, file_sha256=source_hash,
    ).first())
    if existing is not None:
        return existing
    batch = BillingStatementBatch(
        channel=channel,
        source_origin=source_origin,
        bill_date=normalized_date,
        bill_type=bill_type,
        file_sha256=source_hash,
        provider_hash_type=provider_hash_type,
        provider_hash_value=provider_hash_value.lower() if provider_hash_value else None,
        file_name=_safe_file_name(filename),
        file_size=len(raw),
        parser_version=STATEMENT_PARSER_VERSION,
        row_count=len(parsed.records),
        payment_total_fen=parsed.payment_total_fen,
        refund_total_fen=parsed.refund_total_fen,
        received_total_fen=parsed.received_total_fen,
        received_missing_count=parsed.received_missing_count,
        fee_total_micros=parsed.fee_total_micros,
        fee_missing_count=parsed.fee_missing_count,
        imported_by=actor_id,
    )
    try:
        with db.begin_nested():
            db.add(batch)
            db.flush()
    except IntegrityError:
        existing = (db.query(BillingStatementBatch).filter_by(
            channel=channel, bill_date=normalized_date, bill_type=bill_type,
            source_origin=source_origin, file_sha256=source_hash,
        ).first())
        if existing is not None:
            return existing
        raise
    for record in parsed.records:
        if record.record_type == "payment":
            match = _match_payment(db, record, channel=channel)
            order = db.query(BillingOrder).filter_by(order_no=record.order_no).first()
            if (order is not None and order.channel == channel and record.currency == "CNY"
                    and order.amount_fen != record.amount_fen):
                match["discrepancy_code"] = "payment_amount_mismatch"
        else:
            match = _match_refund(db, record, channel=channel)
        _persist_record(db, batch_id=batch.id, record=record, match=match)
    _local_missing_candidates(db, batch=batch, parsed=parsed)
    db.flush()
    return batch


_REVIEW_DECISIONS = {"acknowledge", "investigating", "resolved_external"}


def review_statement_row(db: Session, *, actor_id: int, row_id: int, decision: str, note: str) -> BillingStatementRow:
    if decision not in _REVIEW_DECISIONS:
        raise StatementImportError("审阅决定无效")
    if not isinstance(note, str) or not note.strip() or len(note.strip()) > 500:
        raise StatementImportError("审阅说明必须为 1 至 500 个字符")
    row = db.query(BillingStatementRow).filter_by(id=row_id).with_for_update().first()
    if row is None:
        raise StatementImportError("账单明细不存在")
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.review_status = decision
    row.last_reviewed_by = actor_id
    row.last_reviewed_at = observed_at
    row.last_review_note = note.strip()
    db.add(BillingStatementReviewEvent(
        statement_row_id=row.id,
        actor_id=actor_id,
        decision=decision,
        note=note.strip(),
        created_at=observed_at,
    ))
    db.flush()
    return row
