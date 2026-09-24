"""全体 BaseAgent 模型调用的上下文容量门禁。"""

from app.agents.base import BaseAgent
from app.core.config import settings


def test_oversized_input_fails_without_sending_a_partial_prompt(monkeypatch) -> None:
    """代码/审计证据过长时不得只发头部并返回看似完整的结果。"""
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 8192)
    agent = BaseAgent(system_prompt="审查全部证据")
    events = []
    monkeypatch.setattr(agent, "_emit", lambda *args, **kwargs: events.append((args, kwargs)))

    def forbidden_network(*args, **kwargs):
        raise AssertionError("容量超限不允许发起模型请求")

    monkeypatch.setattr("app.agents.base.pin_public_http_url", forbidden_network)
    original = "A" * 8100 + "\n末尾关键审计证据"

    result = agent.call(original)

    assert result.success is False
    assert result.failure_kind == "input_exceeds_context"
    assert result.http_attempts == 0
    assert "需分片或压缩" in result.error
    projected, exceeded = agent._project_input(original)
    assert exceeded is True
    assert projected == original
    assert events


def test_input_capacity_reserves_the_actual_output_budget(monkeypatch) -> None:
    """输出预算较大时，输入门禁也应同步收紧。"""
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr(settings, "deepseek_max_output_tokens", 96_000)
    agent = BaseAgent(system_prompt="完整分析")
    monkeypatch.setattr(agent, "_emit", lambda *args, **kwargs: None)

    def forbidden_network(*args, **kwargs):
        raise AssertionError("不应联网")

    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        forbidden_network,
    )

    result = agent.call("证据" * 4_000, max_tokens=96_000)

    assert result.failure_kind == "input_exceeds_context"
    assert result.http_attempts == 0
