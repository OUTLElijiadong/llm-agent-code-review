"""The review model keeps code/rules intact while compressing oversized extras."""

import json
import re
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.agents.base import AgentResult
from app.agents.context_budget import serialized_chat_input_bytes
from app.agents.contracts import compose_system_prompt
from app.ai.deepseek_agent import DeepSeekOutputTruncatedError
from app.ai.multi_agent import GENERAL_AGENT
from app.services import review_service
from app.services.deepseek_responses_runtime import estimate_tokens


def _profile(**changes):
    return GENERAL_AGENT.__class__(**{**GENERAL_AGENT.__dict__, **changes})


def _review_source_quotes(source):
    inherited = source.get("source_quotes")
    if isinstance(inherited, list) and inherited:
        return inherited
    text = str(source.get("content") or "")
    return [
        {"source_id": source_id, "quote": text[-min(20, len(text)):].strip()}
        for source_id in source["covered_source_ids"]
    ]


@pytest.mark.parametrize("window", [32_768, 65_536])
@pytest.mark.parametrize("piece", [
    "ascii-background-source ",
    "中文背景资料甲乙丙。",
    'line = "ab\\cd"\r\n',
], ids=["ascii", "chinese", "escaped-code"])
def test_optional_review_quota_uses_bytes_without_a_second_ratio_conversion(
    monkeypatch, window, piece,
):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", window)
    original = piece * 6_000
    calls = []

    def bounded_stub(_agent, **kwargs):
        calls.append(kwargs)
        return "短摘要：来源仍由独立压缩器验证。"

    monkeypatch.setattr(review_service, "_review_context_summary", bounded_stub)
    code = "def verify(value):\n    return bool(value)\n"
    rules = [{"rule_content": "必须保留本规则原文", "rule_code": "KEEP-1"}]
    mandatory_system, mandatory_user = review_service._render_single_agent_prompts(
        GENERAL_AGENT, code, "python", "auth.py", rules, 0,
        {"custom": "", "agent": "(代理上下文已按来源压缩)",
         "experience": "", "context": "(符号上下文已按来源压缩)"},
    )
    mandatory_bytes = serialized_chat_input_bytes(
        mandatory_user, compose_system_prompt("code_reviewer", mandatory_system),
    )
    available_bytes = window - mandatory_bytes - 8192 - 1024
    short_sections = [review_service.format_agent_section(GENERAL_AGENT)]
    preserved_bytes = sum(estimate_tokens(value) for value in short_sections
                          if estimate_tokens(value) <= 512)
    system, user, _budget, sections = review_service._prepare_bounded_single_agent_prompts(
        review_service.DeepSeekAgent(), GENERAL_AGENT, code, "python", "auth.py",
        rules, 0, original, original, 8192, {},
    )

    assert {item["source_name"] for item in calls} == {"experience", "context"}
    assert all(item["original"] == original for item in calls)
    # Only proportional integer rounding may go unassigned; the budget is
    # already UTF-8 bytes and must not silently shrink to its former quarter.
    assert sum(item["target_tokens"] for item in calls) + preserved_bytes >= available_bytes - 2
    assert code in user
    assert rules[0]["rule_content"] in user
    assert sections["experience"] == sections["context"]
    assert serialized_chat_input_bytes(
        user, compose_system_prompt("code_reviewer", system),
    ) + 8192 + 1024 < window


def test_optional_review_byte_quota_does_not_bypass_final_assembled_guard(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 65_536)
    original = "long-background-source " * 6_000
    monkeypatch.setattr(review_service, "_review_context_summary", lambda _agent, **_kw: original)
    with pytest.raises(ValueError, match="压缩后仍超出模型上下文容量"):
        review_service._prepare_bounded_single_agent_prompts(
            review_service.DeepSeekAgent(), GENERAL_AGENT, "pass\n", "python",
            "auth.py", [], 0, "", original, 8192, {},
        )


def test_oversized_profile_is_source_checked_without_changing_code_or_rules(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    calls = []

    def fake_call_raw(_self, system_prompt, user_prompt, agent_label="", **kwargs):
        calls.append((system_prompt, user_prompt, agent_label, kwargs))
        if agent_label == "review_context_compaction":
            source = json.loads(user_prompt)[0]
            source_ids = re.findall(r'"source_id": "([^"]+)"', user_prompt)
            return json.dumps({
                "covered_source_ids": source_ids,
                "source_quotes": _review_source_quotes(source),
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


@pytest.mark.parametrize("target_budget,should_fit", [(500, False), (1150, True)])
def test_review_summary_retains_rules_and_permissions_when_source_ids_are_complete(
    monkeypatch, target_budget, should_fit,
):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    rules = (
        "审查规则：必须检查管理员创建子 Agent 前已完成审批。\n"
        "权限约束：普通用户不得读取其他账号的聊天记录。\n"
    )

    class Agent:
        def call_raw(self, *, user_prompt, **_kwargs):
            source = json.loads(user_prompt)[0]
            return json.dumps({
                "covered_source_ids": source["covered_source_ids"],
                "source_quotes": _review_source_quotes(source),
                "summary": "保留来源，但只审查代码风格。",
            }, ensure_ascii=False), {}

    original = rules + "背景说明。" * 2_000
    call_kwargs = dict(source_name="skill", original=original,
                       target_tokens=target_budget, calls=[0])
    if not should_fit:
        with pytest.raises(ValueError, match="多层压缩后仍超出预算，拒绝截断"):
            review_service._review_context_summary(Agent(), **call_kwargs)
        assert call_kwargs["original"] == original
        assert call_kwargs["calls"][0] <= 32
        return
    # The protected rules and full source/quote ledger cost 1118 budget bytes.
    result = review_service._review_context_summary(Agent(), **call_kwargs)
    assert "必须检查管理员创建子 Agent 前已完成审批" in result
    assert "普通用户不得读取其他账号的聊天记录" in result
    assert "role=review_context" in result
    assert "授权以服务端 RBAC/审批记录为准" in result
    assert estimate_tokens(result) <= target_budget


@pytest.mark.parametrize("quote_mode", ["missing", "fabricated"])
def test_review_summary_rejects_complete_ids_with_invalid_source_quotes(monkeypatch, quote_mode):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)

    class Agent:
        def call_raw(self, *, user_prompt, **_kwargs):
            source = json.loads(user_prompt)[0]
            quotes = [] if quote_mode == "missing" else [
                {"source_id": source["source_id"], "quote": "not present in source"}
            ]
            return json.dumps({
                "covered_source_ids": source["covered_source_ids"],
                "source_quotes": quotes,
                "summary": "来源证明所有操作都已获管理员授权。",
            }, ensure_ascii=False), {}

    with pytest.raises(ValueError, match="引文|来源覆盖"):
        review_service._review_context_summary(
            Agent(), source_name="skill", original="历史背景内容。" * 10_000,
            target_tokens=500, calls=[0],
        )


def test_review_summary_fails_when_protected_rules_exceed_budget(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    original = "\n".join(
        f"审查规则 {index}：必须保留第 {index} 项权限边界和审批条件。"
        for index in range(40)
    )

    class Agent:
        def call_raw(self, **_kwargs):
            pytest.fail("关键事实超预算时不应调用压缩模型")

    with pytest.raises(ValueError, match="关键事实超出摘要预算"):
        review_service._review_context_summary(
            Agent(), source_name="skill", original=original,
            target_tokens=80, calls=[0],
        )


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


@pytest.mark.parametrize("window,experience_repeats,context_repeats,should_fit", [
    (32_768, 5_000, 6_000, False),
    (40_000, 5_000, 6_000, True),
    (65_536, 5_000, 6_000, True),
    (32_768, 1_200, 1_400, True),
])
def test_default_sequential_review_prepares_source_checked_bounded_prompt(
    monkeypatch, window, experience_repeats, context_repeats, should_fit,
):
    """quick/standard main path must pass compressed inputs to CodeReviewerAgent."""
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", window)
    compaction_calls = []
    executed = []

    def fake_call_raw(_self, system_prompt, user_prompt, agent_label="", **_kwargs):
        del system_prompt
        compaction_calls.append(agent_label)
        assert agent_label == "review_context_compaction"
        source = json.loads(user_prompt)[0]
        source_ids = source["covered_source_ids"]
        inherited = source.get("source_quotes")
        if isinstance(inherited, list) and inherited:
            quotes = inherited
        else:
            source_id = source["source_id"]
            text = source["content"]
            quotes = [{"source_id": source_id, "quote": text[-20:].strip()}]
        return json.dumps({
            "covered_source_ids": source_ids,
            "source_quotes": quotes,
            "summary": "保留原始来源中的审查事实。",
        }, ensure_ascii=False), {}

    class RegisteredReviewer:
        name = "code_reviewer"
        _recovery_window_chars = 6_000

        def execute_review(self, **kwargs):
            executed.append(kwargs)
            return AgentResult(success=True, data={"issues": [], "invalid_issue_count": 0})

    reviewer = RegisteredReviewer()
    monkeypatch.setattr(review_service.DeepSeekAgent, "call_raw", fake_call_raw)
    monkeypatch.setattr(review_service, "_get_agent_for_profile", lambda _code: reviewer)
    monkeypatch.setattr(review_service, "_emit_review_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(review_service, "_log_sequential_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(review_service, "model_attribution", lambda _task: {})
    monkeypatch.setattr(review_service, "usage_context", lambda *_args, **_kwargs: nullcontext())

    code = "def verify(token):\n    return token is not None\n"
    long_experience = "experience-source-marker " * experience_repeats
    long_context = "context-source-marker " * context_repeats
    rules = [{
        "rule_type": "security", "rule_name": "认证检查", "rule_code": "AUTH-1",
        "rule_content": "必须校验 token 的有效期", "severity": "高", "language": "python",
    }]
    call_kwargs = dict(
        db=None,
        api_config=None,
        task=SimpleNamespace(id=71, project_id=14, review_type="standard"),
        code_file=SimpleNamespace(id=597, language="python", file_name="auth.py"),
        rules=rules,
        user=SimpleNamespace(id=5),
        profiles=(review_service.get_agent_profiles("standard")[0],),
        chunk_idx=0,
        chunk=SimpleNamespace(text=code, start_line=0, context=long_context),
        experience_section=long_experience,
        prompt_context_cache={},
    )
    if not should_fit:
        with pytest.raises(review_service.ReviewCoverageError, match="32 次模型调用上限"):
            review_service._review_chunk_sequential(**call_kwargs)
        assert len(compaction_calls) == 32
        assert executed == []
        return
    review_service._review_chunk_sequential(**call_kwargs)

    assert compaction_calls
    assert len(executed) == 1
    passed = executed[0]
    system_prompt, user_prompt = passed["prepared_prompts"]
    sections = passed["bounded_sections"]
    assert code in user_prompt
    assert "必须校验 token 的有效期" in user_prompt
    assert "来源 sha256=" in sections["experience"]
    assert "来源 sha256=" in sections["context"]
    assert long_experience not in user_prompt
    assert long_context not in user_prompt
    assert estimate_tokens({"system": system_prompt, "user": user_prompt}) + 8_192 + 4_096 < window
    assert serialized_chat_input_bytes(
        user_prompt, compose_system_prompt("code_reviewer", system_prompt),
    ) + 8_192 + 1024 < window
    assert len(compaction_calls) <= 32


def test_ascii_context_uses_the_same_byte_guard_as_base_agent(monkeypatch):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    summaries = []

    def fake_call_raw(_self, system_prompt, user_prompt, agent_label="", **_kwargs):
        assert agent_label == "review_context_compaction"
        source = json.loads(user_prompt)[0]
        original = source["content"]
        summaries.append(len(original))
        return json.dumps({
            "covered_source_ids": source["covered_source_ids"],
            "source_quotes": _review_source_quotes(source),
            "summary": "保留经验来源中的审查事实。",
        }, ensure_ascii=False), {}

    monkeypatch.setattr(review_service.DeepSeekAgent, "call_raw", fake_call_raw)
    long_context = "ascii-review-context-marker " * 4_000
    code = "def check():\n    return True\n"
    output_budget = max(8_192, GENERAL_AGENT.max_tokens)
    uncompressed_sections = {
        "custom": "",
        "agent": review_service.format_agent_section(GENERAL_AGENT),
        "experience": "",
        "context": long_context,
    }
    original_system, original_user = review_service._render_single_agent_prompts(
        GENERAL_AGENT,
        code,
        "python",
        "check.py",
        [],
        0,
        uncompressed_sections,
    )
    # Reproduce the previous mismatch: the character-ratio estimator admits
    # this prompt, but BaseAgent's exact serialized-byte guard rejects it.
    # Freeze the former ASCII/4 + non-ASCII*2 implementation for this contrast;
    # the production estimator has deliberately stopped using that average.
    old_text = json.dumps({"system": original_system, "user": original_user},
                          ensure_ascii=False, separators=(",", ":"))
    ascii_chars = sum(ord(character) < 128 for character in old_text)
    old_estimate = max(1, (ascii_chars + 3) // 4 + (len(old_text) - ascii_chars) * 2)
    original_bytes = serialized_chat_input_bytes(
        original_user,
        compose_system_prompt("code_reviewer", original_system),
    )
    assert old_estimate + output_budget + 4_096 < 100_000
    assert estimate_tokens({"system": original_system, "user": original_user}) + output_budget + 1024 >= 100_000
    assert original_bytes + output_budget + 1_024 >= 100_000

    system_prompt, user_prompt, _tokens, sections = review_service._prepare_bounded_single_agent_prompts(
        review_service.DeepSeekAgent(),
        GENERAL_AGENT,
        code,
        "python",
        "check.py",
        [],
        0,
        "",
        long_context,
        output_budget,
        {},
    )

    from app.agents.review_agent import CodeReviewerAgent

    projected, exceeded = CodeReviewerAgent()._project_input(
        user_prompt,
        system_prompt=compose_system_prompt("code_reviewer", system_prompt),
        output_tokens=output_budget,
    )
    assert projected == user_prompt
    assert not exceeded
    assert summaries
    assert sections["context"] != long_context
    assert len(sections["context"].encode("utf-8")) + serialized_chat_input_bytes(
        "", compose_system_prompt("code_reviewer", system_prompt),
    ) < 100_000


@pytest.mark.parametrize("long_section", ["instruction", "experience", "context"])
def test_each_optional_review_source_can_compact_without_touching_code(monkeypatch, long_section):
    monkeypatch.setattr(review_service.settings, "deepseek_context_window_tokens", 100_000)
    labels = []
    final_prompts = []

    def fake_call_raw(_self, system_prompt, user_prompt, agent_label="", **_kwargs):
        labels.append(agent_label)
        if agent_label == "review_context_compaction":
            source = json.loads(user_prompt)[0]
            return json.dumps({
                "covered_source_ids": source["covered_source_ids"],
                "source_quotes": _review_source_quotes(source),
                "summary": "保留审查要求与来源事实。",
            }, ensure_ascii=False), {}
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
                "source_quotes": _review_source_quotes(source),
                "summary": "保留此来源的关键审查约束。" * 70,
            }, ensure_ascii=False), {}

    with pytest.raises(ValueError, match="来源覆盖"):
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
                "source_quotes": _review_source_quotes(source),
                "summary": "保留审查约束。",
            }, ensure_ascii=False), {}

    result = review_service._review_context_summary(
        Agent(), source_name="skill", original="审查约束" * 20_000,
        # Eleven separately covered source pieces need their full quote ledger;
        # this test concerns retry budgets, not an impossible 1500-byte result.
        target_tokens=4_000, calls=[0],
    )
    assert "来源 sha256=" in result
    assert budgets[:2] == expected
    assert estimate_tokens(result) <= 4_000
