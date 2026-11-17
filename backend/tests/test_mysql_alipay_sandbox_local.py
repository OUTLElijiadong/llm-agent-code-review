"""Loopback billing integration with disposable MySQL and a signed mock Alipay provider.

This exercises the real billing routes, not an actual Alipay sandbox account.
The shared fresh-chain fixture requires a separately supplied empty loopback MySQL.
"""

import base64
import json
import os
import re
import socket
import subprocess
import threading
import time
from uuid import uuid4

import httpx
import pytest
import test_fresh_database_chain as chain
import uvicorn
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import Depends, FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api.v1 import billing
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.billing_ledger import BillingCreditBatch, BillingPaymentEvent
from app.models.user import User
from app.services import billing_payment_gateway as gateway

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.fixture(scope="module")
def isolated_chain(request):
    """Prove this is our disposable tmpfs container before the fixture drops a database."""
    name = os.environ.get("PRISM_SANDBOX_MYSQL_CONTAINER", "")
    if not name and os.environ.get("PRISM_SANDBOX_MYSQL_REQUIRED") != "1":
        pytest.skip("需要专用一次性沙箱 MySQL 容器")
    if not re.fullmatch(r"prism-sandbox-integration-[0-9a-f]{12}", name):
        pytest.fail("需要指定专用一次性沙箱 MySQL 容器")
    fmt = ("{{json .HostConfig.Tmpfs}}|{{json .HostConfig.Binds}}|{{json .HostConfig.Mounts}}|"
           "{{json .NetworkSettings.Ports}}|{{json .Config.Labels}}")
    inspected = subprocess.run(["docker", "inspect", "--format", fmt, name], capture_output=True, text=True,
                               timeout=10, check=False)
    if inspected.returncode:
        pytest.fail("无法验证一次性沙箱 MySQL 容器")
    try:
        tmpfs, binds, mounts, ports, labels = (json.loads(item) for item in inspected.stdout.strip().split("|"))
        url = make_url(os.environ["PRISM_FRESH_CHAIN_MYSQL_URL"])
        mapping = ports["3306/tcp"]
        if (not tmpfs or "/var/lib/mysql" not in tmpfs or binds or mounts
                or labels.get("prism.alipay-sandbox") != "ephemeral" or url.host != "127.0.0.1" or not url.port
                or mapping != [{"HostIp": "127.0.0.1", "HostPort": str(url.port)}]):
            pytest.fail("MySQL 容器隔离校验失败")
    except (AttributeError, KeyError, TypeError, ValueError):
        pytest.fail("MySQL 容器隔离校验失败")
    return request.getfixturevalue("fresh_chain")


def test_local_billing_order_query_and_notification_replay(isolated_chain, monkeypatch, tmp_path):
    sessions = sessionmaker(bind=isolated_chain, autoflush=False, expire_on_commit=False)
    with sessions.begin() as db:
        user = User(username=f"sand-{uuid4().hex[:20]}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        user_id = user.id

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path, public_path = tmp_path / "app.pem", tmp_path / "alipay.pem"
    private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    public_path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo))
    for name, value in {
        "alipay_gateway_mode": "sandbox", "alipay_app_id": "sandbox-app", "alipay_seller_id": "sandbox-seller",
        "alipay_private_key_path": str(private_path), "alipay_public_key_path": str(public_path),
        "alipay_notify_url": "https://sandbox.invalid/api/billing/payments/alipay/notify",
        "billing_sales_enabled": True, "billing_commercial_metering_ready": True,
        "billing_entitlement_enforcement_ready": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(billing, "BILLING_SALES_CODE_GATE_OPEN", True)
    monkeypatch.setattr(billing.billing_ledger_service, "active_pricing_metadata",
                        lambda _db: {"model_pricing_ready": True})

    calls = []

    def fake_post(client, url, **kwargs):
        assert client is None
        assert url == gateway.ALIPAY_SANDBOX_GATEWAY
        params = kwargs["data"]
        method = params["method"]
        content = json.loads(params["biz_content"])
        order_no = content["out_trade_no"]
        calls.append((method, order_no))
        if method == "alipay.trade.precreate":
            response = {"code": "10000", "qr_code": "https://qr.alipay.com/sandbox-test"}
        elif method == "alipay.trade.query":
            response = {"code": "10000", "out_trade_no": order_no, "trade_status": "TRADE_SUCCESS",
                        "trade_no": f"trade-{order_no}", "total_amount": "4.00"}
        else:
            pytest.fail("unexpected provider method")
        signed = json.dumps(response, separators=(",", ":")).encode()
        signature = base64.b64encode(key.sign(signed, padding.PKCS1v15(), hashes.SHA256())).decode()
        return httpx.Response(200, json={method.replace(".", "_") + "_response": response, "sign": signature})

    monkeypatch.setattr(gateway, "_post", fake_post)
    app = FastAPI()
    app.add_api_route("/api/billing/orders", billing.create_payment_order, methods=["POST"])
    app.add_api_route("/api/billing/orders/{order_no}/refresh", billing.refresh_order_status, methods=["POST"])
    app.add_api_route("/api/billing/payments/alipay/notify", billing.alipay_payment_notification, methods=["POST"])

    def test_db():
        with sessions() as db:
            yield db

    def test_user(db=Depends(get_db)):
        return db.get(User, user_id)

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = test_user
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
            def create(request_id):
                result = client.post("/api/billing/orders", json={
                    "product": "personal_credits", "quantity": 1, "channel": "alipay",
                    "client_request_id": request_id,
                })
                assert result.status_code == 200, result.text
                return result.json()["data"]["order_no"]

            first = create("query-test")
            assert create("query-test") == first
            result = client.post(f"/api/billing/orders/{first}/refresh")
            assert result.status_code == 200 and result.json()["data"]["status"] == "paid"
            second = create("notify-test")
            params = {"app_id": "sandbox-app", "seller_id": "sandbox-seller", "out_trade_no": second,
                      "trade_no": f"trade-{second}", "trade_status": "TRADE_SUCCESS", "total_amount": "4.00",
                      "sign_type": "RSA2"}
            signed = "&".join(f"{name}={params[name]}" for name in sorted(params) if name != "sign_type")
            params["sign"] = base64.b64encode(key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
            assert client.post("/api/billing/payments/alipay/notify", data=params).text == "success"
            assert client.post("/api/billing/payments/alipay/notify", data=params).text == "success"
        assert calls.count(("alipay.trade.precreate", first)) == 1
        assert calls.count(("alipay.trade.query", first)) == 1
        with sessions() as db:
            assert db.query(BillingPaymentEvent).filter(BillingPaymentEvent.order_no.in_([first, second])).count() == 2
            batches = db.query(BillingCreditBatch).filter(BillingCreditBatch.grant_key.in_(
                [f"order:{first}", f"order:{second}"])).all()
            assert len(batches) == 2
            assert all(batch.issued_milli == batch.remaining_milli == 1_000_000 for batch in batches)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
