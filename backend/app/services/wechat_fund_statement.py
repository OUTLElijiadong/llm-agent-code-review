"""Parse one WeChat account fund-flow bill without altering any business state.

The totals here verify only internal file consistency. A cash-flow entry is not
necessarily a payment or a refund and must not automatically settle either.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime

from app.services.billing_statement_service import (
    MAX_FIELD_CHARS,
    MAX_STATEMENT_ROWS,
    StatementImportError,
    _decode_payload,
    _decode_text,
    _money_fen,
    _normalized_cell,
)

_DETAIL_HEADERS = (
    "记账时间", "微信支付业务单号", "资金流水单号", "业务名称", "业务类型", "收支类型",
    "收支金额(元)", "账户结余(元)", "资金变更提交申请人", "备注", "业务凭证号",
)
_SUMMARY_HEADERS = ("资金流水总笔数", "收入笔数", "收入金额", "支出笔数", "支出金额")
_MAX_I64 = 2**63 - 1
_MAX_COLUMNS = 256
# Bound a physical line before csv.reader can allocate its cells. Official
# bills have 11 detail fields; allow additional fields without an unbounded row.
_MAX_PHYSICAL_LINE_CHARS = _MAX_COLUMNS * (MAX_FIELD_CHARS + 1)


@dataclass(frozen=True)
class FundFlowRecord:
    source_line_number: int
    booked_at: datetime
    provider_business_no: str
    funding_flow_no: str
    business_name: str
    business_type: str
    direction: str
    amount_fen: int
    balance_fen: int
    business_voucher_no: str


@dataclass(frozen=True)
class ParsedWechatFundStatement:
    bill_date: date
    file_sha256: str
    records: tuple[FundFlowRecord, ...]
    income_count: int
    income_fen: int
    expense_count: int
    expense_fen: int
    fee_debit_fen: int
    fee_refund_fen: int
    repeated_flow_numbers: tuple[str, ...]


def _header(value: str) -> str:
    return _normalized_cell(value).replace("（", "(").replace("）", ")")


def _columns(row: list[str], required: tuple[str, ...]) -> dict[str, int] | None:
    names = [_header(cell) for cell in row]
    if not set(required).issubset(names):
        return None
    if any(names.count(name) != 1 for name in required):
        raise StatementImportError("资金账单表头存在重复字段")
    return {name: names.index(name) for name in required}


def _cell(row: list[str], columns: dict[str, int], name: str) -> str:
    index = columns[name]
    if index >= len(row):
        raise StatementImportError("资金账单记录缺少字段")
    value = _normalized_cell(row[index])
    if "\n" in value or "\r" in value:
        raise StatementImportError("资金账单字段包含未转义换行")
    return value


def _positive_money(value: str) -> int:
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,16})(?:\.[0-9]{1,2})?", value) is None:
        raise StatementImportError("资金账单金额格式无效")
    amount = _money_fen(value, required=True)
    if amount is None or amount < 0 or amount > _MAX_I64:
        raise StatementImportError("资金账单金额超出范围")
    return amount


def _count(value: str) -> int:
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.0{1,6})?", value) is None:
        raise StatementImportError("资金账单汇总笔数无效")
    return int(value.split(".", 1)[0])


def _check_csv_line(line: str) -> None:
    """Bound fields before csv.reader allocates a row; official fields do not span lines."""
    content = line.removesuffix("\n").removesuffix("\r")
    columns, chars, index = 1, 0, 0
    at_start, quoted = True, False
    while index < len(content):
        char = content[index]
        codepoint = ord(char)
        # WeChat escapes embedded CR/LF and replaces SOH in downloaded fields.
        # Other control characters are not meaningful CSV data here either.
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            raise StatementImportError("资金账单字段包含控制字符")
        if quoted:
            if char == '"':
                if index + 1 < len(content) and content[index + 1] == '"':
                    chars += 1
                    index += 1
                else:
                    quoted = False
            else:
                chars += 1
        elif char == ",":
            columns += 1
            if columns > _MAX_COLUMNS:
                raise StatementImportError("资金账单超过列数限制")
            chars, at_start = 0, True
        elif char == '"' and at_start:
            quoted, at_start = True, False
        else:
            chars += 1
            at_start = False
        if chars > MAX_FIELD_CHARS:
            raise StatementImportError("资金账单字段长度超过限制")
        index += 1
    if quoted:
        raise StatementImportError("资金账单字段包含未转义换行或未闭合引号")


def _fund_rows(raw: bytes) -> Iterator[list[str]]:
    text = _decode_text(_decode_payload(raw))

    def bounded_lines() -> Iterator[str]:
        stream = io.StringIO(text, newline="")
        while line := stream.readline(_MAX_PHYSICAL_LINE_CHARS + 1):
            if len(line) > _MAX_PHYSICAL_LINE_CHARS:
                raise StatementImportError("资金账单单行超过长度限制")
            _check_csv_line(line)
            yield line

    try:
        for count, row in enumerate(csv.reader(bounded_lines(), strict=True), start=1):
            if count > MAX_STATEMENT_ROWS + 40:
                raise StatementImportError("资金账单超过行数限制")
            if len(row) > _MAX_COLUMNS:
                raise StatementImportError("资金账单超过列数限制")
            if any(len(cell) > MAX_FIELD_CHARS for cell in row):
                raise StatementImportError("资金账单字段长度超过限制")
            if any("\n" in cell or "\r" in cell for cell in row):
                raise StatementImportError("资金账单字段包含未转义换行")
            yield row
    except (csv.Error, UnicodeError) as exc:
        raise StatementImportError("资金账单 CSV 格式无效") from exc


def parse_wechat_fund_statement(*, raw: bytes, bill_date: str | date) -> ParsedWechatFundStatement:
    """Parse an official fund CSV and check its detail and summary agreement."""
    try:
        expected_date = date.fromisoformat(bill_date) if isinstance(bill_date, str) else bill_date
    except ValueError as exc:
        raise StatementImportError("账单日期必须为 YYYY-MM-DD") from exc
    if (not isinstance(expected_date, date) or isinstance(expected_date, datetime)
            or isinstance(bill_date, str) and expected_date.isoformat() != bill_date):
        raise StatementImportError("账单日期必须为 YYYY-MM-DD")
    rows = ((line, row) for line, row in enumerate(_fund_rows(raw), start=1)
            if row and any(_normalized_cell(cell) for cell in row))
    first_row = next(rows, None)
    if first_row is None:
        raise StatementImportError("资金账单为空")
    detail_columns = _columns(first_row[1], _DETAIL_HEADERS)
    if detail_columns is None:
        raise StatementImportError("资金账单明细表头不受支持")
    records: list[FundFlowRecord] = []
    summary_columns = None
    summary_row = None
    for index, row in rows:
        if summary_columns is not None:
            if summary_row is not None:
                raise StatementImportError("资金账单汇总后存在额外内容")
            summary_row = row
            continue
        candidate = _columns(row, _SUMMARY_HEADERS)
        if candidate is not None:
            summary_columns = candidate
            continue
        if any(_normalized_cell(cell) for cell in row[len(first_row[1]):]):
            raise StatementImportError("资金账单明细存在超出表头的非空列")
        if len(records) >= MAX_STATEMENT_ROWS:
            raise StatementImportError("资金账单明细超过行数限制")
        def value(name: str) -> str:
            return _cell(row, detail_columns, name)
        timestamp = value("记账时间")
        try:
            booked_at = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise StatementImportError("资金账单记账时间无效") from exc
        if booked_at.strftime("%Y-%m-%d %H:%M:%S") != timestamp or booked_at.date() != expected_date:
            raise StatementImportError("资金账单记账时间与所选日期不一致")
        flow_no = value("资金流水单号")
        name, kind, direction = value("业务名称"), value("业务类型"), value("收支类型")
        if not flow_no or not name or not kind or direction not in {"收入", "支出"}:
            raise StatementImportError("资金账单业务、流水号或收支类型无效")
        if (kind == "扣除交易手续费" and direction != "支出") or (
            kind == "退还交易手续费" and direction != "收入"
        ):
            raise StatementImportError("资金账单手续费收支方向无效")
        records.append(FundFlowRecord(
            source_line_number=index, booked_at=booked_at, provider_business_no=value("微信支付业务单号"),
            funding_flow_no=flow_no, business_name=name, business_type=kind, direction=direction,
            amount_fen=_positive_money(value("收支金额(元)")), balance_fen=_positive_money(value("账户结余(元)")),
            business_voucher_no=value("业务凭证号"),
        ))
    if summary_columns is None or summary_row is None:
        raise StatementImportError("资金账单缺少汇总表头或汇总记录")
    def summary(name: str) -> str:
        return _cell(summary_row, summary_columns, name)
    income = [record for record in records if record.direction == "收入"]
    expense = [record for record in records if record.direction == "支出"]
    income_fen, expense_fen = sum(row.amount_fen for row in income), sum(row.amount_fen for row in expense)
    if (income_fen > _MAX_I64 or expense_fen > _MAX_I64
            or _count(summary("资金流水总笔数")) != len(records)
            or _count(summary("收入笔数")) != len(income)
            or _count(summary("支出笔数")) != len(expense)
            or _positive_money(summary("收入金额")) != income_fen
            or _positive_money(summary("支出金额")) != expense_fen):
        raise StatementImportError("资金账单汇总与明细不一致")
    counts = Counter(row.funding_flow_no for row in records)
    return ParsedWechatFundStatement(
        bill_date=expected_date, file_sha256=hashlib.sha256(raw).hexdigest(), records=tuple(records),
        income_count=len(income), income_fen=income_fen, expense_count=len(expense), expense_fen=expense_fen,
        fee_debit_fen=sum(row.amount_fen for row in expense if row.business_type == "扣除交易手续费"),
        fee_refund_fen=sum(row.amount_fen for row in income if row.business_type == "退还交易手续费"),
        repeated_flow_numbers=tuple(sorted(key for key, count in counts.items() if count > 1)),
    )
