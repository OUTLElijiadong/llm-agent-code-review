"""沙箱源码分片上下文：逐片提炼并核验来源覆盖。"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

MAX_SOURCE_CHUNKS = 32
MAX_CHUNK_SUMMARY_CHARS = 600


class SourceContextError(ValueError):
    """源码上下文未完整覆盖，禁止把局部材料当完整项目。"""


def _context_call(agent: Any, source_ids: list[str], payload: Any, ctx: Any, deadline: float | None) -> str:
    if deadline is not None and time.monotonic() >= deadline:
        raise SourceContextError("源码上下文压缩超过时间预算")
    message = (
        "请压缩以下沙箱源码材料，保留入口、依赖、模块、调用和安全相关事实。"
        "不得臆测，不能把未出现的符号说成已存在。"
        "必须返回 JSON: {\"covered_source_ids\":[全部输入 source_id],\"summary\":\"...\"}；"
        "summary 长度不超过 600 字。若任一片看不清，返回 error 字段。\n"
        f"本批来源 ID: {json.dumps(source_ids, ensure_ascii=False)}\n"
        f"原始材料:\n{json.dumps(payload, ensure_ascii=False, default=str)}"
    )
    try:
        result = agent.call_json(message, ctx=ctx, max_tokens=2_048, deadline_monotonic=deadline)
    except Exception as exc:  # noqa: BLE001 - 模型异常必须暴露为未完成压缩。
        raise SourceContextError(f"源码分片压缩调用异常: {str(exc)[:160]}") from exc
    if not getattr(result, "success", False) or not isinstance(getattr(result, "data", None), dict):
        raise SourceContextError(f"源码分片压缩失败: {str(getattr(result, 'error', '无有效 JSON'))[:160]}")
    data = result.data
    if data.get("error"):
        raise SourceContextError(f"源码分片无法确认: {str(data['error'])[:160]}")
    covered = data.get("covered_source_ids")
    summary = data.get("summary")
    if not isinstance(covered, list) or covered != source_ids:
        raise SourceContextError("源码分片压缩覆盖 ID 不完整或顺序错误")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_CHUNK_SUMMARY_CHARS:
        raise SourceContextError("源码分片摘要为空或超长")
    return summary.strip()


def compact_source_context(
    agent: Any,
    source_summary: dict[str, Any],
    *,
    ctx: Any,
    deadline: float | None = None,
    max_chars: int = 10_000,
) -> dict[str, Any]:
    """按源文本哈希和序号压缩全量分片；失败时不交给决策模型。"""
    if source_summary.get("coverage_complete") is not True:
        raise SourceContextError(str(source_summary.get("coverage_error") or "源码覆盖不完整"))
    chunks = source_summary.get("source_chunks")
    if not isinstance(chunks, list) or not chunks:
        raise SourceContextError("源码分片为空")
    expected_count = source_summary.get("source_chunk_count")
    if expected_count is not None and expected_count != len(chunks):
        raise SourceContextError("源码分片数量与覆盖记录不一致")
    if len(chunks) > MAX_SOURCE_CHUNKS:
        raise SourceContextError(f"源码共 {len(chunks)} 片，超过单轮压缩上限 {MAX_SOURCE_CHUNKS}")
    expected_ids: list[str] = []
    compressed: list[dict[str, str]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise SourceContextError("源码分片结构无效")
        source_id = chunk.get("source_id")
        content = chunk.get("text")
        digest = chunk.get("sha256")
        if (
            not isinstance(source_id, str) or not source_id
            or not isinstance(content, str) or not content
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest
            or not source_id.endswith(str(digest)[:12])
            or source_id in expected_ids
        ):
            raise SourceContextError("源码分片哈希或来源 ID 无效")
        expected_ids.append(source_id)
        compressed.append({
            "source_id": source_id,
            "summary": _context_call(agent, [source_id], chunk, ctx, deadline),
        })
    compacted: dict[str, Any] = {
        "language": source_summary.get("language"),
        "source_archive_sha256": source_summary.get("source_archive_sha256"),
        "source_file_count": source_summary.get("source_file_count"),
        "source_text_file_count": source_summary.get("source_text_file_count"),
        "source_binary_file_count": source_summary.get("source_binary_file_count"),
        "source_text_bytes": source_summary.get("source_text_bytes"),
        "source_manifest_sha256": source_summary.get("source_manifest_sha256"),
        "covered_source_ids": expected_ids,
        "source_summaries": compressed,
    }
    if len(json.dumps(compacted, ensure_ascii=False)) > max_chars:
        # 第二层再压缩全部摘要；覆盖 ID 必须与第一层严格一致。
        compacted["source_summaries"] = [{
            "source_id": "all-source-summaries",
            "summary": _context_call(agent, expected_ids, compressed, ctx, deadline),
        }]
    if len(json.dumps(compacted, ensure_ascii=False)) > max_chars:
        raise SourceContextError("压缩后源码上下文仍超过模型预算")
    return compacted
