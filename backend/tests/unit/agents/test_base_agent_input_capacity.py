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


def test_chinese_json_payload_exceeding_token_budget_is_not_sent(monkeypatch) -> None:
    """大量中文 JSON 按字符一半估算会漏过容量门禁。"""
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    agent = BaseAgent(system_prompt="逐条审计 JSON，必须保留所有来源")
    monkeypatch.setattr(agent, "_emit", lambda *args, **kwargs: None)

    def forbidden_network(*args, **kwargs):
        raise AssertionError("中文 JSON 超出 token 预算，不应联网")

    monkeypatch.setattr("app.agents.base.pin_public_http_url", forbidden_network)
    original = '{"审计证据":"' + "中文漏洞证据" * 10_000 + '"}'

    result = agent.call(original)

    assert result.failure_kind == "input_exceeds_context"
    assert result.http_attempts == 0
    projected, exceeded = agent._project_input(original)
    assert projected == original
    assert exceeded is True


def test_long_chinese_system_prompt_counts_against_input_budget(monkeypatch) -> None:
    """系统指令也必须按 token 计，不能只检查用户消息。"""
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    agent = BaseAgent(system_prompt="规范" * 30_000)

    projected, exceeded = agent._project_input("请审查")

    assert projected == "请审查"
    assert exceeded is True


def test_dense_ascii_json_is_not_underestimated_by_average_ascii_ratio(monkeypatch) -> None:
    """JSON 标点密集时，ASCII/4 的平均估算也不能充当容量上界。"""
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    agent = BaseAgent(system_prompt="完整审查")
    original = '{"x":1},' * 12_000

    projected, exceeded = agent._project_input(original)

    assert projected == original
    assert exceeded is True


def test_small_mixed_language_prompt_remains_within_capacity(monkeypatch) -> None:
    """保守容量门禁不能把正常中英混合请求全部拒绝。"""
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 100_000)
    agent = BaseAgent(system_prompt="审查 JSON 证据")
    original = '{"title":"中文说明","detail":"review the source"}'

    projected, exceeded = agent._project_input(original)

    assert projected == original
    assert exceeded is False


def test_input_budget_reserves_actual_max_output_not_a_fixed_eight_thousand(monkeypatch) -> None:
    """较小 max_tokens 仍有安全输入空间；增大输出预算时同一原文须被拒绝。"""
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 12_000)
    agent = BaseAgent(system_prompt="审查全部证据")
    original = "证据" * 1_200

    assert agent._project_input(original, output_tokens=2_048) == (original, False)
    assert agent._project_input(original, output_tokens=8_192) == (original, True)
