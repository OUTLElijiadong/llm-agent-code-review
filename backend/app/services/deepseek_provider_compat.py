"""官方 DeepSeek Responses 的请求级兼容，不修改模型配置或工具约束。"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit


def prepare_deepseek_responses_payload(payload: Mapping[str, Any], upstream_url: str) -> dict[str, Any]:
    """自主轮次保留思考；受工具约束轮次只关闭该次思考，保留精确约束。

    官方 Responses reasoning.effort=none 关闭思考，required/指定工具在
    思考模式受限；实际 V4 服务也会拒绝显式 auto。省略 auto 与默认自主选择
    等价，不等于去掉 required/none/指定工具。未知供应商不套用此兼容策略。
    来源：https://api-docs.deepseek.com/api/create-response/
          https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/
    """
    outgoing = dict(payload)
    if urlsplit(upstream_url).hostname != "api.deepseek.com":
        return outgoing
    reasoning = payload.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, Mapping):
        return outgoing  # 不把格式不合法的调用方请求悄悄修成其他含义。
    choice = payload.get("tool_choice")
    reasoning_options = dict(reasoning or {})
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
