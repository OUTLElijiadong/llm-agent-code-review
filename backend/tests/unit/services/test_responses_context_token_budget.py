"""Budget regressions measured with the pinned official V4.1-Flash tokenizer.

Frozen counts are local content-token counts, not provider usage or full prompt
counts.  No tokenizer dependency, download or model request is needed here.
The provenance and standalone remeasurement probe live in the audit evidence.
"""

from __future__ import annotations

import copy

import pytest

from app.services.deepseek_responses_runtime import (
    ContextBudgetError, DeepSeekResponsesRuntime, InMemoryCheckpointStore,
    RunCheckpoint, _semantic_compaction_json_schema, _split_compaction_source,
    compact_transcript, estimate_tokens,
)


@pytest.mark.parametrize(
    ("unit", "repeats", "official_content_tokens"),
    [
        (" a", 10_000, 10_000),
        (".a", 10_000, 10_000),
        (" 7", 10_000, 20_000),
        ('{"a":1},', 10_000, 40_001),
        ("x=1;\n", 10_000, 40_000),
        ("信息", 10_000, 10_000),
        ("龘", 10_000, 20_000),
    ],
    ids=["words", "punctuation", "numbers", "json", "code", "chinese", "rare-chinese"],
)
def test_unknown_tokenizer_budget_does_not_undercount_measured_content(
    unit: str, repeats: int, official_content_tokens: int,
) -> None:
    assert estimate_tokens(unit * repeats) >= official_content_tokens


@pytest.mark.parametrize(
    ("unit", "per_message", "message_count", "official_content_tokens"),
    [
        (" 7", 26_500, 20, 1_060_022),
        (" a", 26_500, 40, 1_060_022),
        ("x=1;\n", 10_000, 27, 1_080_022),
    ],
    ids=["million-numbers", "million-words", "million-code"],
)
def test_million_local_content_tokens_trigger_sourced_projection_without_mutation(
    unit: str, per_message: int, message_count: int, official_content_tokens: int,
) -> None:
    history = [
        {"role": "user", "content": "Analyse the supplied material. Only provide a concise read-only answer."},
        *[{"role": "user", "content": unit * per_message} for _ in range(message_count)],
        {"role": "user", "content": "Return a concise summary of the material."},
    ]
    original = copy.deepcopy(history)
    assert official_content_tokens > 1_000_000
    assert len(history) <= 100
    assert max(len(item["content"]) for item in history) <= 100_000

    projected, metadata = compact_transcript(
        history,
        context_window_tokens=1_000_000,
        max_output_tokens=65_536,
        compaction_threshold_tokens=850_000,
        keep_recent_tokens=200_000,
        overhead_tokens=4096,
        # This only tests planning. Actual model summaries need source validation.
        semantic_summary="[local planning fixture; no model semantic claim]",
    )

    assert metadata["compacted"] is True
    assert metadata["omitted_items"] > 0
    assert metadata["summary_sha256"]
    assert estimate_tokens(projected) + 65_536 + 4096 <= 1_000_000
    assert history == original


@pytest.mark.asyncio
async def test_compactor_reserves_actual_structured_output_schema_before_transport() -> None:
    class NeverSend:
        calls = 0

        async def create_response(self, _payload):
            self.calls += 1
            raise AssertionError("oversized protocol schema must not be sent")

    transport = NeverSend()
    store = InMemoryCheckpointStore()
    checkpoint = RunCheckpoint(run_id="schema-byte-bound", model="local-no-model", transcript=[], tools=[])
    await store.create(checkpoint)
    runtime = DeepSeekResponsesRuntime(
        transport=transport, tool_executor=object(), checkpoint_store=store,
        context_window_tokens=7000, max_output_tokens=512,
        compaction_threshold_tokens=5000, keep_recent_tokens=1000,
    )
    ids = [f"source-{index}-" + "s" * 1000 for index in range(10)]
    with pytest.raises(ContextBudgetError, match="压缩请求本身超出"):
        await runtime._call_compactor(
            checkpoint, instruction="compress only", source="small source", max_output_tokens=512,
            expected_source_ids=ids, source_texts={key: "small source" for key in ids},
        )
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_compactor_rechecks_added_validation_retry_instructions_before_transport() -> None:
    instruction = "compress only"
    source = "small source"
    output_budget = 512
    initial_payload = {
        "model": "local-no-model", "instructions": instruction,
        "input": [{"role": "user", "content": source}], "tools": [],
        "stream": True, "reasoning": {"effort": "none"},
        "text": {"format": {"type": "json_schema", "name": "semantic_compaction",
                            "schema": _semantic_compaction_json_schema(["S1"])}},
    }
    # The first request fits; the mandatory repair instructions do not. Keep
    # enough headroom for the original payload, but less than the retry addition.
    context_window = estimate_tokens(initial_payload) + 1024 + output_budget + 80

    class InvalidFirst:
        calls = 0

        async def create_response(self, payload):
            self.calls += 1
            assert self.calls == 1, "oversized validation retry must not be sent"
            assert payload["instructions"] == instruction
            return {"id": "local-invalid", "status": "completed", "output": [
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "not valid structured JSON"},
                ]},
            ]}

    transport = InvalidFirst()
    store = InMemoryCheckpointStore()
    checkpoint = RunCheckpoint(run_id="retry-byte-bound", model="local-no-model", transcript=[], tools=[])
    await store.create(checkpoint)
    runtime = DeepSeekResponsesRuntime(
        transport=transport, tool_executor=object(), checkpoint_store=store,
        context_window_tokens=context_window, max_output_tokens=output_budget,
        compaction_threshold_tokens=context_window - 600, keep_recent_tokens=256,
    )
    with pytest.raises(ContextBudgetError, match="压缩请求本身超出"):
        await runtime._call_compactor(
            checkpoint, instruction=instruction, source=source, max_output_tokens=output_budget,
            expected_source_ids=["S1"], source_texts={"S1": source},
        )
    assert transport.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("instructions", ["仅回复已核验结果。", "账号归属和权限由服务端验证。" * 2000],
                         ids=["short-instructions", "long-instructions"])
@pytest.mark.parametrize("output_budget", [32_768, 65_536], ids=["runtime-fallback", "configured-default"])
async def test_default_window_and_output_allow_instructions_and_tool_schema(instructions: str, output_budget: int) -> None:
    payloads = []

    class CompleteAnswer:
        async def create_response(self, payload):
            payloads.append(payload)
            return {"id": "local-default-completed", "status": "completed", "output": [
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "本地协议验证完成"},
                ]},
            ]}

    store = InMemoryCheckpointStore()
    runtime = DeepSeekResponsesRuntime(
        transport=CompleteAnswer(), tool_executor=object(), checkpoint_store=store,
        max_output_tokens=output_budget,
    )
    history = [{"role": "user", "content": "请说明当前任务"}]
    original = copy.deepcopy(history)
    result = await runtime.start(history, instructions=instructions, tools=[{
        "type": "function", "name": "read_current_account",
        "description": "只读查询当前账号",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }])
    assert result.status == "completed" and len(payloads) == 1
    assert payloads[0]["max_output_tokens"] == output_budget
    assert estimate_tokens({key: value for key, value in payloads[0].items()
                            if key != "max_output_tokens"}) + 1024 + output_budget < 1_000_000
    assert history == original
    checkpoint = await runtime.get_checkpoint(result.run_id)
    assert checkpoint.transcript[:len(history)] == original


@pytest.mark.parametrize("unit", [" 7", " a", "x=1;\n", "信息 7 x=1;\n", '\x00\\"\n字🙂'])
def test_budgeted_source_pieces_reconstruct_ascii_mixed_and_escaped_input(unit: str) -> None:
    text = unit * 80
    pieces = _split_compaction_source(text, max_tokens=128)
    assert "".join(pieces) == text
    assert pieces and all(piece for piece in pieces)
    assert all(estimate_tokens(piece) <= 128 for piece in pieces)


@pytest.mark.parametrize("unit", [" 7", "信息 7 x=1;\n"])
def test_unfit_protected_first_message_rejects_instead_of_trimming(unit: str) -> None:
    history = [{"role": "user", "content": unit * 5000}]
    original = copy.deepcopy(history)
    with pytest.raises(ContextBudgetError, match="首条目标"):
        compact_transcript(
            history, context_window_tokens=4096, max_output_tokens=512,
            compaction_threshold_tokens=3000, keep_recent_tokens=1000,
            overhead_tokens=1024, semantic_summary="[must not hide an oversized protected message]",
        )
    assert history == original


def test_tiny_text_still_reserves_protocol_and_output_budget() -> None:
    history = [{"role": "user", "content": "a"}]
    projected, metadata = compact_transcript(
        history, context_window_tokens=128, max_output_tokens=32,
        compaction_threshold_tokens=64, keep_recent_tokens=32, overhead_tokens=32,
    )
    assert projected == history
    assert metadata["token_estimate_kind"] == "serialized_utf8_byte_upper_bound"
    with pytest.raises(ContextBudgetError, match="已占用"):
        compact_transcript(
            history, context_window_tokens=128, max_output_tokens=32,
            compaction_threshold_tokens=64, keep_recent_tokens=32, overhead_tokens=97,
        )
