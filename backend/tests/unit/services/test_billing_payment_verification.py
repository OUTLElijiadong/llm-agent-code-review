import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.billing_payment_verification import PaymentVerificationError, verify_alipay, verify_wechat


@pytest.fixture
def signing_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, public


def _sign(key, message):
    return base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()


def test_wechat_verify_signature_and_aes_before_matching_order(signing_keys):
    key, public = signing_keys
    aes_key = b"a" * 32
    clear = {"appid": "app", "mchid": "merchant", "out_trade_no": "ord-1",
             "transaction_id": "wx-1", "trade_state": "SUCCESS", "amount": {"total": 4900, "currency": "CNY"}}
    cipher = AESGCM(aes_key).encrypt(b"123456789012", json.dumps(clear).encode(), b"transaction")
    body = json.dumps({"resource": {"algorithm": "AEAD_AES_256_GCM", "nonce": "123456789012",
                                    "associated_data": "transaction", "ciphertext": base64.b64encode(cipher).decode()}},
                      separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = _sign(key, timestamp.encode() + b"\nnonce\n" + body + b"\n")
    headers = {"Wechatpay-Timestamp": timestamp, "Wechatpay-Nonce": "nonce",
               "Wechatpay-Serial": "serial", "Wechatpay-Signature": signature}
    kwargs = dict(headers=headers, body=body, public_key_pem=public, api_v3_key=aes_key,
                  expected_serial="serial", expected_appid="app", expected_mchid="merchant",
                  expected_order_no="ord-1", expected_amount_fen=4900)
    assert verify_wechat(**kwargs)["transaction_id"] == "wx-1"
    for change in ({"body": body+b" "}, {"expected_amount_fen": 4901}, {"expected_mchid": "wrong"},
                   {"expected_serial": "other"}, {"api_v3_key": b"b"*32}):
        with pytest.raises(PaymentVerificationError):
            verify_wechat(**{**kwargs, **change})


def test_alipay_verify_signature_and_payment_amount(signing_keys):
    key, public = signing_keys
    params = {"app_id": "app", "seller_id": "seller", "out_trade_no": "ord-1", "trade_no": "ali-1",
              "trade_status": "TRADE_SUCCESS", "total_amount": "49.00", "sign_type": "RSA2"}
    raw = "&".join(f"{k}={params[k]}" for k in sorted(params) if k not in {"sign", "sign_type"})
    params["sign"] = _sign(key, raw.encode())
    kwargs = dict(params=params, public_key_pem=public, expected_appid="app", expected_seller="seller",
                  expected_order_no="ord-1", expected_amount_fen=4900)
    assert verify_alipay(**kwargs)["trade_no"] == "ali-1"
    for change in ({"expected_amount_fen": 1}, {"expected_seller": "other"},
                   {"params": {**params, "trade_status": "WAIT_BUYER_PAY"}},
                   {"params": {**params, "sign_type": "RSA"}}):
        with pytest.raises(PaymentVerificationError):
            verify_alipay(**{**kwargs, **change})
