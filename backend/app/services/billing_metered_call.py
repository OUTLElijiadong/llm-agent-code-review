"""One-request pre-authorization adapter for explicitly integrated model clients.

This adapter does not by itself cover any existing model call path. Never enable
commercial sales until every upstream call, retry, stream and tool is integrated.
"""

from collections.abc import Callable, Mapping

import httpx

from app.services import billing_ledger_service as ledger
from app.services.ai_usage_context import usage_tokens


class MeteredRequestNotSent(ledger.BillingError):
    """The authorization fence rejected the call before invoking its provider."""


def provider_tool_usage_counts(usage):
    """读取可审计的供应商服务端工具成功计数；不推断缺失计数为零。"""
    if not isinstance(usage, Mapping) or "server_tool_use" not in usage:
        return None
    counters = usage["server_tool_use"]
    if (not isinstance(counters, Mapping) or set(counters) != {"tool_search", "tool_fetch"}
            or any(type(value) is not int or not 0 <= value <= 2**63 - 1
                   for value in counters.values())):
        raise ledger.BillingError("服务端工具用量未知或尚未计价，待人工对账")
    return counters["tool_search"], counters["tool_fetch"]


def verify_priced_provider_usage(usage):
    """服务端按次工具尚未定价；异常及未来字段均需人工对账。"""
    counts = provider_tool_usage_counts(usage)
    if counts is not None and any(counts):
        raise ledger.BillingError("服务端工具用量尚未计价，待人工对账")
    return counts


def metered_call(*, session_factory, request_key: str, model_name: str, user_id: int | None,
                 max_input_tokens: int, max_output_tokens: int, send: Callable,
                 provider_identity: ledger.BillingProviderIdentity | None = None,
                 enterprise_wallet_id=None, personal_wallet_id=None, enterprise_id=None,
                 group_id=None, allow_personal=False, usage_kind="member", attempt_guard=None,
                 send_guard=None, on_reserved=None, on_unknown=None, on_settled=None):
    """Commit a trusted upper-bound hold before `send`, then settle trusted usage.

    `send` must return `(result, usage_mapping)`. Optional `send_guard(send, request_key)`
    may serialize the final authorization fence with the provider request. On transport failure or missing
    usage the hold remains unknown for reconciliation; the request is not retried.
    Caller must authenticate and authorize context, supply a provable token bound,
    and provide the validated identity of the exact managed upstream endpoint.
    """
    provider_identity = ledger.validate_billing_provider_identity(provider_identity)
    if model_name not in ledger._BILLABLE_MODELS:
        raise ledger.BillingError("该模型的供应商路由或结算价格尚未确认，禁止平台计费")
    with session_factory.begin() as db:
        pricing_now = ledger._now()
        maximum = ledger.quote_catalog_milli(
            db, model_name, max_input_tokens, max_output_tokens, now=pricing_now,
        )
        if maximum <= 0:
            raise ledger.BillingError("模型调用必须有可验证的最大用量")
        if attempt_guard is not None:
            if not callable(attempt_guard):
                raise ledger.BillingError("模型调用授权校验无效")
            attempt_guard(db, request_key)
        ledger.reserve(db, request_key=request_key, model_name=model_name, user_id=user_id,
                       max_milli=maximum, enterprise_wallet_id=enterprise_wallet_id,
                       personal_wallet_id=personal_wallet_id, enterprise_id=enterprise_id,
                       group_id=group_id, allow_personal=allow_personal, usage_kind=usage_kind,
                       require_new=True, provider_identity=provider_identity, now=pricing_now)
        if on_reserved is not None:
            on_reserved(db, request_key)
    tool_counts = None
    try:
        if send_guard is not None:
            if not callable(send_guard):
                raise ledger.BillingError("模型请求最终授权闸无效")
            result, usage = send_guard(send, request_key)
        else:
            result, usage = send()
        tool_counts = provider_tool_usage_counts(usage)
        verify_priced_provider_usage(usage)
        input_tokens = usage_tokens(usage, "prompt_tokens", "input_tokens")
        output_tokens = usage_tokens(usage, "completion_tokens", "output_tokens")
        if input_tokens is None or output_tokens is None:
            raise ledger.BillingError("模型返回用量缺失，待人工对账")
        if input_tokens > max_input_tokens or output_tokens > max_output_tokens:
            raise ledger.BillingError("模型实际用量超过预占上限，待人工对账")
    except MeteredRequestNotSent:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        with session_factory.begin() as db:
            ledger.mark_unknown(db, request_key)
            if on_unknown is not None:
                on_unknown(db, request_key)
        raise ledger.BillingError("模型调用结果未知，预占待对账，禁止自动重试") from exc
    except BaseException:
        with session_factory.begin() as db:
            ledger.mark_unknown(
                db, request_key,
                server_tool_search_calls=tool_counts[0] if tool_counts is not None else None,
                server_tool_fetch_calls=tool_counts[1] if tool_counts is not None else None,
            )
            if on_unknown is not None:
                on_unknown(db, request_key)
        raise
    with session_factory.begin() as db:
        actual = ledger.quote_reservation_milli(
            db, request_key=request_key, input_tokens=input_tokens, output_tokens=output_tokens,
        )
        ledger.settle(db, request_key=request_key, actual_milli=actual,
                      input_tokens=input_tokens, output_tokens=output_tokens,
                      server_tool_search_calls=tool_counts[0] if tool_counts is not None else None,
                      server_tool_fetch_calls=tool_counts[1] if tool_counts is not None else None)
        if on_settled is not None:
            on_settled(db, request_key)
    return result
