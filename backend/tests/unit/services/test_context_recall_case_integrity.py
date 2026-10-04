"""历史召回的大小写与原文字符坐标回归，不调用外部模型。"""

import json

import pytest

from app.services.context_fidelity import retrieve_relevant_user_facts
from app.services.deepseek_responses_runtime import (
    _SEMANTIC_SUMMARY_PREFIX,
    SEMANTIC_SUMMARY_FORMAT_VERSION,
    DeepSeekResponsesRuntime,
    InMemoryCheckpointStore,
    RunCheckpoint,
)


@pytest.mark.parametrize("prefix", ["背景" * 700, "ß" * 900, "İ" * 900], ids=["chinese", "sharp-s", "dotted-i"])
def test_long_history_recall_matches_case_insensitively_with_raw_offsets(prefix: str) -> None:
    fact = "ASIA/TAIPEI REFUND TIMEZONE uses the original transaction month"
    transcript = [{"role": "user", "content": prefix + fact + "补充背景" * 600}]

    retrieved = retrieve_relevant_user_facts(
        transcript, [0], "asia/taipei refund timezone",
    )

    assert len(retrieved) == 1
    index, excerpt = retrieved[0]
    assert index == 0
    assert fact in excerpt
    assert len(excerpt) <= 1_200
    assert excerpt.removeprefix("…").removesuffix("…") in transcript[0]["content"]


def test_earlier_keyword_noise_cannot_move_excerpt_away_from_relevant_fact() -> None:
    fact = "ASIA/TAIPEI REFUND TIMEZONE uses the original transaction month"
    history = "refund " + "背景" * 900 + fact + "补充背景" * 900

    retrieved = retrieve_relevant_user_facts(
        [{"role": "user", "content": history}], [0], "asia/taipei refund timezone",
    )

    assert len(retrieved) == 1
    assert fact in retrieved[0][1]
    assert len(retrieved[0][1]) <= 1_200


def test_widely_scattered_terms_do_not_make_unrelated_middle_excerpt_relevant() -> None:
    history = "refund" + "背景" * 1_000 + "timezone" + "背景" * 1_000 + "transaction"

    assert retrieve_relevant_user_facts(
        [{"role": "user", "content": history}], [0], "refund timezone transaction",
    ) == []


@pytest.mark.parametrize("position", [438, 439, 877, 878, 1_755, 20_000])
def test_relevant_fact_survives_overlapping_window_boundaries(position: int) -> None:
    fact = "ASIA/TAIPEI REFUND TIMEZONE uses the original transaction month"
    history = "ß" * position + fact + "补充背景" * 500

    retrieved = retrieve_relevant_user_facts(
        [{"role": "user", "content": history}], [0], "asia/taipei refund timezone",
    )

    assert fact in retrieved[0][1]
    assert len(retrieved[0][1]) <= 1_200


def test_matching_assistant_text_is_not_promoted_into_user_fact_recall() -> None:
    fact = "ASIA/TAIPEI REFUND TIMEZONE uses the original transaction month"
    transcript = [
        {"role": "assistant", "content": "旧背景" * 700 + fact},
        {"role": "user", "content": "旧背景" * 700 + fact},
    ]

    retrieved = retrieve_relevant_user_facts(transcript, [0, 1], "asia/taipei refund timezone")

    assert [index for index, _ in retrieved] == [1]
    assert fact in retrieved[0][1]


@pytest.mark.asyncio
async def test_cached_summary_retrieves_case_varied_history_for_each_new_query() -> None:
    fact = "ASIA/TAIPEI REFUND TIMEZONE uses the original transaction month"
    source = "旧背景" * 800 + fact + "补充背景" * 800
    digest = "same-long-history"
    store = InMemoryCheckpointStore()
    runtime = DeepSeekResponsesRuntime(
        transport=None, tool_executor=None, checkpoint_store=store,
        model="local-no-model-call",
    )
    checkpoint = RunCheckpoint(
        run_id="cached_case_recall", model="local-no-model-call", tools=[],
        transcript=[{"role": "user", "content": source}, {"role": "user", "content": "继续"}],
        context_metadata={"semantic_summary": {
            "source_sha256": digest,
            "format_version": SEMANTIC_SUMMARY_FORMAT_VERSION,
            "text": _SEMANTIC_SUMMARY_PREFIX + " 权限与审批以当前服务端 RBAC 和审批记录为唯一授权依据。\n旧摘要",
        }},
    )
    await store.create(checkpoint)

    # 复用相同压缩来源，第一次一般追问不能阻止下一次具体英文查询召回。
    first = await runtime._semantic_compact(
        checkpoint, {"summary_sha256": digest, "omitted_indices": [0]}, summary_budget=5_000,
    )
    assert fact not in first
    checkpoint.transcript[-1]["content"] = "asia/taipei refund timezone"
    second = await runtime._semantic_compact(
        checkpoint, {"summary_sha256": digest, "omitted_indices": [0]}, summary_budget=5_000,
    )
    assert fact in second
    assert "[相关历史用户原文摘录 来源#0" in second
    assert json.loads(json.dumps(checkpoint.transcript, ensure_ascii=False))[0]["content"] == source
