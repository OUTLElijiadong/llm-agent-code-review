import csv
import gzip
import hashlib
import io
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.billing_ledger import (
    BillingOrder,
    BillingPaymentEvent,
    BillingRefund,
    BillingStatementBatch,
    BillingStatementReviewEvent,
    BillingStatementRow,
)
from app.models.user import User
from app.services.billing_statement_service import (
    StatementImportError,
    import_statement,
    parse_statement,
    review_statement_row,
)


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _wechat_csv(rows: list[list[str]], *, header: list[str] | None = None) -> bytes:
    header = header or [
        "交易时间", "公众账号ID", "商户号", "特约商户号", "设备号", "微信订单号", "商户订单号", "用户标识",
        "交易类型", "交易状态", "付款银行", "货币种类", "应结订单金额", "代金券金额", "微信退款单号",
        "商户退款单号", "退款金额", "充值券退款金额", "退款类型", "退款状态", "商品名称", "商户数据包",
        "手续费", "费率", "订单金额", "申请退款金额", "费率备注",
    ]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([f"`{item}" for item in header])
    for row in rows:
        writer.writerow([f"`{item}" if item else "" for item in row])
    writer.writerow(["总交易单数", "应结订单总金额", "退款总金额", "手续费总金额"])
    writer.writerow([str(len(rows)), "4.00", "0.00", "0.01"])
    return output.getvalue().encode("utf-8-sig")


def _wechat_payment_row(order_no="pr-order-1", *, fee="0.01", amount="4.00"):
    values = [
        "2026-11-04 12:00:00", "wx-app", "1234567890", "0", "", "wx-trade-1", order_no, "must-not-be-stored",
        "NATIVE", "SUCCESS", "OTHERS", "CNY", amount, "0.00", "0", "0", "0.00", "0.00", "", "",
        "积分", "", fee, "0.60%", amount, "0.00", "",
    ]
    return values


def _wechat_refund_row(order_no="pr-order-1", *, refund_no="pr-refund-1", refund_amount="4.00", fee="-0.01"):
    values = [
        "2026-11-04 13:00:00", "wx-app", "1234567890", "0", "", "wx-trade-1", order_no, "private-openid",
        "NATIVE", "REFUND", "OTHERS", "CNY", "0.00", "0.00", "wx-refund-1", refund_no,
        refund_amount, "0.00", "ORIGINAL", "SUCCESS", "积分", "", fee, "0.60%", "0.00", refund_amount, "",
    ]
    return values


def test_parse_wechat_v3_all_bill_skips_summary_and_keeps_fee_precision_without_pii():
    raw = _wechat_csv([_wechat_payment_row(fee="0.00001"), _wechat_refund_row(fee="-0.00001")])

    parsed = parse_statement(channel="wechat", bill_type="ALL", raw=raw)

    assert [row.record_type for row in parsed.records] == ["payment", "refund"]
    assert parsed.records[0].order_no == "pr-order-1"
    assert parsed.records[0].amount_fen == 400
    assert parsed.records[0].received_fen == 400
    assert parsed.records[0].fee_micros == 10
    assert parsed.records[1].refund_no == "pr-refund-1"
    assert parsed.records[1].amount_fen == 400
    assert parsed.records[1].fee_micros == -10
    assert parsed.payment_total_fen == 400
    assert parsed.refund_total_fen == 400
    assert parsed.received_missing_count == 0
    assert parsed.fee_total_micros == 0
    assert parsed.fee_missing_count == 0
    assert "must-not-be-stored" not in repr(parsed)
    assert "private-openid" not in repr(parsed)


def test_parse_wechat_accepts_gzip_and_legacy_amount_headers():
    header = [
        "交易时间", "公众账号ID", "商户号", "设备号", "微信订单号", "商户订单号", "用户标识", "交易类型", "交易状态",
        "付款银行", "货币种类", "总金额", "企业红包金额", "手续费",
    ]
    row = [
        "2026-11-04 12:00:00", "wx-app", "1234567890", "", "wx-trade-1", "pr-order-1", "private",
        "NATIVE", "SUCCESS", "OTHERS", "CNY", "4.00", "0.00", "0.01",
    ]
    raw = gzip.compress(_wechat_csv([row], header=header))

    parsed = parse_statement(channel="wechat", bill_type="SUCCESS", raw=raw)

    assert parsed.records[0].amount_fen == 400
    assert parsed.records[0].received_fen is None
    assert parsed.received_missing_count == 1
    assert parsed.fee_total_micros == 10_000
    assert parsed.fee_missing_count == 0


def test_parse_rejects_missing_payment_or_refund_amount_instead_of_substituting_other_amounts():
    payment = _wechat_payment_row()
    payment[24] = ""
    with pytest.raises(StatementImportError, match="支付金额无效"):
        parse_statement(channel="wechat", bill_type="SUCCESS", raw=_wechat_csv([payment]))

    refund = _wechat_refund_row()
    refund[16] = ""
    refund[24] = "4.00"
    refund[25] = ""
    with pytest.raises(StatementImportError, match="退款金额无效"):
        parse_statement(channel="wechat", bill_type="REFUND", raw=_wechat_csv([refund]))


def test_parse_reports_missing_fee_source_rows_without_fabricating_a_zero_fee():
    header = ["微信订单号", "商户订单号", "交易状态", "货币种类", "总金额"]
    row = ["wx-trade-1", "pr-order-1", "SUCCESS", "CNY", "4.00"]

    parsed = parse_statement(channel="wechat", bill_type="SUCCESS", raw=_wechat_csv([row], header=header))

    assert parsed.fee_total_micros == 0
    assert parsed.fee_missing_count == 1


def test_parse_alipay_trade_bill_recognizes_documented_transaction_identifiers():
    raw = (
        "\ufeff交易时间,商户订单号,支付宝交易号,交易状态,订单金额,商家实收,服务费,退款金额,商户退款单号\n"
        "2026-11-04 12:00:00,pr-order-1,ali-trade-1,交易成功,4.00,3.99,0.01,0.00,\n"
        "2026-11-04 13:00:00,pr-order-1,ali-trade-1,退款成功,0.00,0.00,-0.01,4.00,pr-refund-1\n"
    ).encode("utf-8-sig")

    parsed = parse_statement(channel="alipay", bill_type="trade", raw=raw)

    assert [(row.record_type, row.order_no, row.amount_fen) for row in parsed.records] == [
        ("payment", "pr-order-1", 400), ("refund", "pr-order-1", 400),
    ]
    assert parsed.records[0].provider_trade_id == "ali-trade-1"
    assert parsed.records[1].refund_no == "pr-refund-1"
    assert parsed.fee_total_micros == 0


@pytest.mark.parametrize(
    ("channel", "bill_type", "raw"),
    [
        ("wechat", "ALL", b"not a statement"),
        ("wechat", "ALL", b"\x00" * 40),
        ("alipay", "trade", "订单号,交易号,金额\n1,2,3.141\n".encode()),
    ],
)
def test_statement_parser_fails_closed_for_unknown_or_invalid_bill_layout(channel, bill_type, raw):
    with pytest.raises(StatementImportError):
        parse_statement(channel=channel, bill_type=bill_type, raw=raw)


def test_import_matches_order_and_refund_without_changing_payment_ledger_and_is_idempotent():
    engine, db = _database()
    try:
        user = User(username="statement-owner", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        order = BillingOrder(
            order_no="pr-order-1", user_id=user.id, enterprise_id=None, product="personal_credits", amount_fen=400,
            quantity=1, channel="wechat", status="paid", paid_at=None,
        )
        db.add(order)
        db.flush()
        db.add(BillingPaymentEvent(
            channel="wechat", trade_id="wx-trade-1", order_no=order.order_no, amount_fen=400,
            verified_at=datetime(2026, 11, 4, 4, 0, 0),
        ))
        db.add(BillingRefund(
            order_no=order.order_no, refund_no="pr-refund-1", amount_fen=400, reason="test",
            status="completed", requested_by=user.id, provider_refund_id="wx-refund-1",
        ))
        db.commit()

        raw = _wechat_csv([_wechat_payment_row(), _wechat_refund_row()])
        result = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="ALL",
            filename="../../wechat.csv", raw=raw,
        )
        db.commit()
        paid_before = (order.status, db.query(BillingPaymentEvent).count())
        duplicate = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="ALL",
            filename="second-upload.csv", raw=raw,
        )

        assert duplicate.id == result.id
        assert result.file_name == "wechat.csv"
        assert result.row_count == 2
        assert result.payment_total_fen == 400
        assert result.refund_total_fen == 400
        assert result.received_missing_count == 0
        assert result.fee_missing_count == 0
        rows = (db.query(BillingStatementRow).filter_by(batch_id=result.id)
                .order_by(BillingStatementRow.source_line_number).all())
        assert [row.discrepancy_code for row in rows] == ["matched", "matched"]
        assert {row.review_status for row in rows} == {"unreviewed"}
        assert (order.status, db.query(BillingPaymentEvent).count()) == paid_before == ("paid", 1)
        assert "private-openid" not in repr(rows)
        assert db.query(BillingStatementBatch).count() == 1
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("local_provider_id", "provider_status", "expected"),
    [
        ("wx-refund-other", "SUCCESS", "refund_provider_id_mismatch"),
        (None, "SUCCESS", "local_refund_provider_id_missing"),
        ("wx-refund-1", "CLOSED", "provider_refund_status_unknown"),
    ],
)
def test_refund_provider_id_and_unknown_status_fail_closed(local_provider_id, provider_status, expected):
    engine, db = _database()
    try:
        user = User(username=f"statement-refund-{expected}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        order = BillingOrder(
            order_no="pr-refund-check", user_id=user.id, enterprise_id=None, product="personal_credits",
            amount_fen=400, quantity=1, channel="wechat", status="refunded", paid_at=None,
        )
        db.add(order)
        db.flush()
        db.add(BillingPaymentEvent(
            channel="wechat", trade_id="wx-trade-1", order_no=order.order_no, amount_fen=400,
        ))
        db.add(BillingRefund(
            order_no=order.order_no, refund_no="pr-refund-1", amount_fen=400, reason="test",
            status="completed", requested_by=user.id, provider_refund_id=local_provider_id,
        ))
        db.commit()
        row = _wechat_refund_row(order_no=order.order_no)
        row[19] = provider_status
        batch = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="REFUND",
            filename="wechat.csv", raw=_wechat_csv([row]),
        )
        db.commit()
        statement_row = db.query(BillingStatementRow).filter_by(batch_id=batch.id).one()
        assert statement_row.discrepancy_code == expected
        assert statement_row.local_provider_refund_id == local_provider_id
    finally:
        db.close()
        engine.dispose()


def test_refund_does_not_match_when_local_payment_event_is_missing():
    engine, db = _database()
    try:
        user = User(username="statement-refund-missing-event", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        order = BillingOrder(
            order_no="pr-refund-missing-event", user_id=user.id, enterprise_id=None,
            product="personal_credits", amount_fen=400, quantity=1, channel="wechat",
            status="refunded", paid_at=None,
        )
        db.add(order)
        db.flush()
        db.add(BillingRefund(
            order_no=order.order_no, refund_no="pr-refund-missing-event-id", amount_fen=400,
            reason="test", status="completed", requested_by=user.id,
            provider_refund_id="wx-refund-missing-event",
        ))
        db.commit()

        refund_row = _wechat_refund_row(order_no=order.order_no)
        refund_row[15] = "pr-refund-missing-event-id"
        refund_row[14] = "wx-refund-missing-event"
        batch = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="REFUND",
            filename="wechat.csv", raw=_wechat_csv([refund_row]),
        )
        db.commit()

        statement_row = db.query(BillingStatementRow).filter_by(batch_id=batch.id).one()
        assert statement_row.discrepancy_code == "local_payment_event_missing"
        assert statement_row.local_trade_id is None
    finally:
        db.close()
        engine.dispose()


def test_import_reports_amount_and_orphan_anomalies_and_local_missing_candidate():
    engine, db = _database()
    try:
        user = User(username="statement-anomaly", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        order = BillingOrder(
            order_no="pr-order-1", user_id=user.id, enterprise_id=None, product="personal_credits", amount_fen=400,
            quantity=1, channel="wechat", status="paid", paid_at=None,
        )
        db.add(order)
        db.flush()
        db.add(BillingPaymentEvent(
            channel="wechat", trade_id="wx-trade-1", order_no=order.order_no, amount_fen=400,
            verified_at=datetime(2026, 11, 4, 4, 0, 0),
        ))
        unseen_order = BillingOrder(
            order_no="pr-order-unseen", user_id=user.id, enterprise_id=None, product="personal_credits",
            amount_fen=600, quantity=1, channel="wechat", status="paid", paid_at=None,
        )
        db.add(unseen_order)
        db.flush()
        db.add(BillingPaymentEvent(
            channel="wechat", trade_id="wx-trade-unseen", order_no=unseen_order.order_no, amount_fen=600,
            verified_at=datetime(2026, 11, 4, 5, 0, 0),
        ))
        db.commit()

        raw = _wechat_csv([_wechat_payment_row(amount="5.00"), _wechat_payment_row("not-local")])
        batch = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="ALL",
            filename="wechat.csv", raw=raw,
        )
        db.commit()

        rows = db.query(BillingStatementRow).filter_by(batch_id=batch.id).order_by(
            BillingStatementRow.source_line_number, BillingStatementRow.event_index,
        ).all()
        codes = [row.discrepancy_code for row in rows]
        assert "payment_amount_mismatch" in codes
        assert "provider_payment_without_local_order" in codes
        assert "local_payment_missing_from_statement_candidate" in codes
    finally:
        db.close()
        engine.dispose()


def test_review_is_append_only_and_never_changes_the_payment_or_refund():
    engine, db = _database()
    try:
        user = User(username="statement-reviewer", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        order = BillingOrder(
            order_no="pr-order-1", user_id=user.id, enterprise_id=None, product="personal_credits", amount_fen=400,
            quantity=1, channel="wechat", status="created", paid_at=None,
        )
        db.add(order)
        db.commit()
        batch = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="SUCCESS",
            filename="wechat.csv", raw=_wechat_csv([_wechat_payment_row()]),
        )
        db.commit()
        row = db.query(BillingStatementRow).filter_by(batch_id=batch.id).one()

        review_statement_row(db, actor_id=user.id, row_id=row.id, decision="investigating", note="已确认需要查单")
        db.commit()
        review_statement_row(
            db, actor_id=user.id, row_id=row.id, decision="resolved_external",
            note="渠道后台已核实，等待系统内查单同步",
        )
        db.commit()

        assert row.review_status == "resolved_external"
        assert db.query(BillingStatementReviewEvent).filter_by(statement_row_id=row.id).count() == 2
        db.refresh(order)
        assert order.status == "created"
        assert db.query(BillingPaymentEvent).count() == 0
        assert db.query(BillingRefund).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_review_requires_a_note_and_valid_decision():
    engine, db = _database()
    try:
        with pytest.raises(StatementImportError):
            review_statement_row(db, actor_id=1, row_id=999, decision="resolved_external", note=" ")
    finally:
        db.close()
        engine.dispose()


def test_verified_provider_source_metadata_is_saved_and_manual_source_is_not_collapsed():
    engine, db = _database()
    try:
        user = User(username="statement-source", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        bill_bytes = _wechat_csv([_wechat_payment_row()])
        raw = gzip.compress(bill_bytes)
        provider_sha1 = hashlib.sha1(bill_bytes, usedforsecurity=False).hexdigest()
        with pytest.raises(StatementImportError, match="SHA1 摘要"):
            import_statement(
                db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="SUCCESS",
                filename="wechat.gz", raw=raw, source_origin="wechat_api",
            )
        with pytest.raises(StatementImportError, match="SHA1 校验失败"):
            import_statement(
                db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="SUCCESS",
                filename="wechat.gz", raw=raw, source_origin="wechat_api",
                provider_hash_type="SHA1", provider_hash_value=hashlib.sha1(
                    raw, usedforsecurity=False,
                ).hexdigest(),
            )
        verified = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="SUCCESS",
            filename="wechat.gz", raw=raw, source_origin="wechat_api",
            provider_hash_type="SHA1", provider_hash_value=provider_sha1.upper(),
        )
        manual = import_statement(
            db, actor_id=user.id, channel="wechat", bill_date="2026-11-04", bill_type="SUCCESS",
            filename="manual.csv", raw=raw,
        )
        assert verified.id != manual.id
        assert verified.source_origin == "wechat_api"
        assert verified.provider_hash_type == "SHA1"
        assert verified.provider_hash_value == provider_sha1
        assert manual.source_origin == "manual_upload"
        assert db.query(BillingStatementBatch).count() == 2
    finally:
        db.close()
        engine.dispose()
