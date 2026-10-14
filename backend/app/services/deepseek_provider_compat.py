"""官方 DeepSeek Responses 的请求级兼容，不修改模型配置或工具约束。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


def _history_missing_reasoning(input_items: Any) -> bool:
    """工具回放须有真实推理状态；旧历史/非思考纠错轮不伪造状态。"""
    if not isinstance(input_items, Sequence) or isinstance(input_items, (str, bytes, bytearray)):
        return False
    has_reasoning = False
    for item in input_items:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "message")
        if kind == "reasoning":
            parts = item.get("content")
            has_reasoning = isinstance(parts, list) and any(
                isinstance(part, Mapping) and part.get("type") == "reasoning_text"
                and isinstance(part.get("text"), str) and bool(part["text"])
                for part in parts
            )
        elif kind in {"function_call", "custom_tool_call"} or item.get("role") == "assistant":
            if not has_reasoning:
                return True
        elif kind in {"function_call_output", "custom_tool_call_output"} or item.get("role") in {
            "user", "developer", "system",
        }:
            has_reasoning = False
    return False


def prepare_deepseek_responses_payload(payload: Mapping[str, Any], upstream_url: str) -> dict[str, Any]:
    """自主轮次保留思考；受工具约束轮次只关闭该次思考，保留精确约束。

    官方 Responses reasoning.effort=none 关闭思考，required/指定工具在
    思考模式受限；实际 V4 服务也会拒绝显式 auto。省略 auto 与默认自主选择
    等价，不等于去掉 required/none/指定工具。未知供应商不套用此兼容策略。
    来源：https://api-docs.deepseek.com/api/create-response/
          https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/
          https://api-docs.deepseek.com/guides/thinking_mode/
    """
    outgoing = dict(payload)
    if urlsplit(upstream_url).hostname != "api.deepseek.com":
        return outgoing
    reasoning = payload.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, Mapping):
        return outgoing  # 不把格式不合法的调用方请求悄悄修成其他含义。
    choice = payload.get("tool_choice")
    reasoning_options = dict(reasoning or {})
    if payload.get("tools") and _history_missing_reasoning(payload.get("input")):
        # 原始历史保留不变；只为这次有缺失的回放使用非思考兼容模式。
        # 下一份完整/新会话请求仍遵循调用方思考设置，工具约束不放宽。
        reasoning_options["effort"] = "none"
        outgoing["reasoning"] = reasoning_options
    if choice == "auto":
        if reasoning_options.get("effort") != "none":
            outgoing.pop("tool_choice", None)
        return outgoing
    constrained = (isinstance(choice, str) and choice in {"none", "required"}) or (
        isinstance(choice, Mapping) and choice.get("type") == "function"
        and isinstance(choice.get("name"), str) and bool(choice["name"])
    )
    if constrained:
        outgoing["reasoning"] = {**reasoning_options, "effort": "none"}
    return outgoing
