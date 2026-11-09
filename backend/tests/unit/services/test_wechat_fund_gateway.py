import base64
import hashlib
import json
import re
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.services.billing_statement_gateway import (
    StatementGatewayError,
    StatementGatewayInputError,
    download_wechat_fund_statement,
)

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (private, private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()),
            private.public_key().public_bytes(serialization.Encoding.PEM,
                                              serialization.PublicFormat.SubjectPublicKeyInfo))


def signed_response(private, payload):
    body = json.dumps(payload, separators=(',', ':')).encode()
    timestamp, nonce = str(int(NOW.timestamp())), 'platform-nonce'
    signature = base64.b64encode(private.sign(f'{timestamp}\n{nonce}\n'.encode() + body + b'\n',
                                              padding.PKCS1v15(), hashes.SHA256())).decode()
    return httpx.Response(200, content=body, headers={
        'Wechatpay-Timestamp': timestamp, 'Wechatpay-Nonce': nonce,
        'Wechatpay-Serial': 'platform-serial', 'Wechatpay-Signature': signature,
    })


def verify_request(request, public):
    header = request.headers['authorization']
    timestamp = re.search(r'timestamp="([0-9]+)"', header).group(1)
    nonce = re.search(r'nonce_str="([^"]+)"', header).group(1)
    signature = re.search(r'signature="([^"]+)"', header).group(1)
    message = f'{request.method}\n{request.url.raw_path.decode()}\n{timestamp}\n{nonce}\n\n'.encode()
    public.verify(base64.b64decode(signature), message, padding.PKCS1v15(), hashes.SHA256())


@pytest.mark.parametrize('account_type', ['BASIC', 'OPERATION', 'FEES'])
def test_fund_download_verifies_api_signature_signed_url_and_raw_sha1(account_type):
    merchant, merchant_pem, _ = keys()
    platform, _, platform_pem = keys()
    content = '资金账单\n'.encode()
    digest = hashlib.sha1(content, usedforsecurity=False).hexdigest()
    requests = []

    def handle(request):
        requests.append(request)
        verify_request(request, merchant.public_key())
        if request.url.path == '/v3/bill/fundflowbill':
            assert dict(request.url.params) == {'bill_date': '2026-11-05', 'account_type': account_type}
            return signed_response(platform, {
                'download_url': 'https://api.mch.weixin.qq.com/v3/billdownload/file?token=opaque',
                'hash_type': 'SHA1', 'hash_value': digest,
            })
        return httpx.Response(200, stream=httpx.ByteStream(content))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = download_wechat_fund_statement(
            mchid='mch', merchant_serial='merchant', merchant_private_key_pem=merchant_pem,
            platform_public_key_pem=platform_pem, platform_serial='platform-serial',
            bill_date='2026-11-05', account_type=account_type, client=client, now=NOW,
        )
    assert result.raw == content
    assert result.provider_hash_type == 'SHA1'
    assert result.provider_hash_value == digest
    assert len(requests) == 2


@pytest.mark.parametrize('mode', [
    'bad_signature', 'wrong_serial', 'stale_timestamp', 'hash_mismatch', 'bad_hash_type',
    'untrusted_host', 'redirect', 'too_large',
])
def test_fund_download_fails_closed_for_untrusted_responses(mode):
    merchant, merchant_pem, _ = keys()
    platform, _, platform_pem = keys()
    other, _, _ = keys()
    body = b'fund flow'
    digest = hashlib.sha1(body, usedforsecurity=False).hexdigest()
    calls = []

    def handle(request):
        calls.append(request)
        verify_request(request, merchant.public_key())
        if request.url.path == '/v3/bill/fundflowbill':
            response = signed_response(other if mode == 'bad_signature' else platform, {
                'download_url': 'https://evil.example/v3/billdownload/file?token=x' if mode == 'untrusted_host'
                else 'https://api.mch.weixin.qq.com/v3/billdownload/file?token=x',
                'hash_type': 'MD5' if mode == 'bad_hash_type' else 'SHA1',
                'hash_value': '0' * 40 if mode == 'hash_mismatch' else digest,
            })
            if mode == 'wrong_serial':
                response.headers['Wechatpay-Serial'] = 'unknown-key'
            if mode == 'stale_timestamp':
                response.headers['Wechatpay-Timestamp'] = str(int(NOW.timestamp()) - 301)
            return response
        if mode == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://evil.example/'})
        if mode == 'too_large':
            return httpx.Response(200, content=b'', headers={'content-length': str(12 * 1024 * 1024 + 1)})
        return httpx.Response(200, stream=httpx.ByteStream(body))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(StatementGatewayError):
            download_wechat_fund_statement(
                mchid='mch', merchant_serial='merchant', merchant_private_key_pem=merchant_pem,
                platform_public_key_pem=platform_pem, platform_serial='platform-serial',
                bill_date='2026-11-05', account_type='BASIC', client=client, now=NOW,
            )
    if mode in {'bad_signature', 'wrong_serial', 'stale_timestamp', 'untrusted_host', 'bad_hash_type'}:
        assert len(calls) == 1


@pytest.mark.parametrize(('bill_date', 'account_type'), [
    ('2026-11-06', 'BASIC'), ('2026-08-05', 'FEES'), ('2026-11-05', 'OTHER'),
])
def test_invalid_date_or_account_is_rejected_before_network(bill_date, account_type):
    _, merchant_pem, _ = keys()
    _, _, platform_pem = keys()
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('unexpected network'))) as client:
        with pytest.raises(StatementGatewayInputError):
            download_wechat_fund_statement(
                mchid='mch', merchant_serial='merchant', merchant_private_key_pem=merchant_pem,
                platform_public_key_pem=platform_pem, platform_serial='platform-serial',
                bill_date=bill_date, account_type=account_type, client=client, now=NOW,
            )


@pytest.mark.parametrize('query', ['token=a b', 'token=中文', 'token=a%2Fb%2Bc'])
def test_download_signs_the_normalized_query_that_httpx_sends(query):
    merchant, merchant_pem, _ = keys()
    platform, _, platform_pem = keys()
    body = b'fund flow'

    def handle(request):
        verify_request(request, merchant.public_key())
        if request.url.path == '/v3/bill/fundflowbill':
            return signed_response(platform, {
                'download_url': f'https://api.mch.weixin.qq.com/v3/billdownload/file?{query}',
                'hash_type': 'SHA1', 'hash_value': hashlib.sha1(body, usedforsecurity=False).hexdigest(),
            })
        return httpx.Response(200, stream=httpx.ByteStream(body))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = download_wechat_fund_statement(
            mchid='mch', merchant_serial='merchant', merchant_private_key_pem=merchant_pem,
            platform_public_key_pem=platform_pem, platform_serial='platform-serial',
            bill_date='2026-11-05', account_type='BASIC', client=client, now=NOW,
        )
    assert result.raw == body


@pytest.mark.parametrize('url', [
    'https://api.mch.weixin.qq.com/v3/billdownload/file',
    'https://api.mch.weixin.qq.com/v3/billdownload/file?',
    'https://api.mch.weixin.qq.com.evil.example/v3/billdownload/file?token=x',
    'https://api.mch.weixin.qq.com@evil.example/v3/billdownload/file?token=x',
    'https://api.mch.weixin.qq.com:444/v3/billdownload/file?token=x',
    'https://api.mch.weixin.qq.com/v3/billdownload/file?token=x#fragment',
    'http://api.mch.weixin.qq.com/v3/billdownload/file?token=x',
])
def test_url_normalization_does_not_expand_download_allowlist(url):
    merchant, merchant_pem, _ = keys()
    platform, _, platform_pem = keys()
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == '/v3/bill/fundflowbill'
        return signed_response(platform, {'download_url': url, 'hash_type': 'SHA1', 'hash_value': '0' * 40})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(StatementGatewayError):
            download_wechat_fund_statement(
                mchid='mch', merchant_serial='merchant', merchant_private_key_pem=merchant_pem,
                platform_public_key_pem=platform_pem, platform_serial='platform-serial',
                bill_date='2026-11-05', account_type='BASIC', client=client, now=NOW,
            )
    assert len(calls) == 1
