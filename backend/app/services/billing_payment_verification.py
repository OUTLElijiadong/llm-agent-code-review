"""Verify official payment notifications, without changing order state or granting credits.

Channel identities, keys and order amount must come from trusted server configuration
and the saved order, never from the callback itself. Adapter activation and durable
idempotent order processing are deliberately not implemented here.
"""

import base64
import binascii
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Mapping

from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PaymentVerificationError(ValueError):
    pass


def _public_key(data):
    if not isinstance(data, bytes) or not data:
        raise PaymentVerificationError("缺少支付平台公钥")
    try:
        key = (x509.load_pem_x509_certificate(data).public_key()
               if b"BEGIN CERTIFICATE" in data else serialization.load_pem_public_key(data))
    except (ValueError, TypeError) as exc:
        raise PaymentVerificationError("支付平台公钥无效") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise PaymentVerificationError("支付平台 RSA 公钥无效")
    return key


def _verify(key_pem, signature, signed):
    try:
        decoded = base64.b64decode(signature, validate=True)
        _public_key(key_pem).verify(decoded, signed, padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError, binascii.Error, InvalidSignature) as exc:
        raise PaymentVerificationError("支付通知验签失败") from exc


def _required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise PaymentVerificationError(f"{label}缺失")
    return value


def decrypt_wechat_notification(*, headers: Mapping[str, str], body: bytes, public_key_pem: bytes,
                                api_v3_key: bytes, expected_serial: str, now: int | None = None) -> dict:
    """Verify the signed envelope and decrypt its transaction without trusting its fields."""
    if not isinstance(body, bytes) or not body or len(body) > 1024 * 1024:
        raise PaymentVerificationError("微信支付通知正文无效")
    normalized = {str(k).lower(): v for k, v in headers.items()}
    stamp = _required(normalized.get("wechatpay-timestamp"), "微信通知时间")
    nonce = _required(normalized.get("wechatpay-nonce"), "微信通知随机值")
    serial = _required(normalized.get("wechatpay-serial"), "微信平台证书编号")
    signature = _required(normalized.get("wechatpay-signature"), "微信通知签名")
    _required(expected_serial, "预设证书编号")
    if serial != expected_serial:
        raise PaymentVerificationError("微信平台证书编号不匹配")
    try:
        numeric_time = int(stamp)
    except ValueError as exc:
        raise PaymentVerificationError("微信通知时间无效") from exc
    if abs((int(time.time()) if now is None else now) - numeric_time) > 300:
        raise PaymentVerificationError("微信通知时间超出允许范围")
    _verify(public_key_pem, signature, f"{stamp}\n{nonce}\n".encode() + body + b"\n")
    if not isinstance(api_v3_key, bytes) or len(api_v3_key) != 32:
        raise PaymentVerificationError("微信 API v3 密钥无效")
    try:
        envelope = json.loads(body)
        resource = envelope["resource"]
        if resource["algorithm"] != "AEAD_AES_256_GCM":
            raise PaymentVerificationError("微信支付加密算法不支持")
        ciphertext = base64.b64decode(resource["ciphertext"], validate=True)
        clear = AESGCM(api_v3_key).decrypt(resource["nonce"].encode(), ciphertext,
                                          resource.get("associated_data", "").encode())
        result = json.loads(clear)
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error, InvalidTag) as exc:
        raise PaymentVerificationError("微信支付通知解密失败") from exc
    if not isinstance(result, dict) or not isinstance(result.get("amount"), dict):
        raise PaymentVerificationError("微信支付结果结构无效")
    return result


def extract_wechat_order_no(*, headers: Mapping[str, str], body: bytes, public_key_pem: bytes,
                            api_v3_key: bytes, expected_serial: str, now: int | None = None) -> str:
    """Return an order number only after signature verification and resource decryption."""
    result = decrypt_wechat_notification(headers=headers, body=body, public_key_pem=public_key_pem,
                                         api_v3_key=api_v3_key, expected_serial=expected_serial, now=now)
    return _required(result.get("out_trade_no"), "微信商户订单号")


def verify_wechat(*, headers: Mapping[str, str], body: bytes, public_key_pem: bytes, api_v3_key: bytes,
                  expected_serial: str, expected_appid: str, expected_mchid: str,
                  expected_order_no: str, expected_amount_fen: int, now: int | None = None) -> dict:
    result = decrypt_wechat_notification(headers=headers, body=body, public_key_pem=public_key_pem,
                                         api_v3_key=api_v3_key, expected_serial=expected_serial, now=now)
    if not isinstance(result.get("amount"), dict):
        raise PaymentVerificationError("微信支付金额结构无效")
    if any(not isinstance(x, str) or not x for x in (expected_appid, expected_mchid, expected_order_no)) \
            or type(expected_amount_fen) is not int or expected_amount_fen <= 0:
        raise PaymentVerificationError("本地支付订单配置无效")
    amount = result["amount"]
    if (result.get("appid") != expected_appid or result.get("mchid") != expected_mchid
            or result.get("out_trade_no") != expected_order_no or result.get("trade_state") != "SUCCESS"
            or type(amount.get("total")) is not int or amount["total"] != expected_amount_fen
            or amount.get("currency") != "CNY"):
        raise PaymentVerificationError("微信支付订单信息不匹配")
    trade_id = _required(result.get("transaction_id"), "微信交易号")
    return {"order_no": expected_order_no, "trade_id": trade_id, "transaction_id": trade_id,
            "amount_fen": expected_amount_fen}


def verify_alipay(*, params: Mapping[str, str], public_key_pem: bytes, expected_appid: str,
                  expected_seller: str, expected_order_no: str, expected_amount_fen: int) -> dict:
    if not isinstance(params, Mapping) or not params or any(not isinstance(k, str) or not isinstance(v, str)
                                                           for k, v in params.items()):
        raise PaymentVerificationError("支付宝通知字段无效")
    if params.get("sign_type") != "RSA2":
        raise PaymentVerificationError("支付宝签名算法不支持")
    signature = _required(params.get("sign"), "支付宝签名")
    signed = "&".join(f"{key}={params[key]}" for key in sorted(params) if key not in {"sign", "sign_type"})
    _verify(public_key_pem, signature, signed.encode("utf-8"))
    if any(not isinstance(x, str) or not x for x in (expected_appid, expected_seller, expected_order_no)) \
            or type(expected_amount_fen) is not int or expected_amount_fen <= 0:
        raise PaymentVerificationError("本地支付订单配置无效")
    try:
        amount = Decimal(_required(params.get("total_amount"), "支付宝金额"))
        if not amount.is_finite() or amount * 100 != Decimal(expected_amount_fen):
            raise PaymentVerificationError("支付宝金额不匹配")
    except InvalidOperation as exc:
        raise PaymentVerificationError("支付宝金额无效") from exc
    if (params.get("app_id") != expected_appid or params.get("seller_id") != expected_seller
            or params.get("out_trade_no") != expected_order_no
            or params.get("trade_status") not in {"TRADE_SUCCESS", "TRADE_FINISHED"}):
        raise PaymentVerificationError("支付宝订单信息不匹配")
    trade_id = _required(params.get("trade_no"), "支付宝交易号")
    return {"order_no": expected_order_no, "trade_id": trade_id, "trade_no": trade_id,
            "amount_fen": expected_amount_fen}
