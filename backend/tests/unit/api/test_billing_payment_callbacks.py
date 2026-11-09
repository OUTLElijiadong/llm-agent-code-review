import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.billing_ledger import BillingCreditBatch, BillingPaymentEvent
from app.models.user import User
from app.services import billing_checkout_service as checkout
from app.services.billing_ledger_service import available_milli, get_or_create_wallet


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    from app.services.billing_rate_catalog_service import seed_test_baseline

    seed_test_baseline(session)
    session.commit()
    return engine, session


def _keys(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    path = tmp_path / "provider-public.pem"
    path.write_bytes(public)
    path.chmod(0o600)
    return key, public, path


def _sign(key, raw):
    return base64.b64encode(key.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()


def _client(db):
    previous = dict(app.dependency_overrides)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), previous


def test_alipay_callback_requires_verified_notification_and_is_idempotent(tmp_path, monkeypatch):
    engine, db = _database()
    key, _, public_path = _keys(tmp_path)
    monkeypatch.setattr(settings, "alipay_app_id", "app")
    monkeypatch.setattr(settings, "alipay_seller_id", "seller")
    monkeypatch.setattr(settings, "alipay_public_key_path", str(public_path))
    buyer = User(username="alipay-callback", password="x", role="user", status=1)
    db.add(buyer)
    db.flush()
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=2, channel="alipay")
    db.commit()
    params = {"app_id": "app", "seller_id": "seller", "out_trade_no": order.order_no, "trade_no": "ali-1",
              "trade_status": "TRADE_SUCCESS", "total_amount": "8.00", "sign_type": "RSA2"}
    signed = "&".join(f"{name}={params[name]}" for name in sorted(params) if name not in {"sign", "sign_type"})
    params["sign"] = _sign(key, signed.encode())
    client, previous = _client(db)
    try:
        invalid = client.post("/api/billing/payments/alipay/notify", data={**params, "total_amount": "0.01"})
        assert invalid.status_code == 400 and invalid.text == "failure"
        assert db.query(BillingPaymentEvent).count() == 0
        success = client.post("/api/billing/payments/alipay/notify", data=params)
        replay = client.post("/api/billing/payments/alipay/notify", data=params)
        assert success.status_code == replay.status_code == 200
        assert success.text == replay.text == "success"
        assert db.query(BillingPaymentEvent).count() == db.query(BillingCreditBatch).count() == 1
        assert available_milli(db, get_or_create_wallet(db, "user", buyer.id).id) == 2_000_000
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        db.close()
        engine.dispose()


def test_wechat_callback_verifies_signature_decrypts_and_records_paid_order(tmp_path, monkeypatch):
    engine, db = _database()
    key, _, public_path = _keys(tmp_path)
    aes_key = b"a" * 32
    monkeypatch.setattr(settings, "wechat_pay_app_id", "app")
    monkeypatch.setattr(settings, "wechat_pay_mchid", "merchant")
    monkeypatch.setattr(settings, "wechat_pay_api_v3_key", aes_key.decode())
    monkeypatch.setattr(settings, "wechat_pay_public_key_path", str(public_path))
    monkeypatch.setattr(settings, "wechat_pay_public_key_serial", "serial")
    buyer = User(username="wechat-callback", password="x", role="user", status=1)
    db.add(buyer)
    db.flush()
    order = checkout.create_order(db, user_id=buyer.id, product="personal_credits", quantity=1, channel="wechat")
    db.commit()
    transaction = {"appid": "app", "mchid": "merchant", "out_trade_no": order.order_no,
                   "transaction_id": "wx-1", "trade_state": "SUCCESS",
                   "amount": {"total": 400, "currency": "CNY"}}
    nonce = "123456789012"
    cipher = AESGCM(aes_key).encrypt(nonce.encode(), json.dumps(transaction).encode(), b"transaction")
    body = json.dumps({"resource": {"algorithm": "AEAD_AES_256_GCM", "nonce": nonce,
                                     "associated_data": "transaction",
                                     "ciphertext": base64.b64encode(cipher).decode()}},
                      separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {"Wechatpay-Timestamp": timestamp, "Wechatpay-Nonce": "notify-nonce",
               "Wechatpay-Serial": "serial",
               "Wechatpay-Signature": _sign(key, timestamp.encode() + b"\nnotify-nonce\n" + body + b"\n")}
    client, previous = _client(db)
    try:
        failed = client.post("/api/billing/payments/wechat/notify", content=body + b" ", headers=headers)
        assert failed.status_code == 400
        assert db.query(BillingPaymentEvent).count() == 0
        response = client.post("/api/billing/payments/wechat/notify", content=body, headers=headers)
        replay = client.post("/api/billing/payments/wechat/notify", content=body, headers=headers)
        assert response.status_code == replay.status_code == 200
        assert response.json() == {"code": "SUCCESS", "message": "成功"}
        assert replay.json() == response.json()
        assert db.query(BillingPaymentEvent).count() == db.query(BillingCreditBatch).count() == 1
        assert available_milli(db, get_or_create_wallet(db, "user", buyer.id).id) == 1_000_000
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        db.close()
        engine.dispose()
