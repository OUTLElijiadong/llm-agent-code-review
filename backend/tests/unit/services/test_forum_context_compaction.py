"""Forum writing assistance must cover every source before a large model call."""

import hashlib
import json

from app.ai.deepseek_agent import DeepSeekOutputTruncatedError
from app.ai.exceptions import AiServiceError
from app.services import personalization_service


def _forum_dependencies(monkeypatch, *, response_fn, profile="", hits=None):
    from app.ai import deepseek_agent
    from app.utils import api_resolver

    calls = []

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def chat(self, **kwargs):
            calls.append(kwargs)
            return response_fn(kwargs), {}

    monkeypatch.setattr(personalization_service.knowledge_service, "retrieve", lambda *_a, **_k: hits or [])
    monkeypatch.setattr(personalization_service.profile_service, "get_summary_text", lambda *_a: profile)
    monkeypatch.setattr(api_resolver, "resolve_api_config", lambda *_a: object())
    monkeypatch.setattr(personalization_service, "resolve_subagent_config", lambda *_a: object())
    monkeypatch.setattr(deepseek_agent, "DeepSeekAgent", FakeAgent)
    return calls


def test_long_forum_draft_compacts_all_source_parts_before_final_request(monkeypatch):
    monkeypatch.setattr(personalization_service.settings, "deepseek_context_window_tokens", 20_000)

    def respond(kwargs):
        if kwargs["agent_label"] == "forum_context_compaction":
            source = json.loads(kwargs["user_prompt"])
            return json.dumps({
                "source_id": source["source_id"],
                "sha256": source["sha256"],
                "quote": source["content"][:20],
                "summary": "保留本片的事实与表达意图。",
            }, ensure_ascii=False)
        return '{"suggestion":"结构建议"}'

    calls = _forum_dependencies(monkeypatch, response_fn=respond, profile="偏好中文说明")
    result = personalization_service.assist_forum_draft(
        None, 7, "请检查发布步骤", "证据" * 10_000 + "最后请保留发布步骤",
    )

    compact_calls = [call for call in calls if call["agent_label"] == "forum_context_compaction"]
    final = calls[-1]
    assert len(compact_calls) > 1
    assert final["agent_label"] == "forum_assist"
    assert "请检查发布步骤" in final["user_prompt"]
    assert "最后请保留发布步骤" in final["user_prompt"]
    assert all(json.loads(call["user_prompt"])["source_id"] in final["user_prompt"] for call in compact_calls)
    assert result["suggestion"] == "结构建议"


def test_forum_compaction_rejects_missing_or_unverifiable_source(monkeypatch):
    monkeypatch.setattr(personalization_service.settings, "deepseek_context_window_tokens", 20_000)

    def respond(kwargs):
        source = json.loads(kwargs["user_prompt"])
        return json.dumps({
            "source_id": source["source_id"],
            "sha256": hashlib.sha256(b"wrong source").hexdigest(),
            "quote": "不在原文中的引文",
            "summary": "伪造摘要",
        }, ensure_ascii=False)

    calls = _forum_dependencies(monkeypatch, response_fn=respond)
    result = personalization_service.assist_forum_draft(None, 7, "标题", "证据" * 10_000)

    assert result["context_complete"] is False
    assert "未生成 AI 建议" in result["suggestion"]
    assert all(call["agent_label"] == "forum_context_compaction" for call in calls)


def test_short_forum_draft_keeps_original_context_without_compaction(monkeypatch):
    monkeypatch.setattr(personalization_service.settings, "deepseek_context_window_tokens", 20_000)
    calls = _forum_dependencies(
        monkeypatch, response_fn=lambda _kwargs: '{"suggestion":"补充复现步骤"}',
        profile="偏好先写结论",
    )

    result = personalization_service.assist_forum_draft(None, 7, "标题", "草稿原文")

    assert [call["agent_label"] for call in calls] == ["forum_assist"]
    assert "草稿原文" in calls[0]["user_prompt"]
    assert result["suggestion"] == "补充复现步骤"


def test_forum_budget_counts_full_utf8_message_bytes(monkeypatch):
    monkeypatch.setattr(personalization_service.settings, "deepseek_context_window_tokens", 10_000)
    assert not personalization_service._forum_input_fits("系统", "😀" * 1_500, output_tokens=4096)


def test_forum_compactor_retries_length_with_larger_output_budget(monkeypatch):
    monkeypatch.setattr(personalization_service.settings, "deepseek_context_window_tokens", 20_000)
    attempted_budgets = []

    def respond(kwargs):
        if kwargs["agent_label"] == "forum_context_compaction":
            attempted_budgets.append(kwargs["max_tokens"])
            if len(attempted_budgets) == 1:
                raise AiServiceError("length", code=50201) from DeepSeekOutputTruncatedError(
                    "length", finish_reason="length",
                )
            source = json.loads(kwargs["user_prompt"])
            return json.dumps({
                "source_id": source["source_id"], "sha256": source["sha256"],
                "quote": source["content"][:20], "summary": "保留来源事实。",
            }, ensure_ascii=False)
        return '{"suggestion":"已形成建议"}'

    _forum_dependencies(monkeypatch, response_fn=respond)
    result = personalization_service.assist_forum_draft(None, 7, "标题", "证据" * 10_000)

    assert attempted_budgets[:2] == [2048, 4096]
    assert result["suggestion"] == "已形成建议"


def test_maximum_forum_draft_stays_within_source_call_budget(monkeypatch):
    monkeypatch.setattr(personalization_service.settings, "deepseek_context_window_tokens", 100_000)

    def respond(kwargs):
        if kwargs["agent_label"] == "forum_context_compaction":
            source = json.loads(kwargs["user_prompt"])
            return json.dumps({
                "source_id": source["source_id"], "sha256": source["sha256"],
                "quote": source["content"][:20], "summary": "本片资料已归纳。",
            }, ensure_ascii=False)
        return '{"suggestion":"完整草稿建议"}'

    calls = _forum_dependencies(monkeypatch, response_fn=respond)
    result = personalization_service.assist_forum_draft(None, 7, "标题", "中" * 99_988 + "末尾目标必须保留")

    assert result["suggestion"] == "完整草稿建议"
    assert len([call for call in calls if call["agent_label"] == "forum_context_compaction"]) <= 32
    assert "末尾目标必须保留" in calls[-1]["user_prompt"]
