"""C13: distinguish provenance coverage from actual semantic preservation."""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from app.agents.source_context import SourceContextError, compact_source_context
from app.services.deepseek_responses_runtime import (
    COMPLETED,
    DeepSeekResponsesRuntime,
    InMemoryCheckpointStore,
    compact_transcript,
)

_FACTS = (
    "仅当前账号可读",
    "最多并行 3 个 Agent",
    "更正：最多并行 2 个 Agent",
    "未经管理员批准不得发布",
)


def _source_summary() -> dict:
    texts = [
        f"入口权限：{_FACTS[0]}。" + "背景甲" * 60,
        f"初始限制：{_FACTS[1]}。{_FACTS[2]}。" + "背景乙" * 60,
        f"最终操作限制：{_FACTS[3]}。" + "背景丙" * 60,
    ]
    chunks = []
    for index, text in enumerate(texts):
        digest = hashlib.sha256(text.encode()).hexdigest()
        chunks.append({
            "source_id": f"chunk-{index}-{digest[:12]}",
            "text": text,
            "sha256": digest,
        })
    return {"coverage_complete": True, "source_chunk_count": len(chunks), "source_chunks": chunks}


def _batch_ids(message: str) -> list[str]:
    match = re.search(r"本批来源 ID: (\[[^\n]+\])", message)
    assert match is not None
    return json.loads(match.group(1))


def _source_quotes(message: str) -> list[dict[str, object]]:
    payload = json.loads(message.split("原始材料:\n", 1)[1])
    entries = payload if isinstance(payload, list) else [payload]
    return [
        {"source_id": item["source_id"], "quotes": [(item.get("text") or item.get("summary"))[:16]]}
        for item in entries
    ]


def test_compacted_history_remains_user_priority_data() -> None:
    transcript = [{"role": "user", "content": "核查本项目"}]
    transcript.extend({"role": "user", "content": "历史消息" + "甲" * 100} for _ in range(10))
    projected, metadata = compact_transcript(
        transcript,
        context_window_tokens=5000,
        max_output_tokens=400,
        compaction_threshold_tokens=500,
        keep_recent_tokens=200,
    )
    assert metadata["compacted"] is True
    assert projected[0]["role"] == "user"
    assert "平台上下文压缩" in projected[0]["content"][0]["text"]


def test_two_layer_source_compaction_preserves_head_middle_correction_and_tail() -> None:
    source = _source_summary()
    ids = [chunk["source_id"] for chunk in source["source_chunks"]]
    calls = []

    class FaithfulAgent:
        def call_json(self, message, **_kwargs):
            covered = _batch_ids(message)
            calls.append(covered)
            if len(covered) == 1:
                index = ids.index(covered[0])
                summary = "；".join(_FACTS[0:1] if index == 0 else _FACTS[1:3] if index == 1 else _FACTS[3:])
            else:
                summary = "；".join(_FACTS)
            return SimpleNamespace(success=True, data={
                "covered_source_ids": covered, "source_quotes": _source_quotes(message),
                "summary": summary,
            })

    with pytest.raises(SourceContextError, match="超过模型预算"):
        compact_source_context(FaithfulAgent(), source, ctx=None, max_chars=500)
    calls.clear()
    result = compact_source_context(FaithfulAgent(), source, ctx=None, max_chars=700)
    assert calls == [[source_id] for source_id in ids] + [ids]
    assert result["covered_source_ids"] == ids
    assert len(result["source_summaries"]) == 1
    rendered = json.dumps(result, ensure_ascii=False)
    assert all(fact in rendered for fact in _FACTS)
    assert rendered.index(_FACTS[1]) < rendered.index(_FACTS[2])
    assert all(chunk["sha256"][:12] in rendered for chunk in source["source_chunks"])


def test_source_compaction_preserves_middle_permission_fact_when_model_summary_omits_it() -> None:
    source = _source_summary()

    class OmittingAgent:
        def call_json(self, message, **_kwargs):
            return SimpleNamespace(success=True, data={
                "covered_source_ids": _batch_ids(message),
                "source_quotes": _source_quotes(message),
                "summary": "已阅读源码并了解项目。",
            })

    result = compact_source_context(OmittingAgent(), source, ctx=None)
    rendered = json.dumps(result, ensure_ascii=False)
    assert all(fact in rendered for fact in _FACTS)


@pytest.mark.asyncio
async def test_runtime_preserves_middle_restriction_when_model_summary_has_all_anchors() -> None:
    middle_fact = "中段权限：不得跨账号读取聊天"
    transcript = [{"role": "user", "content": "审查目标：仅核查当前项目"}]
    transcript.extend({"role": "assistant", "content": f"历史讨论 {i}：" + "甲" * 80} for i in range(9))
    transcript.append({"role": "user", "content": "中段背景" * 200 + middle_fact})
    transcript.extend({"role": "assistant", "content": f"历史讨论 {i}：" + "乙" * 80} for i in range(9, 18))
    transcript.append({"role": "user", "content": "末尾更正：最多并行 2 个 Agent，未经审批不得发布"})

    class OmittingTransport:
        def __init__(self) -> None:
            self.payloads: list[Mapping[str, Any]] = []

        async def create_response(self, payload: Mapping[str, Any]) -> dict:
            self.payloads.append(payload)
            if not payload["tools"]:
                source = str(payload["input"][0]["content"])
                anchors = re.findall(r"\[来源#\d+:片段\d+/\d+\]", source)
                spans = list(re.finditer(r"\[来源#\d+:片段\d+/\d+\]", source))
                quotes = []
                for index, (anchor, span) in enumerate(zip(anchors, spans)):
                    end = spans[index + 1].start() if index + 1 < len(spans) else len(source)
                    raw_piece = source[span.end():end].strip()
                    raw_piece = re.sub(r"^\[来源角色=[^\]]+\]\s*", "", raw_piece)
                    quote = raw_piece[:24]
                    quotes.append({"source_id": anchor[1:-1], "quote": quote})
                response = {
                    "covered_source_ids": [anchor[1:-1] for anchor in anchors],
                    "source_quotes": quotes,
                    "summary": "已阅读全部历史 " + " ".join(anchors),
                }
                return _message_response(json.dumps(response, ensure_ascii=False))
            return _message_response("已完成审查")

    transport = OmittingTransport()
    runtime = DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=SimpleNamespace(),
        checkpoint_store=InMemoryCheckpointStore(),
        context_window_tokens=5000,
        max_output_tokens=400,
        compaction_threshold_tokens=600,
        keep_recent_tokens=200,
        stream=False,
    )
    result = await runtime.start(transcript, run_id="c13_missing_middle")
    compact_payloads = [payload for payload in transport.payloads if not payload["tools"]]
    assert any(middle_fact in json.dumps(payload, ensure_ascii=False) for payload in compact_payloads)
    model_payload = transport.payloads[-1]
    final_input = json.dumps(model_payload["input"], ensure_ascii=False)
    projected_summary = model_payload["input"][0]["content"][0]["text"]
    assert result.status == COMPLETED
    assert middle_fact in final_input
    assert '"source_role":"user"' in projected_summary
    assert '"source_role":"assistant"' in projected_summary
    assert "来源角色与授权边界" in final_input
    assert "唯一授权依据" in final_input


def _message_response(text: str) -> dict:
    return {
        "id": "resp_c13", "object": "response", "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text},
        ]}],
    }
