import base64
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services.billing_statement_gateway import (
    StatementGatewayError,
    StatementGatewayInputError,
    download_alipay_trade_statement,
    download_wechat_trade_statement,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _key_pair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, private_pem, public_pem


def _sign(private, message: bytes) -> str:
    return base64.b64encode(private.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode("ascii")


def _alipay_signed_response(private, result):
    return httpx.Response(200, json={
        "alipay_data_dataservice_bill_downloadurl_query_response": result,
        "sign": _sign(private, json.dumps(result, separators=(",", ":")).encode()),
    })


def test_alipay_download_verifies_signed_url_and_bounds_download():
    private, private_pem, public_pem = _key_pair()
    source = b"csv-bill"
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            params = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
            assert params["method"] == "alipay.data.dataservice.bill.downloadurl.query"
            assert json.loads(params["biz_content"]) == {"bill_type": "trade", "bill_date": "2026-11-05"}
            return _alipay_signed_response(private, {
                "code": "10000",
                "bill_download_url": "https://dwbillcenter.alipay.com/downloadBillFile.resource?token=opaque",
            })
        assert request.url.host == "dwbillcenter.alipay.com"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=httpx.ByteStream(source))

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        result = download_alipay_trade_statement(
            app_id="alipay-app", private_key_pem=private_pem, public_key_pem=public_pem,
            bill_date="2026-11-05", client=client, now=NOW,
        )

    assert result.raw == source
    assert result.source_origin == "alipay_api"
    assert result.provider_hash_type is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://dwbillcenter.alipay.com/downloadBillFile.resource?token=x",
        "https://evil.example/downloadBillFile.resource?token=x",
        "https://dwbillcenter.alipay.com/other?token=x",
        "https://user@dwbillcenter.alipay.com/downloadBillFile.resource?token=x",
    ],
)
def test_alipay_rejects_insecure_or_untrusted_download_urls_before_fetch(url):
    private, private_pem, public_pem = _key_pair()
    calls = []

    def handle(request):
        calls.append(request)
        return _alipay_signed_response(private, {"code": "10000", "bill_download_url": url})

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        with pytest.raises(StatementGatewayError):
            download_alipay_trade_statement(
                app_id="alipay-app", private_key_pem=private_pem, public_key_pem=public_pem,
                bill_date="2026-11-05", client=client, now=NOW,
            )
    assert len(calls) == 1


def _wechat_signed_response(platform_private, body: dict, timestamp: str):
    raw = json.dumps(body, separators=(",", ":")).encode()
    nonce = "platform-nonce"
    signature = _sign(platform_private, f"{timestamp}\n{nonce}\n".encode() + raw + b"\n")
    return httpx.Response(200, content=raw, headers={
        "Wechatpay-Timestamp": timestamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Serial": "platform-serial",
        "Wechatpay-Signature": signature,
        "Content-Type": "application/json",
    })


def _verify_authorization(request, private_public):
    header = request.headers["authorization"]
    timestamp = re.search(r'timestamp="([0-9]+)"', header).group(1)
    nonce = re.search(r'nonce_str="([^"]+)"', header).group(1)
    signature = re.search(r'signature="([^"]+)"', header).group(1)
    path = request.url.raw_path.decode()
    message = f"{request.method}\n{path}\n{timestamp}\n{nonce}\n\n".encode()
    private_public.verify(base64.b64decode(signature), message, padding.PKCS1v15(), hashes.SHA256())


def test_wechat_download_verifies_signed_api_response_download_signature_and_sha1():
    merchant_private, merchant_pem, merchant_public_pem = _key_pair()
    platform_private, _, platform_public_pem = _key_pair()
    bill_bytes = b"uncompressed statement bytes"
    source = gzip.compress(bill_bytes)
    digest = hashlib.sha1(bill_bytes, usedforsecurity=False).hexdigest()
    timestamp = str(int(NOW.timestamp()))
    calls = []

    def handle(request):
        calls.append(request)
        _verify_authorization(request, merchant_private.public_key())
        if request.url.path == "/v3/bill/tradebill":
            assert dict(request.url.params) == {
                "bill_date": "2026-11-05", "bill_type": "ALL", "tar_type": "GZIP",
            }
            return _wechat_signed_response(platform_private, {
                "download_url": "https://api.mch.weixin.qq.com/v3/billdownload/file?token=opaque",
                "hash_type": "SHA1",
                "hash_value": digest,
            }, timestamp)
        assert request.url.path == "/v3/billdownload/file"
        assert request.url.params["token"] == "opaque"
        return httpx.Response(200, stream=httpx.ByteStream(source))

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        result = download_wechat_trade_statement(
            mchid="mch-1", merchant_serial="merchant-serial", merchant_private_key_pem=merchant_pem,
            platform_public_key_pem=platform_public_pem, platform_serial="platform-serial",
            bill_date="2026-11-05", bill_type="ALL", client=client, now=NOW,
        )

    assert result.raw == source
    assert result.source_origin == "wechat_api"
    assert result.provider_hash_type == "SHA1"
    assert result.provider_hash_value == digest
    assert len(calls) == 2


def test_wechat_rejects_hash_mismatch_redirects_oversize_and_untrusted_host():
    merchant_private, merchant_pem, _ = _key_pair()
    platform_private, _, platform_public_pem = _key_pair()
    timestamp = str(int(NOW.timestamp()))
    source = gzip.compress(b"bill")
    mismatch = hashlib.sha1(source, usedforsecurity=False).hexdigest()
    expanded_bomb = gzip.compress(b"x" * (48 * 1024 * 1024 + 1))

    for download_url, download_status, content_length, body in (
        ("https://api.mch.weixin.qq.com/v3/billdownload/file?token=x", 200, None, source),
        ("https://evil.example/v3/billdownload/file?token=x", 200, None, source),
        ("https://api.mch.weixin.qq.com/v3/billdownload/file?token=x", 302, None, source),
        ("https://api.mch.weixin.qq.com/v3/billdownload/file?token=x", 200, str(12 * 1024 * 1024 + 1), b""),
        ("https://api.mch.weixin.qq.com/v3/billdownload/file?token=x", 200, None, b"not gzip"),
        ("https://api.mch.weixin.qq.com/v3/billdownload/file?token=x", 200, None, expanded_bomb),
    ):
        calls = []

        def handle(request):
            calls.append(request)
            _verify_authorization(request, merchant_private.public_key())
            if request.url.path == "/v3/bill/tradebill":
                return _wechat_signed_response(platform_private, {
                    "download_url": download_url,
                    "hash_type": "SHA1",
                    "hash_value": mismatch,
                }, timestamp)
            return httpx.Response(download_status, stream=httpx.ByteStream(body),
                                  headers={"Content-Length": content_length} if content_length else {})

        with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
            with pytest.raises(StatementGatewayError):
                download_wechat_trade_statement(
                    mchid="mch-1", merchant_serial="merchant-serial", merchant_private_key_pem=merchant_pem,
                    platform_public_key_pem=platform_public_pem, platform_serial="platform-serial",
                    bill_date="2026-11-05", bill_type="ALL", client=client, now=NOW,
                )
        if "evil.example" in download_url:
            assert len(calls) == 1


@pytest.mark.parametrize("bill_date", ["2026-11-06", "2026-11-07", "2026-08-05", "2026-2-01", "invalid"])
def test_wechat_rejects_current_future_old_or_non_iso_bill_dates(bill_date):
    _, merchant_pem, _ = _key_pair()
    _, _, platform_public_pem = _key_pair()
    with httpx.Client(transport=httpx.MockTransport(lambda _: None)) as client:
        with pytest.raises(StatementGatewayInputError):
            download_wechat_trade_statement(
                mchid="mch-1", merchant_serial="merchant-serial", merchant_private_key_pem=merchant_pem,
                platform_public_key_pem=platform_public_pem, platform_serial="platform-serial",
                bill_date=bill_date, bill_type="ALL", client=client, now=NOW,
            )
