"""Alipay and WeChat Pay pre-order APIs.

This module only creates a provider-side payment request. It does not mark an
order paid; that state can only be changed by a verified provider notification
or a separately verified order query.
"""

import base64
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services.billing_payment_verification import _verify

ALIPAY_GATEWAY = "https://openapi.alipay.com/gateway.do"
WECHAT_GATEWAY = "https://api.mch.weixin.qq.com"
PAYMENT_TIMEOUT_SECONDS = 10.0


class PaymentGatewayError(ValueError):
    """Provider API failed or returned an invalid pre-order response."""


def _private_key(private_key_pem: bytes):
    if not isinstance(private_key_pem, bytes) or not private_key_pem:
        raise PaymentGatewayError("商户私钥缺失")
    try:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise PaymentGatewayError("商户私钥无效") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise PaymentGatewayError("商户 RSA 私钥长度不足")
    return key


def _sign(private_key_pem: bytes, message: bytes) -> str:
    signature = _private_key(private_key_pem).sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def _post(client: httpx.Client | None, url: str, **kwargs) -> httpx.Response:
    try:
        if client is not None:
            response = client.post(url, follow_redirects=False, **kwargs)
        else:
            with httpx.Client(timeout=PAYMENT_TIMEOUT_SECONDS, follow_redirects=False) as session:
                response = session.post(url, **kwargs)
    except httpx.HTTPError as exc:
        raise PaymentGatewayError("支付渠道连接失败；订单状态需要查询确认") from exc
    return response


def create_alipay_preorder(
    *,
    app_id: str,
    seller_id: str,
    private_key_pem: bytes,
    public_key_pem: bytes,
    notify_url: str,
    order_no: str,
    amount_fen: int,
    subject: str,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Call `alipay.trade.precreate` and return its QR payload after validation."""
    if (
        not all(isinstance(value, str) and value.strip() for value in (app_id, seller_id, notify_url, order_no))
        or type(amount_fen) is not int
        or amount_fen <= 0
        or not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 127
    ):
        raise PaymentGatewayError("支付宝预下单参数无效")
    result = _alipay_call(
        method="alipay.trade.precreate", app_id=app_id, private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        biz_content={"out_trade_no": order_no, "total_amount": f"{amount_fen // 100}.{amount_fen % 100:02d}",
                     "subject": subject, "timeout_express": "15m"},
        client=client, now=now, notify_url=notify_url,
    )
    if result.get("code") != "10000":
        raise PaymentGatewayError("支付宝未接受预下单请求")
    qr_code = result.get("qr_code")
    if not isinstance(qr_code, str) or not qr_code.startswith("https://qr.alipay.com/") or len(qr_code) > 2048:
        raise PaymentGatewayError("支付宝二维码内容无效")
    return {"payment_code_url": qr_code, "provider_order_id": order_no}


def _wechat_authorization(
    *, mchid: str, serial: str, private_key_pem: bytes, method: str, path: str, body: str, timestamp: str, nonce: str
) -> str:
    message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
    signature = _sign(private_key_pem, message)
    return (
        "WECHATPAY2-SHA256-RSA2048 "
        f'mchid="{mchid}",nonce_str="{nonce}",timestamp="{timestamp}",'
        f'serial_no="{serial}",signature="{signature}"'
    )


def _verify_wechat_response(
    response: httpx.Response, public_key_pem: bytes, expected_serial: str, now_timestamp: int,
    *, allow_empty: bool = False,
) -> dict:
    headers = {key.lower(): value for key, value in response.headers.items()}
    try:
        timestamp = headers["wechatpay-timestamp"]
        nonce = headers["wechatpay-nonce"]
        serial = headers["wechatpay-serial"]
        signature = headers["wechatpay-signature"]
        if serial != expected_serial:
            raise PaymentGatewayError("微信支付应答证书编号不匹配")
        if abs(now_timestamp - int(timestamp)) > 300:
            raise PaymentGatewayError("微信支付应答时间超出允许范围")
        _verify(public_key_pem, signature, f"{timestamp}\n{nonce}\n".encode() + response.content + b"\n")
        value = response.json() if response.content else ({} if allow_empty else None)
    except (KeyError, ValueError, TypeError) as exc:
        if isinstance(exc, PaymentGatewayError):
            raise
        raise PaymentGatewayError("微信支付应答验签或解析失败") from exc
    if not isinstance(value, dict):
        raise PaymentGatewayError("微信支付应答格式无效")
    return value


def _alipay_response(body: bytes, response_name: str, public_key_pem: bytes) -> dict:
    """Verify the exact response-node bytes, then return its parsed fields.

    JSON has no canonical serialization: whitespace and escape sequences are
    part of Alipay's signed text.  Decode the top-level object incrementally so
    the original node can be isolated without matching braces inside strings.
    Duplicate keys anywhere are rejected to avoid parser interpretation gaps.
    """
    from app.services.billing_payment_verification import PaymentVerificationError, _verify

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("invalid JSON constant")

    def skip_space(text, index):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index

    try:
        text = body.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        index = skip_space(text, 0)
        if index >= len(text) or text[index] != "{":
            raise ValueError("response is not an object")
        index += 1
        fields = {}
        signed_node = None
        while True:
            index = skip_space(text, index)
            if index < len(text) and text[index] == "}":
                index += 1
                break
            key, index = decoder.raw_decode(text, index)
            if not isinstance(key, str) or key in fields:
                raise ValueError("invalid or duplicate JSON key")
            index = skip_space(text, index)
            if index >= len(text) or text[index] != ":":
                raise ValueError("missing colon")
            start = skip_space(text, index + 1)
            value, index = decoder.raw_decode(text, start)
            fields[key] = value
            if key == response_name:
                signed_node = text[start:index].encode("utf-8")
            index = skip_space(text, index)
            if index < len(text) and text[index] == ",":
                index += 1
                if skip_space(text, index) >= len(text) or text[skip_space(text, index)] == "}":
                    raise ValueError("trailing comma")
                continue
            if index < len(text) and text[index] == "}":
                index += 1
                break
            raise ValueError("invalid object separator")
        if skip_space(text, index) != len(text):
            raise ValueError("trailing JSON")
        response, signature = fields.get(response_name), fields.get("sign")
        if not isinstance(response, dict) or not isinstance(signature, str) or not signature or signed_node is None:
            raise ValueError("response or signature missing")
    except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise PaymentGatewayError("支付宝响应格式无效") from exc
    try:
        _verify(public_key_pem, signature, signed_node)
    except PaymentVerificationError as exc:
        raise PaymentGatewayError("支付宝响应验签失败") from exc
    return response


def _alipay_call(*, method: str, app_id: str, private_key_pem: bytes, public_key_pem: bytes,
                 biz_content: dict, client: httpx.Client | None = None,
                 now: datetime | None = None, notify_url: str | None = None) -> dict:
    if not all(isinstance(value, str) and value.strip() for value in (method, app_id)):
        raise PaymentGatewayError("支付宝接口配置无效")
    observed_at = _utc(now) if now is not None else datetime.now(timezone.utc)
    params = {
        "app_id": app_id,
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": observed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    if notify_url is not None:
        params["notify_url"] = notify_url
    canonical = "&".join(f"{key}={params[key]}" for key in sorted(params)).encode("utf-8")
    params["sign"] = _sign(private_key_pem, canonical)
    response = _post(client, ALIPAY_GATEWAY, data=params, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise PaymentGatewayError("支付宝接口请求失败；结果需要查询确认")
    response_name = method.replace(".", "_") + "_response"
    return _alipay_response(response.content, response_name, public_key_pem)


def query_alipay_trade(*, app_id: str, private_key_pem: bytes, public_key_pem: bytes,
                       order_no: str, amount_fen: int, client: httpx.Client | None = None,
                       now: datetime | None = None) -> dict:
    if (not isinstance(order_no, str) or not order_no or len(order_no) > 64
            or type(amount_fen) is not int or amount_fen <= 0):
        raise PaymentGatewayError("支付宝查单参数无效")
    result = _alipay_call(method="alipay.trade.query", app_id=app_id,
                          private_key_pem=private_key_pem, public_key_pem=public_key_pem,
                          biz_content={"out_trade_no": order_no}, client=client, now=now)
    if result.get("sub_code") == "ACQ.TRADE_NOT_EXIST":
        return {"state": "NOT_FOUND", "order_no": order_no}
    if result.get("code") != "10000":
        raise PaymentGatewayError("支付宝查单未返回确定结果")
    if result.get("out_trade_no") != order_no:
        raise PaymentGatewayError("支付宝查单订单号不匹配")
    status = result.get("trade_status")
    amount = result.get("total_amount")
    if status in {"TRADE_SUCCESS", "TRADE_FINISHED"} and amount is None:
        raise PaymentGatewayError("支付宝成功订单缺少支付金额")
    if amount is not None:
        try:
            from decimal import Decimal
            matches = Decimal(amount) * 100 == Decimal(amount_fen)
        except Exception as exc:
            raise PaymentGatewayError("支付宝查单金额格式无效") from exc
        if not matches:
            raise PaymentGatewayError("支付宝查单金额不匹配")
    if status in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
        trade_id = result.get("trade_no")
        if not isinstance(trade_id, str) or not trade_id:
            raise PaymentGatewayError("支付宝查单缺少交易号")
    return {"state": status if isinstance(status, str) else "UNKNOWN", "order_no": order_no,
            "trade_id": result.get("trade_no"), "amount_fen": amount_fen}


def close_alipay_trade(*, app_id: str, private_key_pem: bytes, public_key_pem: bytes,
                       order_no: str, client: httpx.Client | None = None,
                       now: datetime | None = None) -> dict:
    result = _alipay_call(method="alipay.trade.close", app_id=app_id,
                          private_key_pem=private_key_pem, public_key_pem=public_key_pem,
                          biz_content={"out_trade_no": order_no}, client=client, now=now)
    if result.get("code") != "10000" or result.get("out_trade_no") != order_no:
        raise PaymentGatewayError("支付宝未确认关单")
    return {"state": "CLOSED", "order_no": order_no, "trade_id": result.get("trade_no")}


def create_alipay_refund(*, app_id: str, private_key_pem: bytes, public_key_pem: bytes,
                         order_no: str, refund_no: str, amount_fen: int, reason: str,
                         client: httpx.Client | None = None, now: datetime | None = None) -> dict:
    if (not isinstance(refund_no, str) or not refund_no or len(refund_no) > 64
            or type(amount_fen) is not int or amount_fen <= 0):
        raise PaymentGatewayError("支付宝退款参数无效")
    result = _alipay_call(method="alipay.trade.refund", app_id=app_id,
                          private_key_pem=private_key_pem, public_key_pem=public_key_pem,
                          biz_content={"out_trade_no": order_no,
                                       "refund_amount": f"{amount_fen // 100}.{amount_fen % 100:02d}",
                                       "refund_reason": reason[:200], "out_request_no": refund_no},
                          client=client, now=now)
    if result.get("code") != "10000" or result.get("out_trade_no") != order_no:
        raise PaymentGatewayError("支付宝未受理退款")
    if result.get("fund_change") == "Y":
        return {"state": "SUCCESS", "provider_refund_id": result.get("trade_no")}
    return {"state": "UNKNOWN", "provider_refund_id": result.get("trade_no")}


def query_alipay_refund(*, app_id: str, private_key_pem: bytes, public_key_pem: bytes,
                        order_no: str, refund_no: str, amount_fen: int, total_fen: int,
                        client: httpx.Client | None = None,
                        now: datetime | None = None) -> dict:
    if (not order_no or not refund_no or type(amount_fen) is not int or type(total_fen) is not int
            or amount_fen <= 0 or total_fen <= 0 or amount_fen > total_fen):
        raise PaymentGatewayError("支付宝退款查单参数无效")
    result = _alipay_call(method="alipay.trade.fastpay.refund.query", app_id=app_id,
                          private_key_pem=private_key_pem, public_key_pem=public_key_pem,
                          biz_content={"out_trade_no": order_no, "out_request_no": refund_no},
                          client=client, now=now)
    if result.get("code") != "10000" or result.get("out_trade_no") != order_no:
        raise PaymentGatewayError("支付宝退款查单未返回确定结果")
    request_no = result.get("out_request_no")
    if request_no is not None and request_no != refund_no:
        raise PaymentGatewayError("支付宝退款查询编号不匹配")
    for key, expected in (("refund_amount", amount_fen), ("total_amount", total_fen)):
        value = result.get(key)
        if value is None:
            continue
        try:
            if not isinstance(value, str) or not Decimal(value).is_finite() or Decimal(value) * 100 != expected:
                raise PaymentGatewayError("支付宝退款查询金额不匹配")
        except InvalidOperation as exc:
            raise PaymentGatewayError("支付宝退款查询金额格式无效") from exc
    status = result.get("refund_status")
    # The documented query contract defines REFUND_SUCCESS. An absent status
    # can be an early query; other values do not prove an irreversible failure.
    mapped = "SUCCESS" if status == "REFUND_SUCCESS" else "PROCESSING" if status is None else "UNKNOWN"
    # The provider marks these response fields conditional. Missing evidence
    # cannot establish that a successful refund has the expected identity and amount.
    if mapped == "SUCCESS" and (request_no is None or result.get("refund_amount") is None):
        mapped = "UNKNOWN"
    return {"state": mapped,
            "provider_refund_id": result.get("trade_no")}


def _wechat_request(*, method: str, path: str, mchid: str, merchant_serial: str,
                    merchant_private_key_pem: bytes, platform_public_key_pem: bytes,
                    platform_serial: str, payload: dict | None = None,
                    client: httpx.Client | None = None, now: datetime | None = None,
                    allow_empty: bool = False, allow_not_found: bool = False) -> dict:
    observed_at = _utc(now) if now is not None else datetime.now(timezone.utc)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    timestamp = str(int(observed_at.timestamp()))
    nonce = secrets.token_hex(16)
    headers = {
        "Authorization": _wechat_authorization(
            mchid=mchid, serial=merchant_serial, private_key_pem=merchant_private_key_pem,
            method=method, path=path, body=body, timestamp=timestamp, nonce=nonce,
        ),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        if client is not None:
            response = client.request(method, f"{WECHAT_GATEWAY}{path}", content=body or None,
                                      headers=headers, follow_redirects=False)
        else:
            with httpx.Client(timeout=PAYMENT_TIMEOUT_SECONDS, follow_redirects=False) as session:
                response = session.request(method, f"{WECHAT_GATEWAY}{path}", content=body or None, headers=headers)
    except httpx.HTTPError as exc:
        raise PaymentGatewayError("微信支付连接失败；状态需要查询确认") from exc
    expected_status = 204 if allow_empty else 200
    if allow_not_found and response.status_code == 404:
        result = _verify_wechat_response(response, platform_public_key_pem, platform_serial,
                                         int(timestamp))
        if result.get("code") in {"ORDER_NOT_EXIST", "REFUND_NOT_EXIST"}:
            return {"__not_found__": True}
        raise PaymentGatewayError("微信支付查询返回非预期错误")
    if response.status_code != expected_status:
        raise PaymentGatewayError("微信支付接口未确认操作")
    return _verify_wechat_response(response, platform_public_key_pem, platform_serial,
                                   int(timestamp), allow_empty=allow_empty)


def query_wechat_trade(*, mchid: str, merchant_serial: str, merchant_private_key_pem: bytes,
                       platform_public_key_pem: bytes, platform_serial: str, order_no: str,
                       client: httpx.Client | None = None, now: datetime | None = None) -> dict:
    if not isinstance(order_no, str) or not order_no or len(order_no) > 64:
        raise PaymentGatewayError("微信查单参数无效")
    path = f"/v3/pay/transactions/out-trade-no/{quote(order_no, safe='')}?mchid={quote(mchid, safe='')}"
    result = _wechat_request(method="GET", path=path, mchid=mchid,
                             merchant_serial=merchant_serial, merchant_private_key_pem=merchant_private_key_pem,
                             platform_public_key_pem=platform_public_key_pem, platform_serial=platform_serial,
                             client=client, now=now, allow_not_found=True)
    if result.get("__not_found__"):
        return {"state": "NOT_FOUND", "order_no": order_no}
    if result.get("out_trade_no") != order_no:
        raise PaymentGatewayError("微信查单订单号不匹配")
    return {"state": result.get("trade_state", "UNKNOWN"), "order_no": order_no,
            "trade_id": result.get("transaction_id"), "amount_fen": result.get("amount", {}).get("total"),
            "appid": result.get("appid"), "mchid": result.get("mchid")}


def close_wechat_trade(*, mchid: str, merchant_serial: str, merchant_private_key_pem: bytes,
                       platform_public_key_pem: bytes, platform_serial: str, order_no: str,
                       client: httpx.Client | None = None, now: datetime | None = None) -> dict:
    path = f"/v3/pay/transactions/out-trade-no/{quote(order_no, safe='')}/close"
    _wechat_request(method="POST", path=path, mchid=mchid, merchant_serial=merchant_serial,
                    merchant_private_key_pem=merchant_private_key_pem,
                    platform_public_key_pem=platform_public_key_pem, platform_serial=platform_serial,
                    payload={"mchid": mchid}, client=client, now=now, allow_empty=True)
    return {"state": "CLOSED", "order_no": order_no}


def create_wechat_refund(*, mchid: str, merchant_serial: str, merchant_private_key_pem: bytes,
                         platform_public_key_pem: bytes, platform_serial: str,
                         order_no: str, refund_no: str, amount_fen: int, total_fen: int,
                         reason: str, client: httpx.Client | None = None,
                         now: datetime | None = None) -> dict:
    if (not order_no or len(order_no) > 64 or not refund_no or len(refund_no) > 64
            or type(amount_fen) is not int or type(total_fen) is not int
            or amount_fen <= 0 or total_fen <= 0 or amount_fen > total_fen):
        raise PaymentGatewayError("微信退款金额或订单编号无效")
    path = "/v3/refund/domestic/refunds"
    result = _wechat_request(
        method="POST", path=path, mchid=mchid, merchant_serial=merchant_serial,
        merchant_private_key_pem=merchant_private_key_pem, platform_public_key_pem=platform_public_key_pem,
        platform_serial=platform_serial,
        payload={"out_trade_no": order_no, "out_refund_no": refund_no, "reason": reason[:80],
                 "amount": {"refund": amount_fen, "total": total_fen, "currency": "CNY"}},
        client=client, now=now,
    )
    if result.get("out_trade_no") != order_no or result.get("out_refund_no") != refund_no:
        raise PaymentGatewayError("微信退款响应与本地退款单不匹配")
    state = result.get("status")
    # The application response confirms acceptance, not final refund settlement.
    # Retain the freeze until a separately signed query confirms SUCCESS.
    mapped = {"SUCCESS": "PROCESSING", "PROCESSING": "PROCESSING", "ABNORMAL": "PROCESSING",
              "CLOSED": "FAILED"}.get(state, "UNKNOWN")
    return {"state": mapped,
            "provider_refund_id": result.get("refund_id")}


def query_wechat_refund(*, mchid: str, merchant_serial: str, merchant_private_key_pem: bytes,
                        platform_public_key_pem: bytes, platform_serial: str,
                        order_no: str, refund_no: str, amount_fen: int, total_fen: int,
                        client: httpx.Client | None = None,
                        now: datetime | None = None) -> dict:
    if (not order_no or not refund_no or type(amount_fen) is not int or type(total_fen) is not int
            or amount_fen <= 0 or total_fen <= 0 or amount_fen > total_fen):
        raise PaymentGatewayError("微信退款查单参数无效")
    path = f"/v3/refund/domestic/refunds/{quote(refund_no, safe='')}"
    result = _wechat_request(method="GET", path=path, mchid=mchid, merchant_serial=merchant_serial,
                             merchant_private_key_pem=merchant_private_key_pem,
                             platform_public_key_pem=platform_public_key_pem, platform_serial=platform_serial,
                             client=client, now=now, allow_not_found=True)
    if result.get("__not_found__"):
        return {"state": "NOT_FOUND", "provider_refund_id": None}
    amount = result.get("amount")
    if (result.get("out_refund_no") != refund_no or result.get("out_trade_no") != order_no
            or not isinstance(amount, dict) or type(amount.get("total")) is not int
            or type(amount.get("refund")) is not int or amount["total"] != total_fen
            or amount["refund"] != amount_fen or amount.get("currency") != "CNY"):
        raise PaymentGatewayError("微信退款查询订单或金额不匹配")
    status = result.get("status")
    mapped = {"SUCCESS": "SUCCESS", "PROCESSING": "PROCESSING", "ABNORMAL": "PROCESSING",
              "CLOSED": "FAILED"}.get(status, "UNKNOWN")
    return {"state": mapped,
            "provider_refund_id": result.get("refund_id")}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create_wechat_native_preorder(
    *,
    app_id: str,
    mchid: str,
    merchant_serial: str,
    merchant_private_key_pem: bytes,
    platform_public_key_pem: bytes,
    platform_serial: str,
    notify_url: str,
    order_no: str,
    amount_fen: int,
    description: str,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Call WeChat Pay API v3 Native order and verify the signed response."""
    if (
        not all(
            isinstance(value, str) and value.strip()
            for value in (app_id, mchid, merchant_serial, platform_serial, notify_url, order_no)
        )
        or type(amount_fen) is not int
        or amount_fen <= 0
        or not re.fullmatch(r"[A-Za-z0-9_|*\-]{6,32}", order_no)
        or not isinstance(description, str)
        or not description.strip()
        or len(description) > 127
    ):
        raise PaymentGatewayError("微信支付预下单参数无效")
    observed_at = _utc(now) if now is not None else datetime.now(timezone.utc)
    expiry = observed_at + timedelta(minutes=15)
    payload = {
        "appid": app_id,
        "mchid": mchid,
        "description": description,
        "out_trade_no": order_no,
        "time_expire": expiry.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "notify_url": notify_url,
        "amount": {"total": amount_fen, "currency": "CNY"},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path = "/v3/pay/transactions/native"
    timestamp = str(int(observed_at.timestamp()))
    nonce = secrets.token_hex(16)
    headers = {
        "Authorization": _wechat_authorization(
            mchid=mchid,
            serial=merchant_serial,
            private_key_pem=merchant_private_key_pem,
            method="POST",
            path=path,
            body=body,
            timestamp=timestamp,
            nonce=nonce,
        ),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        if client is not None:
            response = client.post(f"{WECHAT_GATEWAY}{path}", content=body, headers=headers)
        else:
            with httpx.Client(timeout=PAYMENT_TIMEOUT_SECONDS, follow_redirects=False) as session:
                response = session.post(f"{WECHAT_GATEWAY}{path}", content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise PaymentGatewayError("微信支付连接失败；订单状态需要查询确认") from exc
    if response.status_code != 200:
        raise PaymentGatewayError("微信支付预下单失败；请查询渠道订单状态")
    result = _verify_wechat_response(response, platform_public_key_pem, platform_serial, int(timestamp))
    code_url = result.get("code_url")
    if not isinstance(code_url, str) or not code_url.startswith("weixin://") or len(code_url) > 2048:
        raise PaymentGatewayError("微信支付二维码内容无效")
    prepay_id = result.get("prepay_id")
    if prepay_id is not None and (not isinstance(prepay_id, str) or not prepay_id or len(prepay_id) > 128):
        raise PaymentGatewayError("微信预支付编号无效")
    return {"payment_code_url": code_url, "provider_order_id": prepay_id or quote(order_no, safe="")}
