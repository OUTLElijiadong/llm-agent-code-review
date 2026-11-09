import gzip
import hashlib
from datetime import date

import pytest

from app.services import wechat_fund_statement
from app.services.billing_statement_service import StatementImportError
from app.services.wechat_fund_statement import parse_wechat_fund_statement

HEADER = (
    '记账时间,微信支付业务单号,资金流水单号,业务名称,业务类型,收支类型,'
    '收支金额(元),账户结余(元),资金变更提交申请人,备注,业务凭证号\n'
)
SUMMARY = '资金流水总笔数,收入笔数,收入金额,支出笔数,支出金额\n'
LINES = [
    '`2026-11-05 08:00:00,`wx01,`flow01,`交易,`交易,`收入,`100.00,`100.00,`system,`private,`order01',
    '`2026-11-05 08:00:01,`wx01,`flow02,`交易,`扣除交易手续费,`支出,`0.60,`99.40,`system,`private,`order01',
    '`2026-11-05 09:00:00,`wx02,`flow03,`退款,`退款,`支出,`20.00,`79.40,`system,`private,`refund01',
    '`2026-11-05 09:00:01,`wx02,`flow04,`退款,`退还交易手续费,`收入,`0.12,`79.52,`system,`private,`refund01',
]


def sample(lines=LINES, summary='`4,`2,`100.12,`2,`20.60'):
    return (HEADER + '\n'.join(lines) + '\n' + SUMMARY + summary + '\n').encode()


def test_parses_official_fund_flow_and_verifies_summary_without_personal_fields():
    parsed = parse_wechat_fund_statement(raw=sample(), bill_date=date(2026, 11, 5))
    assert len(parsed.records) == 4
    assert (parsed.income_count, parsed.income_fen, parsed.expense_count, parsed.expense_fen) == (2, 10012, 2, 2060)
    assert (parsed.fee_debit_fen, parsed.fee_refund_fen) == (60, 12)
    assert parsed.records[2].business_voucher_no == 'refund01'
    assert parsed.repeated_flow_numbers == ()
    assert 'private' not in repr(parsed)
    assert parsed.file_sha256 == hashlib.sha256(sample()).hexdigest()


@pytest.mark.parametrize('raw', [sample(), gzip.compress(sample())])
def test_plain_and_gzip_manual_file_formats_are_supported(raw):
    assert len(parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05').records) == 4


def test_repeated_flow_numbers_are_preserved_without_assuming_they_are_duplicates():
    lines = [LINES[0], LINES[0]]
    parsed = parse_wechat_fund_statement(raw=sample(lines, '`2,`2,`200.00,`0,`0.00'), bill_date='2026-11-05')
    assert len(parsed.records) == 2
    assert parsed.repeated_flow_numbers == ('flow01',)


def test_one_flow_number_can_cover_income_and_fee_without_false_discrepancy():
    fee = LINES[1].replace('`flow02', '`flow01')
    parsed = parse_wechat_fund_statement(
        raw=sample([LINES[0], fee], '`2.0,`1.0,`100.00,`1.0,`0.60'), bill_date='2026-11-05',
    )
    assert [record.direction for record in parsed.records] == ['收入', '支出']
    assert parsed.income_fen == 10000
    assert parsed.expense_fen == parsed.fee_debit_fen == 60
    assert parsed.repeated_flow_numbers == ('flow01',)


def test_blank_lines_preserve_source_line_numbers():
    raw = (HEADER + '\n' + LINES[0] + '\n\n' + SUMMARY + '`1,`1,`100.00,`0,`0.00\n').encode()
    parsed = parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')
    assert parsed.records[0].source_line_number == 3


def test_row_limit_stops_reading_before_materializing_rest(monkeypatch):
    consumed = 0

    def rows(*_args, **_kwargs):
        nonlocal consumed
        for _ in range(200_000):
            consumed += 1
            yield []

    monkeypatch.setattr(wechat_fund_statement.csv, 'reader', rows)
    with pytest.raises(StatementImportError, match='行数限制'):
        list(wechat_fund_statement._fund_rows(b'placeholder'))
    assert consumed <= wechat_fund_statement.MAX_STATEMENT_ROWS + 41


def test_compact_gzip_with_too_many_blank_rows_is_rejected():
    raw = gzip.compress((HEADER + '\n' * (wechat_fund_statement.MAX_STATEMENT_ROWS + 41)).encode())
    with pytest.raises(StatementImportError, match='行数限制'):
        parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')


def test_row_with_excess_columns_is_rejected():
    raw = (HEADER.rstrip('\n') + ',' * 300 + '\n').encode()
    with pytest.raises(StatementImportError, match='列数限制'):
        parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')


def test_many_empty_columns_are_rejected_before_csv_row_allocation(monkeypatch):
    csv_reader = wechat_fund_statement.csv.reader
    allocated_sizes = []

    def observed_reader(*args, **kwargs):
        for row in csv_reader(*args, **kwargs):
            allocated_sizes.append(len(row))
            yield row

    monkeypatch.setattr(wechat_fund_statement.csv, 'reader', observed_reader)
    with pytest.raises(StatementImportError, match='列数限制'):
        list(wechat_fund_statement._fund_rows(b',' * 100000 + b'\n'))
    assert allocated_sizes == []


@pytest.mark.parametrize('raw', [
    b'"a,\nb",c\n',
    b'"unfinished',
    b'"a"x,b\n',
])
def test_malformed_or_multiline_quoted_fields_fail_closed(raw):
    with pytest.raises(StatementImportError):
        list(wechat_fund_statement._fund_rows(raw))


def test_precheck_respects_escaped_quotes_commas_crlf_and_trailing_empty_fields():
    raw = sample().replace(b'`private,`order01', b'"`private,""quoted"",part",`order01,,', 1)
    parsed = parse_wechat_fund_statement(raw=raw.replace(b'\n', b'\r\n'), bill_date='2026-11-05')
    assert parsed.records[0].business_voucher_no == 'order01'


def test_precheck_does_not_count_commas_inside_a_quoted_field():
    row = b'"' + b',' * 300 + b'",last\n'
    assert list(wechat_fund_statement._fund_rows(row)) == [[',' * 300, 'last']]


@pytest.mark.parametrize('control', ['\x01', '\x09', '\x1b', '\x7f', '\x85'])
def test_fund_csv_rejects_control_characters_in_fields(control):
    raw = sample().replace(b'`order01', f'`order{control}01'.encode(), 1)
    with pytest.raises(StatementImportError, match='控制字符'):
        parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')


def test_oversized_physical_line_is_rejected_before_csv_cell_allocation():
    raw = gzip.compress(b',' * (wechat_fund_statement._MAX_PHYSICAL_LINE_CHARS + 1))
    with pytest.raises(StatementImportError, match='单行超过长度限制'):
        parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')


def test_unquoted_extra_field_cannot_silently_shift_business_voucher():
    raw = sample().replace(b'`private,`order01', b'`private,`SPLIT,`order01', 1)
    with pytest.raises(StatementImportError, match='明细.*列'):
        parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')


def test_quoted_comma_and_extra_empty_columns_preserve_voucher():
    raw = sample().replace(b'`private,`order01', b'"`private,part",`order01,,', 1)
    parsed = parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')
    assert parsed.records[0].business_voucher_no == 'order01'


def test_unknown_business_types_are_preserved_not_guessed_as_fees():
    line = LINES[0].replace('`交易,`交易,', '`未来业务,`新费用,')
    parsed = parse_wechat_fund_statement(raw=sample([line], '`1,`1,`100.00,`0,`0.00'), bill_date='2026-11-05')
    assert parsed.records[0].business_type == '新费用'
    assert parsed.fee_debit_fen == parsed.fee_refund_fen == 0


@pytest.mark.parametrize('raw', [
    sample(summary='`4,`2,`100.11,`2,`20.60'),
    sample(summary='`4,`1,`100.12,`3,`20.60'),
    sample().replace(b'`2026-11-05', b'`2026-11-06', 1),
    sample().replace(b'`0.60', b'`-0.60', 1),
    sample().replace(b'`0.60', b'`0.601', 1),
    sample().replace('`支出'.encode(), '`未知'.encode(), 1),
    sample().replace('`扣除交易手续费,`支出'.encode(), '`扣除交易手续费,`收入'.encode()),
    sample().replace(b'`private', b'"private\nline"', 1),
    sample().replace(b'`20.60\n', b'"`20.60'),
    sample() + b'extra,row\n',
    sample().replace(SUMMARY.encode(), b''),
])
def test_rejects_inconsistent_or_unrecognised_statement(raw):
    with pytest.raises(StatementImportError):
        parse_wechat_fund_statement(raw=raw, bill_date='2026-11-05')


def test_empty_fund_statement_has_explicit_matching_zero_summary():
    parsed = parse_wechat_fund_statement(raw=sample([], '`0,`0,`0.00,`0,`0.00'), bill_date='2026-11-05')
    assert parsed.records == ()
    assert parsed.income_fen == parsed.expense_fen == 0


def test_integral_decimal_summary_counts_from_official_example():
    parsed = parse_wechat_fund_statement(
        raw=sample(summary='`4.0,`2.0,`100.12,`2.0,`20.60'), bill_date='2026-11-05',
    )
    assert len(parsed.records) == 4
