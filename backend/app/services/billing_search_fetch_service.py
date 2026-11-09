"""Metered DeepSeek Search/Fetch calls for enterprise-scoped assistant tools."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.models.billing_ledger import BillingReservation, BillingReservationPart, BillingToolExecutionResult
from app.services import billing_ledger_service as ledger

_ENDPOINTS = {
    "search": "https://api.deepseek.com/v1/tools/search",
    "fetch": "https://api.deepseek.com/v1/tools/fetch",
}
_MAX_URL_LENGTH = 2_048
_MAX_RESULTS = 5
_MAX_OUTPUT_CHARS = 12_000
_MAX_RESPONSE_BYTES = 256 * 1024
_REQUEST_TIMEOUT = httpx.Timeout(connect=5, read=25, write=5, pool=5)
_REQUEST_TOTAL_TIMEOUT_SECONDS = 120
_INVALID_KEYS = {"", "sk-xxxxx"}


class SearchFetchValidationError(ValueError):
    """Invalid tool input or unavailable platform provider configuration."""


class SearchFetchOutcomeUnknown(RuntimeError):
    """The provider may have charged; the request remains frozen for reconciliation."""


class SearchFetchReplayBlocked(RuntimeError):
    """An earlier attempt exists but its sanitized result was not durably recorded."""

    def __init__(self, message: str, *, failure_kind: str):
        super().__init__(message)
        self.failure_kind = failure_kind


class SearchFetchAuthorizationFailure(RuntimeError):
    """The upstream result arrived after runtime authority was revoked or fenced."""


class _RetryFreshSnapshot(RuntimeError):
    """A committed row was invisible in an older repeatable-read snapshot."""


@dataclass(frozen=True)
class SearchFetchResult:
    tool_name: str
    status: str
    billable: bool
    payload: Mapping[str, Any]


def validate_arguments(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if tool_name not in _ENDPOINTS or not isinstance(arguments, Mapping):
        raise SearchFetchValidationError("Search/Fetch 工具参数无效")
    if tool_name == "search":
        if set(arguments) - {"query", "ori_question"} or "query" not in arguments:
            raise SearchFetchValidationError("Search 只接受 query 与可选 ori_question")
        query = arguments.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
            raise SearchFetchValidationError("Search query 必须为 1 到 128 个 Unicode 字符")
        normalized: dict[str, Any] = {"query": query.strip(), "count": _MAX_RESULTS, "need_content": False}
        original_question = arguments.get("ori_question")
        if original_question is not None:
            if (not isinstance(original_question, str)
                    or not 1 <= len(original_question.strip()) <= 128):
                raise SearchFetchValidationError("ori_question 必须为 1 到 128 个 Unicode 字符")
            normalized["ori_question"] = original_question.strip()
        return normalized

    if set(arguments) != {"url"}:
        raise SearchFetchValidationError("Fetch 只接受 url")
    url = arguments.get("url")
    if not isinstance(url, str) or not 1 <= len(url) <= _MAX_URL_LENGTH:
        raise SearchFetchValidationError("Fetch URL 长度必须为 1 到 2,048 个字符")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise SearchFetchValidationError("Fetch URL 含有控制字符")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise SearchFetchValidationError("Fetch URL 格式无效") from exc
    if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise SearchFetchValidationError("Fetch 只接受无账号密码的 HTTP/HTTPS URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise SearchFetchValidationError("Fetch 不接受非公网 IP 地址")
    return {"url": url}


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _public_result_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        return ""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return ""
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return ""
    return value


def _normalize_search_result(item: Mapping[str, Any]) -> dict[str, str]:
    return {
        "title": _clean_text(item.get("title"), 256),
        "url": _public_result_url(item.get("url")),
        "snippet": _clean_text(item.get("snippet"), 500),
        "summary": _clean_text(item.get("summary"), 1_500),
    }


def _bound_search_results(results: list[Any]) -> tuple[list[dict[str, str]], bool]:
    normalized = [_normalize_search_result(item) for item in results[:_MAX_RESULTS] if isinstance(item, Mapping)]
    output: list[dict[str, str]] = []
    truncated = False
    for item in normalized:
        current_size = len(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        remaining = _MAX_OUTPUT_CHARS - current_size - (1 if output else 0)
        if remaining <= 0:
            truncated = True
            break
        shaped = dict(item)
        # Count the serialized characters instead of only the field values so
        # braces, keys and separators stay inside the hard tool-output budget.
        size = len(json.dumps(shaped, ensure_ascii=False, separators=(",", ":")))
        if size > remaining:
            for field in ("summary", "snippet", "title", "url"):
                if size <= remaining:
                    break
                excess = size - remaining
                shaped[field] = shaped[field][:-excess] if excess < len(shaped[field]) else ""
                size = len(json.dumps(shaped, ensure_ascii=False, separators=(",", ":")))
            truncated = True
        if size <= remaining:
            output.append(shaped)
        else:
            truncated = True
            break
    if len(normalized) > len(output):
        truncated = True
    return output, truncated


async def _read_body_limited(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                raise SearchFetchOutcomeUnknown("供应商响应超过安全读取上限")
        except ValueError:
            raise SearchFetchOutcomeUnknown("供应商响应长度无法验证")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise SearchFetchOutcomeUnknown("供应商响应超过安全读取上限")
        body.extend(chunk)
    return bytes(body)


def _authorize(authorize: Callable[[Session], None], db: Session) -> None:
    if not callable(authorize):
        raise SearchFetchValidationError("Search/Fetch 必须提供企业权限复核")
    authorize(db)


def _persist_unknown(session_factory: sessionmaker, request_key: str) -> None:
    with session_factory.begin() as db:
        ledger.mark_unknown(db, request_key)


def _input_sha256(tool_name: str, payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": tool_name, "arguments": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_result(
    db: Session, request_key: str, tool_name: str, input_sha256: str,
) -> SearchFetchResult | None:
    # Missing-key locking reads take an InnoDB gap lock. Callers that observe
    # a terminal reservation with a missing result must retry in a new Session
    # to obtain a fresh snapshot, rather than locking a potentially empty gap.
    row = db.query(BillingToolExecutionResult).filter_by(request_key=request_key).one_or_none()
    if row is None:
        return None
    if row.tool_name != tool_name:
        raise SearchFetchOutcomeUnknown("幂等键已用于其他 Search/Fetch 工具，禁止重复发送")
    if row.input_sha256 != input_sha256:
        raise SearchFetchReplayBlocked(
            "相同 tool call_id 对应的参数与已保存结果不一致；已停止当前运行，请以新消息重新发起。",
            failure_kind="tool_result_missing",
        )
    payload = json.loads(row.result_json)
    if not isinstance(payload, Mapping):
        raise SearchFetchOutcomeUnknown("已保存的 Search/Fetch 结果格式无效，禁止重复发送")
    return SearchFetchResult(
        tool_name=tool_name,
        status=str(payload.get("status") or "unknown"),
        billable=bool(payload.get("billable")),
        payload={key: value for key, value in payload.items() if key not in {"tool", "status", "billable"}},
    )


def load_saved_result(
    db: Session, request_key: str, tool_name: str, arguments: Mapping[str, Any],
) -> SearchFetchResult | None:
    """Read a previously paired result after the caller rechecks tenant permissions."""
    normalized = validate_arguments(tool_name, arguments)
    return _existing_result(db, request_key, tool_name, _input_sha256(tool_name, normalized))


def _lock_tool_reservation(db: Session, request_key: str) -> BillingReservation:
    """Lock wallet → reservation after the runtime authorization lock."""
    row = db.query(BillingReservation).filter_by(request_key=request_key).one_or_none()
    if row is None or row.billable_kind not in {"search", "fetch"}:
        raise SearchFetchOutcomeUnknown("Search/Fetch 预占记录不存在，无法安全结算")
    parts = (db.query(BillingReservationPart).filter_by(reservation_id=row.id)
             .order_by(BillingReservationPart.id).all())
    ledger._lock_wallets(db, [int(part.wallet_id) for part in parts])
    return (db.query(BillingReservation).filter_by(request_key=request_key)
            .populate_existing().with_for_update().one())


def _finalize_result(session_factory: sessionmaker, request_key: str,
                     result: SearchFetchResult, input_sha256: str,
                     authorize: Callable[[Session], None]) -> SearchFetchResult:
    payload = {
        "tool": result.tool_name,
        "status": result.status,
        "billable": result.billable,
        **dict(result.payload),
    }
    try:
        with session_factory.begin() as db:
            # Both runtime attempts and stale recovery acquire runtime state
            # first. Keep authorization and settlement in one transaction so
            # a recovery cannot fence the worker between these operations.
            try:
                _authorize(authorize, db)
            except Exception as exc:  # noqa: BLE001 - preserve a frozen hold after provider success
                raise SearchFetchAuthorizationFailure from exc
            reservation = _lock_tool_reservation(db, request_key)
            if reservation.status != "reserved":
                raise SearchFetchOutcomeUnknown("Search/Fetch 预占已进入对账或终态，禁止自动结算")
            if result.billable:
                ledger.settle_tool(db, request_key=request_key, tool_successful_calls=1)
            else:
                ledger.release_tool_nonbillable(db, request_key)
            db.add(BillingToolExecutionResult(
                request_key=request_key,
                tool_name=result.tool_name,
                input_sha256=input_sha256,
                result_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                created_at=ledger._now(),
            ))
        return result
    except SearchFetchAuthorizationFailure as exc:
        raise _finish_unknown(
            session_factory, request_key,
            "外呼完成后权限或运行租约复核失败，账单转待核对",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a successful provider call without durable pairing is uncertain
        try:
            with session_factory.begin() as db:
                _authorize(authorize, db)
                saved = _existing_result(db, request_key, result.tool_name, input_sha256)
                if saved is not None:
                    return saved
        except Exception:  # noqa: BLE001 - retain the original uncertainty
            pass
        raise _finish_unknown(session_factory, request_key, "Search/Fetch 结果与账单未能原子保存") from exc


def _finish_unknown(session_factory: sessionmaker, request_key: str, reason: str) -> SearchFetchOutcomeUnknown:
    try:
        _persist_unknown(session_factory, request_key)
    except Exception:  # noqa: BLE001 - retain the frozen hold even if reconciliation status write fails
        return SearchFetchOutcomeUnknown(f"{reason}；账单状态需人工检查")
    return SearchFetchOutcomeUnknown(f"{reason}；预占已冻结，待人工对账")


async def execute_tool(
    *,
    session_factory: sessionmaker,
    request_key: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    user_id: int,
    enterprise_id: int,
    enterprise_wallet_id: int,
    group_id: int | None,
    provider_name: str,
    base_url: str,
    credential_source: str,
    api_key: str,
    authorize: Callable[[Session], None],
) -> SearchFetchResult:
    """Reserve, reauthorize, commit the one-way attempt marker, then make one HTTP call."""
    payload = validate_arguments(tool_name, arguments)
    input_sha256 = _input_sha256(tool_name, payload)
    if not isinstance(api_key, str) or api_key.strip() in _INVALID_KEYS:
        raise SearchFetchValidationError("DeepSeek 平台凭据未配置")
    provider_identity = ledger.resolve_billing_tool_provider_identity(
        provider_name=provider_name, base_url=base_url, credential_source=credential_source,
    )
    if not isinstance(request_key, str) or not request_key or len(request_key) > 160:
        raise SearchFetchValidationError("Search/Fetch 幂等键无效")

    replay_error: SearchFetchReplayBlocked | SearchFetchOutcomeUnknown | None = None
    for attempt in range(2):
        try:
            with session_factory.begin() as db:
                _authorize(authorize, db)
                # Only lock a reservation if the snapshot shows a real row.
                # Missing-key FOR UPDATE would lock an InnoDB index gap.
                candidate = db.query(BillingReservation.id).filter_by(request_key=request_key).one_or_none()
                existing = _lock_tool_reservation(db, request_key) if candidate else None
                if existing is not None:
                    if (existing.billable_kind != tool_name or existing.user_id != user_id
                            or existing.enterprise_id != enterprise_id
                            or existing.enterprise_wallet_id != enterprise_wallet_id
                            or existing.group_id != group_id):
                        raise SearchFetchOutcomeUnknown("幂等键已绑定其他工具或账户，禁止重放")
                    if existing.status in {"settled", "released"}:
                        cached = _existing_result(db, request_key, tool_name, input_sha256)
                        if cached is not None:
                            return cached
                        if attempt == 0:
                            raise _RetryFreshSnapshot
                        replay_error = SearchFetchReplayBlocked(
                            "上一次 Search/Fetch 已结束但未保存可恢复结果；禁止按同一调用再次发送。",
                            failure_kind="tool_result_missing",
                        )
                    elif existing.status == "reserved" and existing.provider_attempt_started_at is None:
                        ledger.release_not_sent(db, request_key)
                        replay_error = SearchFetchReplayBlocked(
                            "上一次 Search/Fetch 已确认未发送且结果未保存；请重新发起一条新请求。",
                            failure_kind="tool_result_missing",
                        )
                    elif existing.status == "reserved" and existing.provider_attempt_started_at is not None:
                        ledger.mark_unknown(db, request_key)
                        replay_error = SearchFetchOutcomeUnknown("上一次 Search/Fetch 结果未知，预占已转人工对账")
                    elif existing.status == "unknown":
                        replay_error = SearchFetchOutcomeUnknown("上一次 Search/Fetch 结果仍待人工对账")
                    else:
                        raise SearchFetchOutcomeUnknown("Search/Fetch 预占状态无法识别")
                else:
                    try:
                        ledger.reserve_tool(
                            db, request_key=request_key, tool_name=tool_name, user_id=user_id,
                            enterprise_id=enterprise_id, enterprise_wallet_id=enterprise_wallet_id,
                            group_id=group_id, provider_identity=provider_identity,
                        )
                    except ledger.IdempotencyConflict as exc:
                        raise _RetryFreshSnapshot from exc
            break
        except _RetryFreshSnapshot as exc:
            if attempt:
                raise SearchFetchReplayBlocked(
                    "Search/Fetch 调用键发生并发冲突，请以新消息重新发起。",
                    failure_kind="tool_result_missing",
                ) from exc
    if replay_error is not None:
        if isinstance(replay_error, SearchFetchOutcomeUnknown):
            raise _finish_unknown(session_factory, request_key, str(replay_error))
        raise replay_error

    try:
        with session_factory.begin() as db:
            _authorize(authorize, db)
            ledger.mark_tool_attempt_started(db, request_key)
    except BaseException:
        # Authorization failure before the committed send marker is provably unsent.
        try:
            with session_factory.begin() as db:
                ledger.release_not_sent(db, request_key)
        except Exception:  # noqa: BLE001 - ambiguous commit keeps the reservation closed to retries
            pass
        raise

    async def _request_body() -> tuple[int, bytes | None]:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST",
                _ENDPOINTS[tool_name],
                headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
                json=payload,
            ) as response:
                if response.status_code != 200:
                    return response.status_code, None
                return response.status_code, await _read_body_limited(response)

    try:
        status_code, response_body = await asyncio.wait_for(
            _request_body(), timeout=_REQUEST_TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise _finish_unknown(session_factory, request_key, "供应商请求超过总时限，结果未知") from exc
    except SearchFetchOutcomeUnknown as exc:
        raise _finish_unknown(session_factory, request_key, str(exc)) from exc
    except asyncio.CancelledError as exc:
        raise _finish_unknown(session_factory, request_key, "供应商请求已中断，结果未知") from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _finish_unknown(session_factory, request_key, "供应商请求超时或连接中断，结果未知") from exc
    except Exception as exc:  # noqa: BLE001 - any post-marker transport/runtime failure is non-retryable
        raise _finish_unknown(session_factory, request_key, "供应商请求结果未知") from exc

    if status_code != 200:
        return _finalize_result(session_factory, request_key, SearchFetchResult(
            tool_name=tool_name,
            status="provider_error",
            billable=False,
            payload={"provider_status": status_code, "message": "供应商未返回可计费结果"},
        ), input_sha256, authorize)
    try:
        response_json = json.loads(response_body or b"")
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise _finish_unknown(session_factory, request_key, "供应商返回了无法验证的成功响应") from exc

    if not isinstance(response_json, Mapping):
        raise _finish_unknown(session_factory, request_key, "供应商返回了无法验证的成功响应")

    if tool_name == "search":
        results = response_json.get("results")
        if not isinstance(results, list):
            raise _finish_unknown(session_factory, request_key, "供应商返回了无法验证的 Search 结果")
        if not results:
            return _finalize_result(
                session_factory,
                request_key,
                SearchFetchResult(tool_name="search", status="empty", billable=False, payload={"results": []}),
                input_sha256,
                authorize,
            )
        try:
            normalized, truncated = _bound_search_results(results)
        except Exception as exc:  # noqa: BLE001 - malformed output cannot erase a potentially billable result
            raise _finish_unknown(session_factory, request_key, "Search 成功结果无法安全整理") from exc
        return _finalize_result(
            session_factory,
            request_key,
            SearchFetchResult(
                tool_name="search",
                status="success" if normalized else "success_unrenderable",
                billable=True,
                payload={"results": normalized, "truncated": truncated},
            ),
            input_sha256,
            authorize,
        )

    content = response_json.get("content")
    if not isinstance(content, str):
        raise _finish_unknown(session_factory, request_key, "供应商返回了无法验证的 Fetch 正文")
    if not content.strip():
        return _finalize_result(
            session_factory,
            request_key,
            SearchFetchResult(tool_name="fetch", status="empty", billable=False, payload={"content": ""}),
            input_sha256,
            authorize,
        )
    title = _clean_text(response_json.get("title"), 500)
    limited_content = content[:_MAX_OUTPUT_CHARS]
    return _finalize_result(
        session_factory,
        request_key,
        SearchFetchResult(
            tool_name="fetch",
            status="success",
            billable=True,
            payload={"title": title, "content": limited_content, "truncated": len(content) > len(limited_content)},
        ),
        input_sha256,
        authorize,
    )
