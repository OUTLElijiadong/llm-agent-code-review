"""Bounded, signed downloads for provider transaction statements.

Provider URLs and source bytes stay in memory. Only normalized rows and verified
hash metadata are handed to the statement importer; this module never changes
payment or entitlement state.
"""

from __future__ import annotations

import calendar
import gzip
import hashlib
import hmac
import io
import re
import secrets
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from app.core.request_size_limit import STATEMENT_FILE_MAX_BYTES
from app.services.billing_payment_gateway import (
    PAYMENT_TIMEOUT_SECONDS,
    PaymentGatewayError,
    _alipay_call,
    _wechat_authorization,
    _wechat_request,
)
from app.services.billing_statement_service import MAX_STATEMENT_EXPANDED_BYTES

MAX_PROVIDER_DOWNLOAD_BYTES = STATEMENT_FILE_MAX_BYTES
_CHINA = timezone(timedelta(hours=8))
_ALIPAY_HOSTS = {"dwbillcenter.alipay.com"}
_WECHAT_HOSTS = {"api.mch.weixin.qq.com", "api2.mch.weixin.qq.com"}


class StatementGatewayError(ValueError):
    """A provider statement cannot be downloaded or safely verified."""


class StatementGatewayInputError(StatementGatewayError):
    """The requested statement date or type is invalid for the provider API."""


@dataclass(frozen=True)
class DownloadedProviderStatement:
    raw: bytes
    file_name: str
    source_origin: str
    provider_hash_type: str | None = None
    provider_hash_value: str | None = None


def _china_today(now: datetime | None) -> date:
    observed = now.astimezone(_CHINA) if now is not None else datetime.now(_CHINA)
    return observed.date()


def _validate_bill_date(value: str, *, now: datetime | None, recent_months: int | None = None) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StatementGatewayInputError("账单日期必须为 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise StatementGatewayInputError("账单日期必须为 YYYY-MM-DD")
    today = _china_today(now)
    if parsed >= today:
        raise StatementGatewayInputError("只能下载已结束日期的账单")
    if recent_months is not None:
        month_index = today.year * 12 + today.month - 1 - recent_months
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        earliest = date(year, month, day)
        if parsed < earliest:
            raise StatementGatewayInputError("微信支付 API 仅支持最近三个月的账单")
    return parsed


def _trusted_download_url(value: str, *, channel: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (AttributeError, TypeError, ValueError) as exc:
        raise StatementGatewayError("渠道账单下载地址格式无效") from exc
    allowed_hosts = _ALIPAY_HOSTS if channel == "alipay" else _WECHAT_HOSTS
    expected_path = "/downloadBillFile.resource" if channel == "alipay" else "/v3/billdownload/file"
    if parsed.scheme.lower() != "https":
        raise StatementGatewayError(
            "渠道返回的账单下载地址不是 HTTPS，已拒绝不加密传输；请手动下载并导入账单"
        )
    if (host not in allowed_hosts or parsed.path != expected_path or not parsed.query
            or parsed.username is not None or parsed.password is not None
            or port not in {None, 443} or parsed.fragment):
        raise StatementGatewayError("渠道账单下载地址不在允许的 HTTPS 主机和路径范围内")
    return parsed.geturl(), f"{parsed.path}?{parsed.query}"


def _download_bounded(
    *, url: str, headers: dict[str, str], client: httpx.Client | None,
) -> bytes:
    try:
        if client is None:
            with httpx.Client(timeout=PAYMENT_TIMEOUT_SECONDS, follow_redirects=False) as session:
                return _download_bounded(url=url, headers=headers, client=session)
        with client.stream("GET", url, headers=headers, timeout=PAYMENT_TIMEOUT_SECONDS,
                           follow_redirects=False) as response:
            if response.status_code != 200:
                raise StatementGatewayError("渠道账单下载请求未成功")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_PROVIDER_DOWNLOAD_BYTES:
                        raise StatementGatewayError("渠道账单文件超过 12 MiB 限制")
                except ValueError as exc:
                    raise StatementGatewayError("渠道账单文件长度响应无效") from exc
            chunks = bytearray()
            for chunk in response.iter_raw():
                if len(chunks) + len(chunk) > MAX_PROVIDER_DOWNLOAD_BYTES:
                    raise StatementGatewayError("渠道账单文件超过 12 MiB 限制")
                chunks.extend(chunk)
            if not chunks:
                raise StatementGatewayError("渠道账单文件为空")
            return bytes(chunks)
    except StatementGatewayError:
        raise
    except httpx.HTTPError as exc:
        raise StatementGatewayError("渠道账单下载连接失败；请稍后重试") from exc


def _sha1_gzip_content(raw: bytes) -> str:
    """Hash WeChat's original bill bytes after bounded GZIP decompression."""
    digest = hashlib.sha1(usedforsecurity=False)
    expanded = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
            while chunk := stream.read(64 * 1024):
                expanded += len(chunk)
                if expanded > MAX_STATEMENT_EXPANDED_BYTES:
                    raise StatementGatewayError("微信支付解压后的账单超过 48 MiB 限制")
                digest.update(chunk)
    except StatementGatewayError:
        raise
    except (OSError, EOFError, zlib.error) as exc:
        raise StatementGatewayError("微信支付返回的 GZIP 账单无效") from exc
    if expanded == 0:
        raise StatementGatewayError("微信支付解压后的账单为空")
    return digest.hexdigest()


def download_alipay_trade_statement(
    *, app_id: str, private_key_pem: bytes, public_key_pem: bytes, bill_date: str,
    client: httpx.Client | None = None, now: datetime | None = None,
) -> DownloadedProviderStatement:
    _validate_bill_date(bill_date, now=now)
    try:
        result = _alipay_call(
            method="alipay.data.dataservice.bill.downloadurl.query", app_id=app_id,
            private_key_pem=private_key_pem, public_key_pem=public_key_pem,
            biz_content={"bill_type": "trade", "bill_date": bill_date}, client=client, now=now,
        )
    except PaymentGatewayError as exc:
        raise StatementGatewayError("支付宝账单地址查询失败；请检查商户配置或账单日期") from exc
    if result.get("code") != "10000":
        raise StatementGatewayError("支付宝未返回可下载的 trade 账单")
    raw_url = result.get("bill_download_url")
    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        raise StatementGatewayError("支付宝账单地址响应无效")
    url, _ = _trusted_download_url(raw_url, channel="alipay")
    raw = _download_bounded(url=url, headers={"Accept-Encoding": "identity"}, client=client)
    return DownloadedProviderStatement(
        raw=raw, file_name=f"alipay-{bill_date}-trade.download", source_origin="alipay_api",
    )


def download_wechat_trade_statement(
    *, mchid: str, merchant_serial: str, merchant_private_key_pem: bytes,
    platform_public_key_pem: bytes, platform_serial: str, bill_date: str,
    bill_type: str, client: httpx.Client | None = None, now: datetime | None = None,
) -> DownloadedProviderStatement:
    parsed_date = _validate_bill_date(bill_date, now=now, recent_months=3)
    if bill_type not in {"ALL", "SUCCESS", "REFUND"}:
        raise StatementGatewayInputError("微信支付交易账单类型无效")
    query = urlencode({"bill_date": parsed_date.isoformat(), "bill_type": bill_type, "tar_type": "GZIP"})
    path = f"/v3/bill/tradebill?{query}"
    try:
        result = _wechat_request(
            method="GET", path=path, mchid=mchid, merchant_serial=merchant_serial,
            merchant_private_key_pem=merchant_private_key_pem,
            platform_public_key_pem=platform_public_key_pem, platform_serial=platform_serial,
            client=client, now=now,
        )
    except PaymentGatewayError as exc:
        raise StatementGatewayError("微信支付账单申请失败；请检查商户配置、日期和账单状态") from exc
    return _download_verified_wechat_statement(
        result=result, mchid=mchid, merchant_serial=merchant_serial,
        merchant_private_key_pem=merchant_private_key_pem, file_name=f"wechat-{bill_date}-{bill_type}.gz",
        compressed=True, client=client, now=now,
    )


def download_wechat_fund_statement(
    *, mchid: str, merchant_serial: str, merchant_private_key_pem: bytes,
    platform_public_key_pem: bytes, platform_serial: str, bill_date: str,
    account_type: str, client: httpx.Client | None = None, now: datetime | None = None,
) -> DownloadedProviderStatement:
    """Download an uncompressed account fund-flow file for later review.

    This function is not wired to a public endpoint and does not reconcile
    orders, refunds, or merchant balances.
    """
    parsed_date = _validate_bill_date(bill_date, now=now, recent_months=3)
    if not isinstance(account_type, str) or account_type not in {"BASIC", "OPERATION", "FEES"}:
        raise StatementGatewayInputError("微信支付资金账户类型无效")
    path = "/v3/bill/fundflowbill?" + urlencode({
        "bill_date": parsed_date.isoformat(), "account_type": account_type,
    })
    try:
        result = _wechat_request(
            method="GET", path=path, mchid=mchid, merchant_serial=merchant_serial,
            merchant_private_key_pem=merchant_private_key_pem,
            platform_public_key_pem=platform_public_key_pem, platform_serial=platform_serial,
            client=client, now=now,
        )
    except PaymentGatewayError as exc:
        raise StatementGatewayError("微信支付资金账单申请失败；请检查商户配置、日期和账单状态") from exc
    return _download_verified_wechat_statement(
        result=result, mchid=mchid, merchant_serial=merchant_serial,
        merchant_private_key_pem=merchant_private_key_pem, file_name=f"wechat-{bill_date}-fund-{account_type}.csv",
        compressed=False, client=client, now=now,
    )


def _download_verified_wechat_statement(
    *, result: dict, mchid: str, merchant_serial: str, merchant_private_key_pem: bytes,
    file_name: str, compressed: bool, client: httpx.Client | None, now: datetime | None,
) -> DownloadedProviderStatement:
    raw_url = result.get("download_url")
    hash_type = result.get("hash_type")
    hash_value = result.get("hash_value")
    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        raise StatementGatewayError("微信支付账单地址响应无效")
    if hash_type != "SHA1" or not isinstance(hash_value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", hash_value) is None:
        raise StatementGatewayError("微信支付账单摘要类型或格式不受支持")
    url, _ = _trusted_download_url(raw_url, channel="wechat")
    try:
        # Sign exactly the request target httpx will send: it percent-encodes
        # raw spaces/non-ASCII in the query before transmitting the request.
        request_url = httpx.URL(url)
        url, _ = _trusted_download_url(str(request_url), channel="wechat")
        signed_path = request_url.raw_path.decode("ascii")
    except (httpx.InvalidURL, UnicodeDecodeError) as exc:
        raise StatementGatewayError("微信支付账单下载地址格式无效") from exc
    observed_at = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    timestamp = str(int(observed_at.timestamp()))
    nonce = secrets.token_hex(16)
    headers = {
        "Authorization": _wechat_authorization(
            mchid=mchid, serial=merchant_serial, private_key_pem=merchant_private_key_pem,
            method="GET", path=signed_path, body="", timestamp=timestamp, nonce=nonce,
        ),
        "Accept": "application/octet-stream",
        "Accept-Encoding": "identity",
    }
    raw = _download_bounded(url=url, headers=headers, client=client)
    actual_hash = (_sha1_gzip_content(raw) if compressed else hashlib.sha1(raw, usedforsecurity=False).hexdigest())
    if not hmac.compare_digest(actual_hash, hash_value.lower()):
        raise StatementGatewayError("微信支付账单 SHA1 校验失败，已丢弃下载内容")
    return DownloadedProviderStatement(
        raw=raw, file_name=file_name, source_origin="wechat_api",
        provider_hash_type=hash_type, provider_hash_value=actual_hash,
    )
