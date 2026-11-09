"""Trusted billing context for model requests and their external-cost gates.

An absent scope is not a free-call authorization: commercial readiness rejects
it, while legacy mode requires caller-owned credentials or an explicit
deployment opt-in for unmetered platform calls. Paid scopes only accept paths
whose request bounds, provider identity, and prices are approved.
"""

import json
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from uuid import uuid4

from app.core.config import settings
from app.services import billing_ledger_service as ledger
from app.services.ai_usage_context import usage_tokens
from app.services.billing_metered_call import (
    metered_call,
    provider_tool_usage_counts,
    verify_priced_provider_usage,
)
from app.services.multimodal_service import MAX_IMAGE_BYTES
from app.utils.image_utils import decode_data_image_url

_scope = ContextVar("billing_runtime_scope", default=None)

# UTF-8 bytes upper-bound byte-level tokenizers for text payloads. The margins
# cover provider message framing and per-item separators; they are added to
# each request-derived size and are not a substitute for the scope limit.
_PROTOCOL_OVERHEAD_TOKENS = 32
_ITEM_OVERHEAD_TOKENS = 8
_IMAGE_TOKEN_UPPER_BOUND = 1024
_MAX_BILLABLE_IMAGES_PER_REQUEST = 600
# Keep inline base64 below DeepSeek's 48 MiB request limit after expansion,
# while leaving room for the bounded text and JSON framing.
_MAX_BILLABLE_IMAGE_BYTES = 28 * 1024 * 1024
_BILLABLE_VISION_MODELS = frozenset({
    "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
})
_IMAGE_BOUND_SENTINEL = "[image reserved at 1024 input tokens]"
_REQUEST_FIELDS = frozenset({
    "model", "instructions", "input", "messages", "tools", "max_output_tokens", "max_tokens",
    "temperature", "top_p", "stream", "store", "tool_choice", "parallel_tool_calls",
    "response_format", "thinking", "reasoning", "text", "metadata", "include", "truncation",
    "service_tier", "background", "prompt_cache_key", "user", "frequency_penalty",
    "presence_penalty", "stop", "logprobs", "top_logprobs", "n",
})
_STATEFUL_REQUEST_FIELDS = frozenset({
    "previous_response_id", "response_id", "conversation", "conversation_id", "thread_id",
})
_CHAT_MESSAGE_FIELDS = frozenset({"role", "content", "name", "tool_call_id", "tool_calls"})
_RESPONSES_MESSAGE_FIELDS = frozenset({"type", "role", "content", "id", "status"})
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text", "refusal", "summary_text", "reasoning_text"})


def _current_scope():
    scope = _scope.get()
    if scope is None and settings.billing_commercial_metering_ready:
        raise ledger.BillingError("商业计量已启用，但模型请求未进入可信计费作用域")
    return scope


def _require_unmetered_provider_opt_in(credential_source):
    """Allow caller-owned keys; platform/unknown provider costs need explicit legacy opt-in."""
    if credential_source == "user" or settings.billing_allow_unmetered_external_calls:
        return
    raise ledger.BillingError("平台模型调用未进入可信计费作用域，未计量兼容调用默认关闭")


@contextmanager
def billing_scope(*, session_factory, user_id, max_input_tokens, max_output_tokens,
                  enterprise_wallet_id=None, personal_wallet_id=None, enterprise_id=None,
                  group_id=None, allow_personal=False, usage_kind="member", request_key_prefix=None,
                  attempt_guard=None, send_guard=None, on_reserved=None, on_unknown=None, on_settled=None,
                  allow_streaming=True, before_stream_send=None):
    if (not callable(session_factory) or type(max_input_tokens) is not int or max_input_tokens <= 0
            or type(max_output_tokens) is not int or max_output_tokens <= 0
            or any(callback is not None and not callable(callback) for callback in
                   (attempt_guard, send_guard, on_reserved, on_unknown, on_settled, before_stream_send))):
        raise ledger.BillingError("计费调用缺少可信用量上界")
    data = {"session_factory": session_factory, "user_id": user_id, "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens, "enterprise_wallet_id": enterprise_wallet_id,
            "personal_wallet_id": personal_wallet_id, "enterprise_id": enterprise_id, "group_id": group_id,
            "allow_personal": allow_personal, "usage_kind": usage_kind,
            "request_key_prefix": request_key_prefix, "request_counter": 0,
            "request_lock": Lock(), "attempt_guard": attempt_guard}
    data.update(send_guard=send_guard, on_reserved=on_reserved, on_unknown=on_unknown,
                on_settled=on_settled, allow_streaming=bool(allow_streaming), before_stream_send=before_stream_send)
    token = _scope.set(data)
    try:
        yield
    finally:
        _scope.reset(token)


def verify_priced_tools(payload, *, allow_unmetered_server_tools=False):
    """Reject unpriced server tools in paid scopes and default-deny platform use."""
    scope = _current_scope()
    if scope is None:
        tools = payload.get("tools") if isinstance(payload, Mapping) else None
        if isinstance(tools, list) and any(
            not isinstance(tool, Mapping)
            or tool.get("type") not in {"function", "custom"}
            or (tool.get("type") == "custom" and tool.get("name") != "apply_patch")
            for tool in tools
        ) and not allow_unmetered_server_tools:
            reject_unpriced_call("服务端工具")
        return
    if not isinstance(payload, Mapping):
        raise ledger.BillingError("工具配置无效，禁止付费调用")
    if _STATEFUL_REQUEST_FIELDS.intersection(payload):
        raise ledger.BillingError("状态化模型请求无法建立单次输入上界，禁止付费调用")
    tools = payload.get("tools")
    if tools is None:
        return
    if not isinstance(tools, list):
        raise ledger.BillingError("工具配置无效，禁止付费调用")
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ledger.BillingError("工具配置无效，禁止付费调用")
        if tool.get("type") == "function":
            if set(tool) == {"type", "function"}:
                function = tool.get("function")
                if not isinstance(function, Mapping) or set(function) - {"name", "description", "parameters", "strict"}:
                    raise ledger.BillingError("函数工具字段未知，禁止付费调用")
                if not isinstance(function.get("name"), str) or not function["name"]:
                    raise ledger.BillingError("函数工具配置无效，禁止付费调用")
            elif set(tool) - {"type", "name", "description", "parameters", "strict"}:
                raise ledger.BillingError("函数工具字段未知，禁止付费调用")
            elif not isinstance(tool.get("name"), str) or not tool["name"]:
                raise ledger.BillingError("函数工具配置无效，禁止付费调用")
            continue
        if (tool.get("type") == "custom" and tool.get("name") == "apply_patch"
                and not set(tool) - {"type", "name", "description", "format"}):
            continue
        raise ledger.BillingError("服务端工具尚未计价，禁止付费调用")


def _text_content(value, *, field_name):
    if isinstance(value, str):
        lowered = value.lstrip().casefold()
        if lowered.startswith(("data:image/", "data:audio/", "data:video/", "data:application/", "blob:")):
            raise ledger.BillingError("多模态或外部载荷无法建立单次输入上界，禁止付费调用")
        return
    if not isinstance(value, list):
        raise ledger.BillingError(f"{field_name}必须是可计价的纯文本")
    for part in value:
        if not isinstance(part, Mapping) or set(part) != {"type", "text"}:
            raise ledger.BillingError("输入内容字段未知或包含非文本内容，禁止付费调用")
        if part.get("type") not in _TEXT_PART_TYPES or not isinstance(part.get("text"), str):
            raise ledger.BillingError("输入包含图片、音频、文件、视频或未知内容，禁止付费调用")
        _text_content(part["text"], field_name=field_name)


def _image_dimensions(binary):
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(binary)) as image:
        return image.size


def _validate_responses_message_content(value, *, role, model_name):
    if isinstance(value, str):
        _text_content(value, field_name="input.content")
        return value, 0, 0, []
    if not isinstance(value, list):
        raise ledger.BillingError("Responses 消息内容格式未知，禁止付费调用")

    normalized = []
    image_dimensions = []
    image_count = 0
    image_bytes = 0
    for part in value:
        if not isinstance(part, Mapping):
            raise ledger.BillingError("Responses 内容块格式未知，禁止付费调用")
        if part.get("type") in _TEXT_PART_TYPES:
            _text_content([part], field_name="input.content")
            normalized.append(dict(part))
            continue
        if part.get("type") != "input_image":
            raise ledger.BillingError("输入包含图片、音频、文件、视频或未知内容，禁止付费调用")
        if model_name not in _BILLABLE_VISION_MODELS:
            raise ledger.BillingError("图片输入仅支持已批准的 DeepSeek Flash 视觉模型")
        if role not in {"user", "developer"}:
            raise ledger.BillingError("DeepSeek Responses 图片仅允许出现在 user 或 developer 消息中")
        if set(part) not in ({"type", "image_url"}, {"type", "image_url", "detail"}):
            raise ledger.BillingError("图片字段未知或未提供内联 image_url，禁止付费调用")
        detail = part.get("detail", "auto")
        if not isinstance(detail, str) or detail not in {"auto", "low", "high", "original"}:
            raise ledger.BillingError("图片 detail 级别未知，禁止付费调用")
        image_url = part.get("image_url")
        decoded = decode_data_image_url(image_url, max_bytes=MAX_IMAGE_BYTES)
        if decoded is None:
            raise ledger.BillingError("图片必须是有效且不超过 1.5 MB 的内联 PNG/JPEG/WebP/GIF")
        mime, binary = decoded
        header = image_url.partition(",")[0]
        declared_mime = header[5:].split(";", 1)[0].casefold()
        if declared_mime != mime:
            raise ledger.BillingError("图片声明格式与实际文件格式不一致")
        image_count += 1
        image_bytes += len(binary)
        if image_count > _MAX_BILLABLE_IMAGES_PER_REQUEST:
            raise ledger.BillingError("单次计费请求图片数量超过供应商上限")
        if image_bytes > _MAX_BILLABLE_IMAGE_BYTES:
            raise ledger.BillingError("单次计费请求图片总量超过本地安全上限")
        image_dimensions.append(_image_dimensions(binary))
        normalized_part = {"type": "input_image", "image_url": _IMAGE_BOUND_SENTINEL}
        if "detail" in part:
            normalized_part["detail"] = detail
        normalized.append(normalized_part)

    return normalized, image_count, image_bytes, image_dimensions


def _validate_chat_messages(messages):
    if not isinstance(messages, list):
        raise ledger.BillingError("messages 格式未知，禁止付费调用")
    for message in messages:
        if not isinstance(message, Mapping) or set(message) - _CHAT_MESSAGE_FIELDS:
            raise ledger.BillingError("聊天消息字段未知，禁止付费调用")
        if message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ledger.BillingError("聊天消息角色未知，禁止付费调用")
        content = message.get("content")
        if content is not None:
            _text_content(content, field_name="message.content")
        elif message.get("role") != "assistant" or not message.get("tool_calls"):
            raise ledger.BillingError("聊天消息缺少可计价文本，禁止付费调用")
        if "name" in message and not isinstance(message["name"], str):
            raise ledger.BillingError("聊天消息 name 字段无效，禁止付费调用")
        if "tool_call_id" in message and not isinstance(message["tool_call_id"], str):
            raise ledger.BillingError("聊天消息 tool_call_id 字段无效，禁止付费调用")
        if "tool_calls" in message:
            calls = message["tool_calls"]
            if not isinstance(calls, list):
                raise ledger.BillingError("聊天工具调用格式未知，禁止付费调用")
            for call in calls:
                if (not isinstance(call, Mapping) or set(call) != {"id", "type", "function"}
                        or call.get("type") != "function" or not isinstance(call.get("id"), str)):
                    raise ledger.BillingError("聊天工具调用字段未知，禁止付费调用")
                function = call["function"]
                if (not isinstance(function, Mapping) or set(function) != {"name", "arguments"}
                        or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), str)):
                    raise ledger.BillingError("聊天函数调用字段未知，禁止付费调用")


def _validate_responses_input(input_value, *, model_name=None):
    if isinstance(input_value, str):
        _text_content(input_value, field_name="input")
        return input_value, 0
    if not isinstance(input_value, list):
        raise ledger.BillingError("Responses input 格式未知，禁止付费调用")
    normalized_input = []
    total_images = 0
    total_image_bytes = 0
    total_image_dimensions = []
    for item in input_value:
        if not isinstance(item, Mapping):
            raise ledger.BillingError("Responses input 项格式未知，禁止付费调用")
        item_type = item.get("type", "message")
        if item_type == "message":
            if set(item) - _RESPONSES_MESSAGE_FIELDS:
                raise ledger.BillingError("Responses 消息字段未知，禁止付费调用")
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant", "tool"}:
                raise ledger.BillingError("Responses 消息角色未知，禁止付费调用")
            content, image_count, image_bytes, image_dimensions = _validate_responses_message_content(
                item.get("content"), role=role, model_name=model_name,
            )
            total_images += image_count
            total_image_bytes += image_bytes
            total_image_dimensions.extend(image_dimensions)
            if total_images > _MAX_BILLABLE_IMAGES_PER_REQUEST:
                raise ledger.BillingError("单次计费请求图片数量超过供应商上限")
            if total_image_bytes > _MAX_BILLABLE_IMAGE_BYTES:
                raise ledger.BillingError("单次计费请求图片总量超过本地安全上限")
            normalized_item = dict(item)
            normalized_item["content"] = content
            normalized_input.append(normalized_item)
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            if set(item) - {"type", "call_id", "output", "id", "status"}:
                raise ledger.BillingError("Responses 工具输出字段未知，禁止付费调用")
            if not isinstance(item.get("call_id"), str) or not isinstance(item.get("output"), str):
                raise ledger.BillingError("Responses 工具输出格式未知，禁止付费调用")
            _text_content(item["output"], field_name="input.output")
            normalized_input.append(dict(item))
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            if set(item) - {"type", "call_id", "name", "arguments", "input", "id", "status"}:
                raise ledger.BillingError("Responses 工具调用字段未知，禁止付费调用")
            name = item.get("name")
            arguments = item.get("arguments", item.get("input"))
            if not isinstance(name, str) or not isinstance(arguments, str):
                raise ledger.BillingError("Responses 工具调用格式未知，禁止付费调用")
            _text_content(arguments, field_name="input.arguments")
            normalized_input.append(dict(item))
            continue
        if item_type == "reasoning":
            if set(item) - {"type", "id", "summary", "content", "status"}:
                raise ledger.BillingError("Responses 推理项字段未知，禁止付费调用")
            for key in ("summary", "content"):
                if key in item:
                    _text_content(item[key], field_name=f"input.{key}")
            normalized_input.append(dict(item))
            continue
        raise ledger.BillingError("Responses 输入包含图片、音频、文件、视频或未知项目，禁止付费调用")
    # DeepSeek applies the 4,096 px threshold to the entire request, including
    # images split across multiple Responses message items.
    max_dimension = 4096 if total_images >= 15 else 8192
    if any(max(width, height) > max_dimension for width, height in total_image_dimensions):
        raise ledger.BillingError("图片尺寸超过 DeepSeek 当前视觉接口上限")
    return normalized_input, total_images


def _request_input_limit(payload, max_input_tokens, *, model_name=None):
    if not isinstance(payload, Mapping):
        raise ledger.BillingError("模型请求格式未知，禁止付费调用")
    unknown_fields = set(payload) - _REQUEST_FIELDS
    if unknown_fields:
        raise ledger.BillingError("模型请求字段未知，禁止付费调用")
    if "messages" in payload and "input" in payload:
        raise ledger.BillingError("模型请求不能同时包含 messages 和 input")
    if "instructions" in payload and not isinstance(payload["instructions"], str):
        raise ledger.BillingError("instructions 格式未知，禁止付费调用")
    thinking = payload.get("thinking")
    if thinking is not None and (
        not isinstance(thinking, Mapping) or set(thinking) != {"type"}
        or not isinstance(thinking.get("type"), str) or thinking["type"] not in {"enabled", "disabled"}
    ):
        raise ledger.BillingError("thinking 配置未知，禁止付费调用")
    if "messages" in payload:
        _validate_chat_messages(payload["messages"])
    image_count = 0
    normalized_payload = dict(payload)
    if "input" in payload:
        normalized_payload["input"], image_count = _validate_responses_input(
            payload["input"], model_name=model_name,
        )

    try:
        serialized = json.dumps(normalized_payload, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ledger.BillingError("模型请求无法安全计量，禁止付费调用") from exc
    item_count = len(payload.get("messages") or payload.get("input") or [])
    if isinstance(payload.get("input"), str):
        item_count = 1
    tools = payload.get("tools")
    if isinstance(tools, list):
        item_count += len(tools)
    input_limit = (len(serialized.encode("utf-8")) + _PROTOCOL_OVERHEAD_TOKENS
                   + item_count * _ITEM_OVERHEAD_TOKENS + image_count * _IMAGE_TOKEN_UPPER_BOUND)
    if input_limit > max_input_tokens:
        raise ledger.BillingError("模型请求输入上界超过可信预算，禁止付费调用")
    return input_limit


def request_input_upper_bound(payload, max_input_tokens, *, model_name=None):
    """Validate the same serialized UTF-8 bound used by the billing hold path."""
    verify_priced_tools(payload)
    return _request_input_limit(payload, max_input_tokens, model_name=model_name)


def _next_request_key(scope):
    prefix = scope.get("request_key_prefix")
    if not prefix:
        return f"model:{uuid4().hex}"
    # A copied ContextVar context can still share this mutable scope dictionary
    # across worker threads; serialize its counter to keep reservation keys unique.
    with scope["request_lock"]:
        scope["request_counter"] += 1
        key = f"{prefix}:{scope['request_counter']}"
    if len(key) > 160:
        raise ledger.BillingError("计费请求幂等键过长")
    return key


def metered_response_request(*, model_name, payload, send, provider_name=None, base_url=None,
                             credential_source=None):
    scope = _current_scope()
    verify_priced_tools(payload, allow_unmetered_server_tools=credential_source == "user")
    if scope is None:
        _require_unmetered_provider_opt_in(credential_source)
        return send()
    if payload.get("model") not in (None, model_name):
        raise ledger.BillingError("计费模型与实际请求模型不一致")
    provider_identity = ledger.resolve_billing_provider_identity(
        provider_name=provider_name,
        base_url=base_url,
        credential_source=credential_source,
        model_name=model_name,
    )
    input_limit = _request_input_limit(payload, scope["max_input_tokens"], model_name=model_name)
    output_limit = payload.get("max_tokens") if isinstance(payload, dict) else None
    if type(output_limit) is not int or not 0 < output_limit <= scope["max_output_tokens"]:
        raise ledger.BillingError("模型请求输出上限未受可信预算约束")

    def tracked_send():
        response, duration = send()
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = {}
        usage = body.get("usage") if isinstance(body, dict) else None
        return (response, duration), usage

    call_scope = {
        key: scope[key] for key in (
            "session_factory", "user_id", "max_output_tokens", "enterprise_wallet_id",
            "personal_wallet_id", "enterprise_id", "group_id", "allow_personal", "usage_kind",
            "attempt_guard", "send_guard", "on_reserved", "on_unknown", "on_settled",
        )
        if key in scope
    }
    call_scope["max_input_tokens"] = input_limit
    call_scope["max_output_tokens"] = output_limit
    return metered_call(request_key=_next_request_key(scope), model_name=model_name,
                        provider_identity=provider_identity, send=tracked_send, **call_scope)


def begin_attempt(*, model_name, payload, output_key="max_output_tokens", provider_name=None,
                  base_url=None, credential_source=None, allow_unmetered_server_tools=False):
    """Commit a hold for a buffered or streaming async request."""
    scope = _current_scope()
    verify_priced_tools(
        payload,
        allow_unmetered_server_tools=allow_unmetered_server_tools or credential_source == "user",
    )
    if scope is None:
        _require_unmetered_provider_opt_in(credential_source)
        return None
    if not scope.get("allow_streaming", True):
        raise ledger.BillingError("当前团队执行器不支持流式上游请求，已在发出请求前拒绝")
    if payload.get("model") not in (None, model_name):
        raise ledger.BillingError("计费模型与实际请求模型不一致")
    provider_identity = ledger.resolve_billing_provider_identity(
        provider_name=provider_name,
        base_url=base_url,
        credential_source=credential_source,
        model_name=model_name,
    )
    input_limit = _request_input_limit(payload, scope["max_input_tokens"], model_name=model_name)
    output_limit = payload.get(output_key) if isinstance(payload, dict) else None
    if type(output_limit) is not int or not 0 < output_limit <= scope["max_output_tokens"]:
        raise ledger.BillingError("模型请求输出上限未受可信预算约束")
    key = _next_request_key(scope)
    with scope["session_factory"].begin() as db:
        pricing_now = ledger._now()
        maximum = ledger.quote_catalog_milli(db, model_name, input_limit, output_limit,
                                            now=pricing_now)
        guard = scope.get("attempt_guard")
        if guard is not None:
            guard(db, key)
        ledger.reserve(db, request_key=key, model_name=model_name, user_id=scope["user_id"],
                       max_milli=maximum, enterprise_wallet_id=scope["enterprise_wallet_id"],
                       personal_wallet_id=scope["personal_wallet_id"], enterprise_id=scope["enterprise_id"],
                       group_id=scope["group_id"], allow_personal=scope["allow_personal"],
                       usage_kind=scope["usage_kind"], require_new=True,
                       provider_identity=provider_identity, now=pricing_now)
        callback = scope.get("on_reserved")
        if callback is not None:
            callback(db, key)
    return {"key": key, "model_name": model_name, "scope": scope,
            "input_limit": input_limit, "output_limit": output_limit}


def before_stream_send(ticket):
    """Run an optional final authorization check after the hold and before transport I/O."""
    if ticket is not None:
        callback = ticket["scope"].get("before_stream_send")
        if callback is not None:
            callback(ticket["key"])


def validate_priced_usage(usage, *, input_limit, output_limit):
    """Return usage tokens only when the same settlement rules as finish_attempt pass."""
    verify_priced_provider_usage(usage)
    in_tokens = usage_tokens(usage, "input_tokens", "prompt_tokens")
    out_tokens = usage_tokens(usage, "output_tokens", "completion_tokens")
    if (in_tokens is None or out_tokens is None or in_tokens > input_limit or out_tokens > output_limit):
        raise ledger.BillingError("模型返回用量缺失或超出预占，待人工对账")
    return in_tokens, out_tokens


def validate_payload_usage(usage, *, payload, max_input_tokens, max_output_tokens,
                           output_key="max_output_tokens", model_name=None):
    """Validate usage against a request's actual preauthorization bounds."""
    verify_priced_tools(payload)
    input_limit = _request_input_limit(payload, max_input_tokens, model_name=model_name)
    output_limit = payload.get(output_key) if isinstance(payload, dict) else None
    if type(output_limit) is not int or not 0 < output_limit <= max_output_tokens:
        raise ledger.BillingError("模型请求输出上限未受可信预算约束")
    return validate_priced_usage(usage, input_limit=input_limit, output_limit=output_limit)


def unknown_attempt(ticket):
    if ticket is not None and not ticket.get("done"):
        counts = ticket.get("server_tool_counts")
        with ticket["scope"]["session_factory"].begin() as db:
            ledger.mark_unknown(
                db, ticket["key"],
                server_tool_search_calls=counts[0] if counts is not None else None,
                server_tool_fetch_calls=counts[1] if counts is not None else None,
            )
            callback = ticket["scope"].get("on_unknown")
            if callback is not None:
                callback(db, ticket["key"])


def reject_unpriced_call(resource: str, *, caller_pays=False):
    """Block external-cost resources without a ledger scope unless legacy opt-in is explicit."""
    if _current_scope() is not None:
        raise ledger.BillingError(f"{resource}尚未配置可信计费规则，禁止付费作用域调用")
    if caller_pays:
        return
    if not settings.billing_allow_unmetered_external_calls:
        raise ledger.BillingError(f"{resource}尚未配置可信计费规则，未计量兼容调用默认关闭")


def finish_attempt(ticket, usage):
    if ticket is None:
        return
    try:
        counts = provider_tool_usage_counts(usage)
        in_tokens, out_tokens = validate_priced_usage(
            usage, input_limit=ticket["input_limit"], output_limit=ticket["output_limit"],
        )
    except ledger.BillingError:
        try:
            ticket["server_tool_counts"] = provider_tool_usage_counts(usage)
        except ledger.BillingError:
            ticket["server_tool_counts"] = None
        unknown_attempt(ticket)
        raise
    with ticket["scope"]["session_factory"].begin() as db:
        actual = ledger.quote_reservation_milli(
            db, request_key=ticket["key"], input_tokens=in_tokens, output_tokens=out_tokens,
        )
        ledger.settle(db, request_key=ticket["key"], actual_milli=actual,
                      input_tokens=in_tokens, output_tokens=out_tokens,
                      server_tool_search_calls=counts[0] if counts is not None else None,
                      server_tool_fetch_calls=counts[1] if counts is not None else None)
        callback = ticket["scope"].get("on_settled")
        if callback is not None:
            callback(db, ticket["key"])
    ticket["done"] = True
