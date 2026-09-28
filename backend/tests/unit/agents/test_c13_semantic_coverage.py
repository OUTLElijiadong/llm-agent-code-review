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
    FAILED,
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
            return SimpleNamespace(success=True, data={"covered_source_ids": covered, "summary": summary})

    result = compact_source_context(FaithfulAgent(), source, ctx=None, max_chars=450)
    assert calls == [[source_id] for source_id in ids] + [ids]
    assert result["covered_source_ids"] == ids
    assert len(result["source_summaries"]) == 1
    rendered = json.dumps(result, ensure_ascii=False)
    assert all(fact in rendered for fact in _FACTS)
    assert rendered.index(_FACTS[1]) < rendered.index(_FACTS[2])
    assert all(chunk["sha256"][:12] in rendered for chunk in source["source_chunks"])


@pytest.mark.xfail(strict=True, reason="正确来源 ID 和非空摘要不能检测模型遗漏的关键事实")
def test_source_compaction_rejects_model_summary_that_omits_middle_permission_fact() -> None:
    source = _source_summary()

    class OmittingAgent:
        def call_json(self, message, **_kwargs):
            return SimpleNamespace(success=True, data={
                "covered_source_ids": _batch_ids(message),
                "summary": "已阅读源码并了解项目。",
            })

    with pytest.raises(SourceContextError, match="关键事实|语义|遗漏"):
        compact_source_context(OmittingAgent(), source, ctx=None)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="来源标记齐全的模型摘要仍可能遗漏中段约束")
async def test_runtime_rejects_complete_anchors_but_missing_middle_restriction() -> None:
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
                anchors = sorted(set(re.findall(r"\[来源#\d+:片段\d+/\d+\]|\[压缩块#\d+\]", source)))
                return _message_response("已阅读全部历史 " + " ".join(anchors))
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
    assert result.status == FAILED or (result.status == COMPLETED and middle_fact in final_input)


def _message_response(text: str) -> dict:
    return {
        "id": "resp_c13", "object": "response", "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text},
        ]}],
    }
