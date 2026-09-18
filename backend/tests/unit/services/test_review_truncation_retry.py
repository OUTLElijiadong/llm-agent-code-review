"""审查画像输出截断兜底:_call_single_agent 提高预算重试一次的回归测试。"""

import pytest

from app.ai.deepseek_agent import DeepSeekOutputTruncatedError, _clamp_max_tokens
from app.ai.multi_agent import GENERAL_AGENT
from app.services import review_service


def test_clamp_max_tokens_ceiling_follows_settings():
    """钳制上限对齐 deepseek_max_output_tokens(生产 V4 系 65536),不再硬压 8192。"""
    assert _clamp_max_tokens(None) == 4096
    assert _clamp_max_tokens(16384) == 16384
    assert _clamp_max_tokens(10**9) == 65536
    assert _clamp_max_tokens(1) == 128


def test_profile_default_max_tokens_covers_reasoning_budget():
    """内置画像默认预算覆盖推理型模型的 reasoning+正文共享输出。"""
    assert GENERAL_AGENT.max_tokens == 16_384


def test_call_single_agent_retries_with_doubled_budget_on_truncation(monkeypatch):
    """finish_reason=length 截断时按翻倍预算重试一次,成功则不再上报覆盖不完整。"""
    calls: list[int] = []

    def fake_call_raw(self, system_prompt, user_prompt, agent_label="", json_mode=True,
                      temperature=None, max_tokens=None):
        calls.append(max_tokens)
        if len(calls) == 1:
            raise DeepSeekOutputTruncatedError(
                "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
            )
        return '{"issues": []}', {"model_tag": "deepseek-flash"}

    monkeypatch.setattr(
        "app.services.review_service.DeepSeekAgent.call_raw", fake_call_raw,
    )
    profile = GENERAL_AGENT.__class__(
        **{**GENERAL_AGENT.__dict__, "max_tokens": 4096},
    )
    text, _meta = review_service._call_single_agent(
        profile, "value = 1\n", "python", "a.py", [], 0,
    )
    assert text == '{"issues": []}'
    assert calls == [4096, 8192]


def test_call_single_agent_truncation_raises_when_already_at_ceiling(monkeypatch):
    """预算已到钳制上限仍截断时直接抛出,不做无效重试。"""
    def fake_call_raw(self, system_prompt, user_prompt, agent_label="", json_mode=True,
                      temperature=None, max_tokens=None):
        raise DeepSeekOutputTruncatedError(
            "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
        )

    monkeypatch.setattr(
        "app.services.review_service.DeepSeekAgent.call_raw", fake_call_raw,
    )
    profile = GENERAL_AGENT.__class__(
        **{**GENERAL_AGENT.__dict__, "max_tokens": 65_536},
    )
    with pytest.raises(DeepSeekOutputTruncatedError):
        review_service._call_single_agent(
            profile, "value = 1\n", "python", "a.py", [], 0,
        )
