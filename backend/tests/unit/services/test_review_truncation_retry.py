"""审查画像输出截断兜底:_call_single_agent 提高预算重试一次的回归测试。"""

import json
import re

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


def test_custom_profile_over_window_fails_when_compaction_has_no_source_coverage(monkeypatch):
    labels = []

    def fake_call_raw(*_args, **kwargs):
        labels.append(kwargs.get("agent_label"))
        return '{"issues": []}', {}

    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr("app.services.review_service.DeepSeekAgent.call_raw", fake_call_raw)
    profile = GENERAL_AGENT.__class__(
        **{**GENERAL_AGENT.__dict__, "is_custom": True, "system_prompt": "规则" * 60_000},
    )
    with pytest.raises(ValueError, match="来源覆盖不完整"):
        review_service._call_single_agent(profile, "tail = 1", "python", "a.py", [], 0)
    assert labels == ["review_context_compaction"]


def test_review_length_retry_compresses_extra_context_for_new_output_budget(monkeypatch):
    calls = []

    def fake_call_raw(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("agent_label") == "review_context_compaction":
            ids = re.findall(r'"source_id": "([^"]+)"', kwargs["user_prompt"])
            return json.dumps({"covered_source_ids": ids, "summary": "保持代理画像的审查重点。"},
                              ensure_ascii=False), {}
        if kwargs["max_tokens"] == 8_192:
            raise DeepSeekOutputTruncatedError("length", finish_reason="length")
        return '{"issues": []}', {}

    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr("app.services.review_service.DeepSeekAgent.call_raw", fake_call_raw)
    profile = GENERAL_AGENT.__class__(
        **{
            **GENERAL_AGENT.__dict__, "is_custom": True,
            "system_prompt": "规则" * 21_000, "max_tokens": 8_192,
        },
    )
    text, _meta = review_service._call_single_agent(profile, "tail = 1", "python", "a.py", [], 0)
    assert text == '{"issues": []}'
    main = [call for call in calls if call.get("agent_label") != "review_context_compaction"]
    assert [call["max_tokens"] for call in main] == [8_192, 16_384]
    assert any(call.get("agent_label") == "review_context_compaction" for call in calls)
    assert "tail = 1" in main[-1]["user_prompt"]
