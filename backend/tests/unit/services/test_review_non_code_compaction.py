"""The review model keeps code/rules intact while compressing oversized extras."""

import json
import re

import pytest

from app.ai.deepseek_agent import DeepSeekOutputTruncatedError
from app.ai.multi_agent import GENERAL_AGENT
from app.services import review_service
from app.services.deepseek_responses_runtime import estimate_tokens


def _profile(**changes):
    return GENERAL_AGENT.__class__(**{**GENERAL_AGENT.__dict__, **changes})


def test_oversized_profile_is_source_checked_without_changing_code_or_rules(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    calls = []

    def fake_call_raw(_self, system_prompt, user_prompt, agent_label="", **kwargs):
        calls.append((system_prompt, user_prompt, agent_label, kwargs))
        if agent_label == "review_context_compaction":
            source_ids = re.findall(r'"source_id": "([^"]+)"', user_prompt)
            return json.dumps({
                "covered_source_ids": source_ids,
                "summary": "此画像要求对认证路径和异常处理做完整审查。",
            }, ensure_ascii=False), {}
        return '{"issues": []}', {}

    monkeypatch.setattr(review_service.DeepSeekAgent, "call_raw", fake_call_raw)
    code = "def login(password):\n    return password == 'expected'\n"
    rules = [{"rule_type": "security", "rule_name": "认证规则", "rule_code": "AUTH-1",
              "rule_content": "必须检查认证条件", "severity": "高", "language": "python"}]
    profile = _profile(is_custom=True, system_prompt="关注认证和异常路径。" * 10_000)

    result, _meta = review_service._call_single_agent(
        profile, code, "python", "auth.py", rules, 0,
        experience_section="经验：未检查 token 有效期。", context_section="符号：login→verify。",
    )

    assert result == '{"issues": []}'
    assert any(item[2] == "review_context_compaction" for item in calls)
    main = calls[-1]
    assert main[2] != "review_context_compaction"
    assert code in main[1]
    assert "必须检查认证条件" in main[1]
    assert "平台强制契约" in main[0]
    assert "来源 sha256=" in main[0]
    assert estimate_tokens({"system": main[0], "user": main[1]}) + 16_384 + 1024 < 100_000


def test_unverified_non_code_summary_rejects_review_before_main_model(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    labels = []

    def fake_call_raw(_self, system_prompt, user_prompt, agent_label="", **_kwargs):
        del system_prompt, user_prompt
        labels.append(agent_label)
        return json.dumps({"covered_source_ids": [], "summary": "缺少来源"}, ensure_ascii=False), {}

    monkeypatch.setattr(review_service.DeepSeekAgent, "call_raw", fake_call_raw)
    profile = _profile(is_custom=True, system_prompt="认证约束" * 12_000)
    with pytest.raises(ValueError, match="来源|覆盖|压缩"):
        review_service._call_single_agent(profile, "pass\n", "python", "auth.py", [], 0)
    assert labels == ["review_context_compaction"]


def test_code_and_rules_alone_over_window_fail_without_compressing_source(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    labels = []

    def fake_call_raw(_self, _system_prompt, _user_prompt, agent_label="", **_kwargs):
        labels.append(agent_label)
        return '{"issues": []}', {}

    monkeypatch.setattr(review_service.DeepSeekAgent, "call_raw", fake_call_raw)
    with pytest.raises(ValueError, match="源码|规则"):
        review_service._call_single_agent(
            GENERAL_AGENT, "重要源码。" * 16_000, "python", "big.py", [], 0,
            experience_section="历史经验" * 100,
        )
    assert labels == []


@pytest.mark.parametrize("long_section", ["instruction", "experience", "context"])
def test_each_optional_review_source_can_compact_without_touching_code(monkeypatch, long_section):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    labels = []
    final_prompts = []

    def fake_call_raw(_self, system_prompt, user_prompt, agent_label="", **_kwargs):
        labels.append(agent_label)
        if agent_label == "review_context_compaction":
            ids = re.findall(r'"source_id": "([^"]+)"', user_prompt)
            return json.dumps({"covered_source_ids": ids, "summary": "保留审查要求与来源事实。"},
                              ensure_ascii=False), {}
        final_prompts.append((system_prompt, user_prompt))
        return '{"issues": []}', {}

    monkeypatch.setattr(review_service.DeepSeekAgent, "call_raw", fake_call_raw)
    extra = "已确认的审查要求。" * 11_000
    profile = _profile(instruction=extra if long_section == "instruction" else GENERAL_AGENT.instruction)
    review_service._call_single_agent(
        profile, "sentinel = True\n", "python", "a.py", [], 0,
        experience_section=extra if long_section == "experience" else "",
        context_section=extra if long_section == "context" else "",
    )
    assert "review_context_compaction" in labels
    assert len(final_prompts) == 1
    assert "sentinel = True" in final_prompts[0][1]
    assert "来源 sha256=" in final_prompts[0][1]


def test_second_layer_review_summary_must_repeat_all_original_source_ids(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    second_layer_seen = []

    class Agent:
        def call_raw(self, *, user_prompt, **_kwargs):
            source = json.loads(user_prompt)[0]
            ids = source["covered_source_ids"]
            if ":第1层块" in source["source_id"]:
                second_layer_seen.append(True)
                ids = ids[:-1]
            return json.dumps({
                "covered_source_ids": ids,
                "summary": "保留此来源的关键审查约束。" * 70,
            }, ensure_ascii=False), {}

    with pytest.raises(ValueError, match="来源覆盖不完整"):
        review_service._review_context_summary(
            Agent(), source_name="skill", original="审查约束" * 20_000,
            target_tokens=500, calls=[0],
        )
    assert second_layer_seen


@pytest.mark.parametrize("configured, expected", [
    (512, [512, 1024]), (8192, [2048, 4096]),
])
def test_review_context_compactor_retries_length_with_larger_output_budget(
    monkeypatch, configured, expected,
):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr(review_service.settings, "deepseek_max_output_tokens", configured)
    budgets = []

    class Agent:
        def call_raw(self, *, user_prompt, max_tokens, **_kwargs):
            budgets.append(max_tokens)
            if len(budgets) == 1:
                raise DeepSeekOutputTruncatedError("length", finish_reason="length")
            source = json.loads(user_prompt)[0]
            return json.dumps({
                "covered_source_ids": source["covered_source_ids"],
                "summary": "保留审查约束。",
            }, ensure_ascii=False), {}

    result = review_service._review_context_summary(
        Agent(), source_name="skill", original="审查约束" * 20_000,
        target_tokens=1_000, calls=[0],
    )
    assert "来源 sha256=" in result
    assert budgets[:2] == expected
