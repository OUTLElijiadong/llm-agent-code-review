import base64
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services.billing_payment_gateway import (
    PaymentGatewayError,
    create_alipay_preorder,
    create_wechat_native_preorder,
)


def _key_pair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, private_pem, public_pem


def _signed(private, message: bytes) -> str:
    return base64.b64encode(private.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode("ascii")


def test_alipay_precreate_signs_server_priced_request_and_returns_only_provider_qr():
    private, private_pem, public_pem = _key_pair()

    def handle(request: httpx.Request):
        params = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        signed = "&".join(f"{key}={params[key]}" for key in sorted(params) if key != "sign")
        private.public_key().verify(
            base64.b64decode(params["sign"]), signed.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        assert request.url == "https://openapi.alipay.com/gateway.do"
        assert params["app_id"] == "app-1"
        assert params["notify_url"] == "https://prism.example/api/billing/payments/alipay/notify"
        content = json.loads(params["biz_content"])
        assert content == {
            "out_trade_no": "pr-order-1",
            "total_amount": "69.00",
            "subject": "企业席位",
            "timeout_express": "15m",
        }
        result = {"code": "10000", "qr_code": "https://qr.alipay.com/abc123"}
        signature = _signed(private, json.dumps(result, separators=(",", ":")).encode())
        return httpx.Response(200, json={"alipay_trade_precreate_response": result, "sign": signature})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = create_alipay_preorder(
            app_id="app-1",
            seller_id="seller-1",
            private_key_pem=private_pem,
            public_key_pem=public_pem,
            notify_url="https://prism.example/api/billing/payments/alipay/notify",
            order_no="pr-order-1",
            amount_fen=6900,
            subject="企业席位",
            client=client,
        )

    assert result == {"payment_code_url": "https://qr.alipay.com/abc123", "provider_order_id": "pr-order-1"}
    assert public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")


def test_alipay_precreate_rejects_provider_error_and_untrusted_qr_scheme():
    private, private_pem, public_pem = _key_pair()

    for payload in (
        {"alipay_trade_precreate_response": {"code": "40004"}},
        {"alipay_trade_precreate_response": {"code": "10000", "qr_code": "https://evil.example/qr"}},
    ):
        response_key = "alipay_trade_precreate_response"
        response_value = payload[response_key]
        signed_payload = {response_key: response_value,
                          "sign": _signed(private, json.dumps(response_value, separators=(",", ":")).encode())}
        transport = httpx.MockTransport(lambda request, value=signed_payload: httpx.Response(200, json=value))
        with httpx.Client(transport=transport) as client:
            with pytest.raises(PaymentGatewayError):
                create_alipay_preorder(
                    app_id="app-1",
                    seller_id="seller-1",
                    private_key_pem=private_pem,
                    public_key_pem=public_pem,
                    notify_url="https://prism.example/notify",
                    order_no="order-1",
                    amount_fen=400,
                    subject="积分",
                    client=client,
                )


@pytest.mark.parametrize("node", [
    '{"code": "10000", "qr_code": "https://qr.alipay.com/a"}',
    '{\n "code":"10000", "qr_code":"https:\\/\\/qr.alipay.com\\/a", "info":"中文"\n}',
    '{"code":"10000","info":"\\u4e2d\\u6587","nested":{"key":"}\\\"["},'
    '"qr_code":"https://qr.alipay.com/a"}',
])
def test_alipay_response_verifies_exact_signed_node_bytes(node):
    private, private_pem, public_pem = _key_pair()
    signature = _signed(private, node.encode("utf-8"))
    body = ('{"other":{"nested":["}",1]},"sign":' + json.dumps(signature)
            + ',"alipay_trade_precreate_response":' + node + '}').encode("utf-8")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
        result = create_alipay_preorder(
            app_id="app-1", seller_id="seller-1", private_key_pem=private_pem, public_key_pem=public_pem,
            notify_url="https://prism.example/notify", order_no="order-1", amount_fen=400,
            subject="积分", client=client,
        )
    assert result["payment_code_url"] == "https://qr.alipay.com/a"


@pytest.mark.parametrize("case", ["duplicate_response", "duplicate_sign", "duplicate_escaped_sign", "duplicate_nested",
                                   "trailing", "trailing_comma", "bad_sign", "invalid_utf8"])
def test_alipay_response_rejects_ambiguous_or_invalid_signed_json(case):
    private, private_pem, public_pem = _key_pair()
    node = '{"code":"10000","qr_code":"https://qr.alipay.com/a"}'
    if case == "duplicate_nested":
        node = '{"code":"10000","qr_code":"https://qr.alipay.com/a","nested":{"a":1,"a":2}}'
    signature = _signed(private, node.encode())
    if case == "bad_sign":
        signature = _signed(private, b"different signed bytes")
    body = '{"alipay_trade_precreate_response":' + node + ',"sign":' + json.dumps(signature)
    if case == "duplicate_response":
        body += ',"alipay_trade_precreate_response":' + node
    if case == "duplicate_sign":
        body += ',"sign":' + json.dumps(signature)
    if case == "duplicate_escaped_sign":
        body += ',"\\u0073ign":' + json.dumps(signature)
    if case == "trailing_comma":
        body += ','
    body += '}' + (' {}' if case == "trailing" else '')
    raw = body.encode() + (b"\xff" if case == "invalid_utf8" else b"")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))) as client:
        with pytest.raises(PaymentGatewayError):
            create_alipay_preorder(
                app_id="app-1", seller_id="seller-1", private_key_pem=private_pem, public_key_pem=public_pem,
                notify_url="https://prism.example/notify", order_no="order-1", amount_fen=400,
                subject="积分", client=client,
            )


def test_wechat_native_signs_request_and_verifies_signed_response():
    merchant, merchant_pem, _ = _key_pair()
    platform, _, platform_pem = _key_pair()

    def handle(request: httpx.Request):
        auth = request.headers["Authorization"]
        fields = dict(re.findall(r'(mchid|nonce_str|timestamp|serial_no|signature)="([^"]+)"', auth))
        body = request.content.decode("utf-8")
        message = f"POST\n/v3/pay/transactions/native\n{fields['timestamp']}\n{fields['nonce_str']}\n{body}\n"
        merchant.public_key().verify(
            base64.b64decode(fields["signature"]), message.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        assert fields["mchid"] == "mch-1"
        assert fields["serial_no"] == "merchant-serial"
        assert request.url == "https://api.mch.weixin.qq.com/v3/pay/transactions/native"
        payload = json.loads(body)
        assert payload["appid"] == "app-1"
        assert payload["mchid"] == "mch-1"
        assert payload["out_trade_no"] == "pr-order-2"
        assert payload["amount"] == {"total": 400, "currency": "CNY"}
        response_body = json.dumps(
            {"code_url": "weixin://wxpay/bizpayurl?pr=abc", "prepay_id": "prepay-1"}, separators=(",", ":")
        ).encode()
        timestamp, nonce = fields["timestamp"], "provider-nonce"
        signature = _signed(platform, f"{timestamp}\n{nonce}\n".encode() + response_body + b"\n")
        return httpx.Response(
            200,
            content=response_body,
            headers={
                "Wechatpay-Timestamp": timestamp,
                "Wechatpay-Nonce": nonce,
                "Wechatpay-Serial": "platform-serial",
                "Wechatpay-Signature": signature,
                "Content-Type": "application/json",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = create_wechat_native_preorder(
            app_id="app-1",
            mchid="mch-1",
            merchant_serial="merchant-serial",
            merchant_private_key_pem=merchant_pem,
            platform_public_key_pem=platform_pem,
            platform_serial="platform-serial",
            notify_url="https://prism.example/api/billing/payments/wechat/notify",
            order_no="pr-order-2",
            amount_fen=400,
            description="积分",
            client=client,
        )

    assert result == {"payment_code_url": "weixin://wxpay/bizpayurl?pr=abc", "provider_order_id": "prepay-1"}


def test_wechat_native_rejects_unsigned_or_wrong_serial_response():
    _, merchant_pem, _ = _key_pair()
    _, _, platform_pem = _key_pair()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code_url": "weixin://wxpay/bizpayurl?pr=abc"})
        )
    ) as client:
        with pytest.raises(PaymentGatewayError):
            create_wechat_native_preorder(
                app_id="app-1",
                mchid="mch-1",
                merchant_serial="merchant-serial",
                merchant_private_key_pem=merchant_pem,
                platform_public_key_pem=platform_pem,
                platform_serial="platform-serial",
                notify_url="https://prism.example/notify",
                order_no="order-1",
                amount_fen=400,
                description="积分",
                client=client,
            )


@pytest.mark.parametrize("order_no", ["pr123", "pr" + "a" * 32, "pr你好1234", "bad order"])
def test_wechat_native_rejects_invalid_order_number_before_network(order_no):
    _, merchant_pem, _ = _key_pair()
    _, _, platform_pem = _key_pair()
    def no_request(_request):
        pytest.fail("invalid order number must fail before network")

    with httpx.Client(transport=httpx.MockTransport(no_request)) as client, pytest.raises(PaymentGatewayError):
        create_wechat_native_preorder(
            app_id="app-1", mchid="mch-1", merchant_serial="merchant-serial",
            merchant_private_key_pem=merchant_pem, platform_public_key_pem=platform_pem,
            platform_serial="platform-serial", notify_url="https://prism.example/notify",
            order_no=order_no, amount_fen=400, description="积分", client=client,
        )


def test_wechat_query_signs_exact_get_path_and_verifies_payment_result():
    from app.services.billing_payment_gateway import query_wechat_trade

    merchant, merchant_pem, _ = _key_pair()
    platform, _, platform_pem = _key_pair()

    def handle(request: httpx.Request):
        auth = request.headers["Authorization"]
        fields = dict(re.findall(r'(mchid|nonce_str|timestamp|serial_no|signature)="([^"]+)"', auth))
        assert request.method == "GET"
        assert request.url.raw_path == b"/v3/pay/transactions/out-trade-no/order%20wx?mchid=mch-1"
        message = f"GET\n{request.url.raw_path.decode()}\n{fields['timestamp']}\n{fields['nonce_str']}\n\n"
        merchant.public_key().verify(base64.b64decode(fields["signature"]), message.encode(),
                                      padding.PKCS1v15(), hashes.SHA256())
        body = json.dumps({"appid": "app-1", "mchid": "mch-1", "out_trade_no": "order wx",
                           "transaction_id": "wx-trade", "trade_state": "SUCCESS",
                           "amount": {"total": 400, "currency": "CNY"}}, separators=(",", ":")).encode()
        nonce = "platform-nonce"
        signature = _signed(platform, f"{fields['timestamp']}\n{nonce}\n".encode() + body + b"\n")
        return httpx.Response(200, content=body, headers={
            "Wechatpay-Timestamp": fields["timestamp"], "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": "platform-serial", "Wechatpay-Signature": signature,
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = query_wechat_trade(mchid="mch-1", merchant_serial="merchant-serial",
                                    merchant_private_key_pem=merchant_pem, platform_public_key_pem=platform_pem,
                                    platform_serial="platform-serial", order_no="order wx", client=client)
    assert result == {"state": "SUCCESS", "order_no": "order wx", "trade_id": "wx-trade", "amount_fen": 400,
                      "appid": "app-1", "mchid": "mch-1"}


def test_wechat_close_requires_signed_204_response():
    from app.services.billing_payment_gateway import close_wechat_trade

    merchant, merchant_pem, _ = _key_pair()
    platform, _, platform_pem = _key_pair()

    def handle(request: httpx.Request):
        auth = request.headers["Authorization"]
        fields = dict(re.findall(r'(mchid|nonce_str|timestamp|serial_no|signature)="([^"]+)"', auth))
        assert request.method == "POST"
        assert request.url.path == "/v3/pay/transactions/out-trade-no/order-1/close"
        assert json.loads(request.content) == {"mchid": "mch-1"}
        message = f"POST\n{request.url.raw_path.decode()}\n{fields['timestamp']}\n{fields['nonce_str']}\n".encode()
        message += request.content + b"\n"
        merchant.public_key().verify(base64.b64decode(fields["signature"]), message,
                                      padding.PKCS1v15(), hashes.SHA256())
        nonce = "provider-nonce"
        signature = _signed(platform, f"{fields['timestamp']}\n{nonce}\n\n".encode())
        return httpx.Response(204, headers={
            "Wechatpay-Timestamp": fields["timestamp"], "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": "platform-serial", "Wechatpay-Signature": signature,
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = close_wechat_trade(mchid="mch-1", merchant_serial="merchant-serial",
                                    merchant_private_key_pem=merchant_pem, platform_public_key_pem=platform_pem,
                                    platform_serial="platform-serial", order_no="order-1", client=client)
    assert result == {"state": "CLOSED", "order_no": "order-1"}


def test_wechat_refund_uses_stable_refund_number_and_provider_state():
    from app.services.billing_payment_gateway import create_wechat_refund

    merchant, merchant_pem, _ = _key_pair()
    platform, _, platform_pem = _key_pair()

    def handle(request: httpx.Request):
        auth = request.headers["Authorization"]
        fields = dict(re.findall(r'(mchid|nonce_str|timestamp|serial_no|signature)="([^"]+)"', auth))
        body = json.loads(request.content)
        assert request.url.path == "/v3/refund/domestic/refunds"
        assert body == {"out_trade_no": "order-1", "out_refund_no": "refund-stable",
                        "reason": "unused credits", "amount": {"refund": 400, "total": 400, "currency": "CNY"}}
        message = f"POST\n/v3/refund/domestic/refunds\n{fields['timestamp']}\n{fields['nonce_str']}\n".encode()
        message += request.content + b"\n"
        merchant.public_key().verify(base64.b64decode(fields["signature"]), message,
                                      padding.PKCS1v15(), hashes.SHA256())
        response_body = json.dumps({"out_trade_no": "order-1", "out_refund_no": "refund-stable",
                                    "refund_id": "wx-refund", "status": "SUCCESS"},
                                   separators=(",", ":")).encode()
        nonce = "provider-nonce"
        signature = _signed(platform, f"{fields['timestamp']}\n{nonce}\n".encode() + response_body + b"\n")
        return httpx.Response(200, content=response_body, headers={
            "Wechatpay-Timestamp": fields["timestamp"], "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": "platform-serial", "Wechatpay-Signature": signature,
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = create_wechat_refund(mchid="mch-1", merchant_serial="merchant-serial",
                                      merchant_private_key_pem=merchant_pem, platform_public_key_pem=platform_pem,
                                      platform_serial="platform-serial", order_no="order-1",
                                      refund_no="refund-stable", amount_fen=400, total_fen=400,
                                      reason="unused credits", client=client)
    # Application acceptance is not a final refund receipt, even if its
    # signed response already carries SUCCESS; only query may finalize it.
    assert result == {"state": "PROCESSING", "provider_refund_id": "wx-refund"}


@pytest.mark.parametrize("field,value", [
    ("out_trade_no", "different-order"), ("out_trade_no", None),
    ("out_refund_no", "different-refund"), ("out_refund_no", None),
    ("amount", None), ("amount", {"total": 401, "refund": 400}),
    ("amount", {"total": 400, "refund": 399}), ("amount", {"total": 400}),
    ("amount", {"total": "400", "refund": 400}),
    ("amount", {"total": 400, "refund": 400}),
    ("amount", {"total": 400, "refund": 400, "currency": "USD"}),
])
def test_wechat_refund_query_rejects_unbound_signed_order_and_amount(field, value):
    from app.services.billing_payment_gateway import query_wechat_refund

    _, merchant_pem, _ = _key_pair()
    platform, _, platform_pem = _key_pair()
    def handle(request):
        fields = dict(re.findall(r'(mchid|nonce_str|timestamp|serial_no|signature)="([^"]+)"',
                                 request.headers["Authorization"]))
        payload = {"out_trade_no": "order-1", "out_refund_no": "refund-1", "refund_id": "wx-1",
                   "status": "SUCCESS", "amount": {"total": 400, "refund": 400, "currency": "CNY"}}
        payload[field] = value
        body = json.dumps(payload, separators=(",", ":")).encode()
        nonce = "provider-nonce"
        signature = _signed(platform, f"{fields['timestamp']}\n{nonce}\n".encode() + body + b"\n")
        return httpx.Response(200, content=body, headers={
            "Wechatpay-Timestamp": fields["timestamp"], "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": "platform-serial", "Wechatpay-Signature": signature,
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(PaymentGatewayError):
        query_wechat_refund(mchid="mch-1", merchant_serial="merchant-serial",
                            merchant_private_key_pem=merchant_pem, platform_public_key_pem=platform_pem,
                            platform_serial="platform-serial", order_no="order-1", refund_no="refund-1",
                            amount_fen=400, total_fen=400, client=client)


def test_wechat_refund_query_accepts_signed_amounts_with_coupon():
    from app.services.billing_payment_gateway import query_wechat_refund

    _, merchant_pem, _ = _key_pair()
    platform, _, platform_pem = _key_pair()
    def handle(request):
        fields = dict(re.findall(r'(mchid|nonce_str|timestamp|serial_no|signature)="([^"]+)"',
                                 request.headers["Authorization"]))
        assert request.url.path == "/v3/refund/domestic/refunds/refund-1"
        body = json.dumps({"out_trade_no": "order-1", "out_refund_no": "refund-1", "refund_id": "wx-1",
                           "status": "SUCCESS", "amount": {"total": 400, "refund": 400, "currency": "CNY",
                           "payer_total": 300, "payer_refund": 300}}, separators=(",", ":")).encode()
        nonce = "provider-nonce"
        signature = _signed(platform, f"{fields['timestamp']}\n{nonce}\n".encode() + body + b"\n")
        return httpx.Response(200, content=body, headers={
            "Wechatpay-Timestamp": fields["timestamp"], "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": "platform-serial", "Wechatpay-Signature": signature,
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = query_wechat_refund(mchid="mch-1", merchant_serial="merchant-serial",
                                     merchant_private_key_pem=merchant_pem, platform_public_key_pem=platform_pem,
                                     platform_serial="platform-serial", order_no="order-1", refund_no="refund-1",
                                     amount_fen=400, total_fen=400, client=client)
    assert result == {"state": "SUCCESS", "provider_refund_id": "wx-1"}


@pytest.mark.parametrize("currency", [None, "USD"])
def test_wechat_bad_currency_query_keeps_refund_frozen(db, monkeypatch, currency):
    from datetime import timedelta

    from fastapi import HTTPException

    from app.api.v1 import billing
    from app.models.billing_ledger import BillingCreditBatch
    from app.models.user import User
    from app.services import billing_checkout_service as checkout
    from app.services.billing_payment_gateway import query_wechat_refund

    user = User(username="wechat-currency-user", password="x", role="user", status=1)
    admin = User(username="wechat-currency-admin", password="x", role="super_admin", status=1)
    db.add_all([user, admin])
    db.commit()
    order = checkout.create_order(db, user_id=user.id, product="personal_credits", quantity=1, channel="wechat")
    checkout._apply_verified(db, order, {"trade_id": "wx-currency-check"})
    db.commit()
    refund = checkout.request_refund(db, order_no=order.order_no, requester_id=user.id, reason="退款")
    checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=admin.id)
    refund.updated_at -= timedelta(seconds=16)
    db.commit()
    batch = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order.order_no}").one()
    frozen = batch.refund_reserved_milli
    assert frozen == batch.remaining_milli and frozen > 0

    _, merchant_pem, _ = _key_pair()
    platform, _, platform_pem = _key_pair()

    def handle(request):
        fields = dict(re.findall(r'(mchid|nonce_str|timestamp|serial_no|signature)="([^\"]+)"',
                                 request.headers["Authorization"]))
        amount = {"total": order.amount_fen, "refund": refund.amount_fen}
        if currency is not None:
            amount["currency"] = currency
        body = json.dumps({"out_trade_no": order.order_no, "out_refund_no": refund.refund_no,
                           "refund_id": "wx-1", "status": "SUCCESS", "amount": amount},
                          separators=(",", ":")).encode()
        nonce = "provider-nonce"
        signature = _signed(platform, f"{fields['timestamp']}\n{nonce}\n".encode() + body + b"\n")
        return httpx.Response(200, content=body, headers={
            "Wechatpay-Timestamp": fields["timestamp"], "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": "platform-serial", "Wechatpay-Signature": signature,
        })

    def query(current_order, current_refund):
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            return query_wechat_refund(
                mchid="mch-1", merchant_serial="merchant-serial", merchant_private_key_pem=merchant_pem,
                platform_public_key_pem=platform_pem, platform_serial="platform-serial",
                order_no=current_order.order_no, refund_no=current_refund.refund_no,
                amount_fen=current_refund.amount_fen, total_fen=current_order.amount_fen, client=client,
            )

    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    monkeypatch.setattr(billing, "_provider_refund_query", query)
    with pytest.raises(HTTPException) as error:
        billing.reconcile_refund(refund.refund_no, db=db, _admin=admin)
    assert error.value.status_code == 502
    db.refresh(refund)
    db.refresh(order)
    db.refresh(batch)
    assert refund.status == "processing" and refund.reconciliation_claim_token is None
    assert order.status == "paid" and batch.refund_reserved_milli == frozen


@pytest.mark.parametrize("field,value", [
    ("out_request_no", "other-refund"), ("refund_amount", "3.99"),
    ("total_amount", "4.01"), ("refund_amount", "not-money"),
])
def test_alipay_refund_query_rejects_signed_mismatched_optional_fields(field, value):
    from app.services.billing_payment_gateway import query_alipay_refund

    private, private_pem, public_pem = _key_pair()
    def handle(request):
        result = {"code": "10000", "out_trade_no": "order-1", "out_request_no": "refund-1",
                  "refund_amount": "4.00", "total_amount": "4.00", "refund_status": "REFUND_SUCCESS"}
        result[field] = value
        signature = _signed(private, json.dumps(result, separators=(",", ":")).encode())
        return httpx.Response(200, json={"alipay_trade_fastpay_refund_query_response": result, "sign": signature})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(PaymentGatewayError):
        query_alipay_refund(app_id="app-1", private_key_pem=private_pem, public_key_pem=public_pem,
                            order_no="order-1", refund_no="refund-1", amount_fen=400, total_fen=400, client=client)


@pytest.mark.parametrize("omit", ["out_request_no", "refund_amount"])
def test_alipay_success_with_missing_optional_binding_is_not_accepted_as_verified(omit):
    from app.services.billing_payment_gateway import query_alipay_refund

    private, private_pem, public_pem = _key_pair()
    def handle(request):
        result = {"code": "10000", "out_trade_no": "order-1", "out_request_no": "refund-1",
                  "refund_amount": "4.00", "total_amount": "4.00", "refund_status": "REFUND_SUCCESS"}
        del result[omit]
        signature = _signed(private, json.dumps(result, separators=(",", ":")).encode())
        return httpx.Response(200, json={"alipay_trade_fastpay_refund_query_response": result, "sign": signature})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = query_alipay_refund(app_id="app-1", private_key_pem=private_pem, public_key_pem=public_pem,
                                     order_no="order-1", refund_no="refund-1", amount_fen=400,
                                     total_fen=400, client=client)
    assert result["state"] == "UNKNOWN"


@pytest.mark.parametrize("status,expected", [
    ("REFUND_SUCCESS", "SUCCESS"), (None, "PROCESSING"), ("REFUND_FAIL", "UNKNOWN"),
    ("future_provider_status", "UNKNOWN"),
])
def test_alipay_refund_query_accepts_matched_signed_evidence_or_pending_status(status, expected):
    from app.services.billing_payment_gateway import query_alipay_refund

    private, private_pem, public_pem = _key_pair()
    def handle(request):
        params = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        assert json.loads(params["biz_content"]) == {"out_trade_no": "order-1", "out_request_no": "refund-1"}
        result = {"code": "10000", "out_trade_no": "order-1", "trade_no": "ali-trade"}
        if status:
            result.update({"out_request_no": "refund-1", "refund_amount": "4.00",
                           "total_amount": "4.00", "refund_status": status})
        signature = _signed(private, json.dumps(result, separators=(",", ":")).encode())
        return httpx.Response(200, json={"alipay_trade_fastpay_refund_query_response": result, "sign": signature})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = query_alipay_refund(app_id="app-1", private_key_pem=private_pem, public_key_pem=public_pem,
                                     order_no="order-1", refund_no="refund-1", amount_fen=400,
                                     total_fen=400, client=client)
    assert result == {"state": expected, "provider_refund_id": "ali-trade"}


@pytest.mark.parametrize("channel,method", [("alipay", "query_alipay_refund"), ("wechat", "query_wechat_refund")])
def test_provider_refund_query_passes_order_and_amount_from_persisted_entities(monkeypatch, channel, method):
    from app.api.v1 import billing
    from app.services import billing_payment_gateway

    captured = {}
    def query(**kwargs):
        captured.update(kwargs)
        return {"state": "PROCESSING"}

    monkeypatch.setattr(billing, "_read_key", lambda *args: b"test-key")
    monkeypatch.setattr(billing_payment_gateway, method, query)
    order = SimpleNamespace(channel=channel, order_no="order-1", amount_fen=400)
    refund = SimpleNamespace(refund_no="refund-1", amount_fen=400)
    assert billing._provider_refund_query(order, refund) == {"state": "PROCESSING"}
    assert {key: captured[key] for key in ("order_no", "refund_no", "amount_fen", "total_fen")} == {
        "order_no": "order-1", "refund_no": "refund-1", "amount_fen": 400, "total_fen": 400,
    }
