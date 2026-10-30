"""任务级「已记录的片段处理数」：只由逐文件回执汇总得到。

背景（生产任务 #231）：审查按文件逐个处理，每个文件在 ``coverage["files"][文件ID]`` 留下一份
回执（``total_chunks`` / ``completed_chunks`` / ``failed_chunks``）。``coverage`` 顶层的
``completed_chunks`` / ``total_chunks`` 只是进度条的临时槽位：每开始处理一个新文件就被重置并覆盖，
结束后只对应最后一个文件。把它与全任务的文件数并列展示（“文件处理 48/48；代码片段处理 1/1”）
会让读者误以为整个任务只有 1 个片段，而同一任务 48 份逐文件回执合计是 54/54。

本模块给出报告详情、任务详情和全部导出共用的唯一口径：

* 任务级片段数 = 各文件回执之和；顶层计数不参与任何任务级数字；
* 缺少有效回执的文件既不计 0，也不被忽略：单独计数、列出文件名，并在文案里写明“片段数未知”；
* 文案只说“已记录的片段处理数”。逐文件台账不能单独证明模型实际逐片审查的质量，也不是语义覆盖率。

本模块只在读取时派生，不修改任务持久化的 ``coverage``，也不要求数据库迁移。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CHUNK_RECEIPT_VERSION = "chunk-receipts-v1"
# 只有标准审查流水线（review_service._review_one_file）会写逐文件片段回执。
CHUNK_LEDGER_REVIEW_TYPES = frozenset({"quick", "standard", "security", "performance", "full"})
ACTIVE_TASK_STATUSES = frozenset({"pending", "queued", "running"})
CHUNK_RECEIPT_NOTE = "仅为逐文件处理回执的合计，不单独证明模型逐片审查的质量，也不是语义覆盖率"
_MAX_LISTED_NAMES = 20
_AMBIGUOUS_TOP_LEVEL_KEYS = ("completed_chunks", "total_chunks")


def _count(value: Any) -> int | None:
    """严格读取非负整数计数；布尔、负数、字符串和小数都视为没有记录。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def chunk_receipts_apply(review_type: str | None) -> bool:
    """该审查类型是否带逐文件片段回执；类型未知时按标准审查处理。"""
    return review_type is None or str(review_type) in CHUNK_LEDGER_REVIEW_TYPES


def _file_receipt(entry: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """读取单个文件的 (总片段, 已完成片段, 失败片段)；计数缺失或自相矛盾时没有有效回执。"""
    total = _count(entry.get("total_chunks"))
    completed = _count(entry.get("completed_chunks"))
    if total is None or completed is None or completed > total:
        return None
    failed_raw = entry.get("failed_chunks")
    if isinstance(failed_raw, list):
        failed = len(failed_raw)
    else:
        failed = _count(failed_raw) or 0
    return total, completed, failed


def _is_binary_skip(entry: Mapping[str, Any]) -> bool:
    return entry.get("status") == "skipped" and entry.get("reason") == "binary_file"


def _entry_name(file_id: Any, entry: Mapping[str, Any]) -> str:
    return str(entry.get("file_name") or file_id)


def _label(
    *, known_completed: int | None, known_total: int | None, known_failed: int | None,
    with_receipt: int, missing: int, not_applicable: int, total_files: int | None, active: bool,
) -> str:
    if known_total is None or known_completed is None:
        scope = f"共 {total_files} 个文件" if total_files is not None else ""
        if active:
            return "片段处理数未知：尚无逐文件回执（任务进行中" + (f"，{scope}" if scope else "") + "）"
        return "片段处理数未知：没有逐文件回执" + (f"（{scope}）" if scope else "")
    notes: list[str] = []
    if active:
        notes.append("进行中")
    if with_receipt:
        notes.append(f"{with_receipt} 个文件均有回执" if not missing else f"{with_receipt} 个文件有回执")
    if missing:
        notes.append(
            f"尚有 {missing} 个文件未产生回执" if active else f"另有 {missing} 个文件缺少回执，片段数未知"
        )
    if not_applicable:
        notes.append(f"{not_applicable} 个二进制文件不分片")
    if known_failed:
        notes.append(f"其中 {known_failed} 个片段失败")
    head = f"已记录的片段处理数 {known_completed}/{known_total}"
    return head + (f"（{'；'.join(notes)}）" if notes else "")


def summarize_chunk_receipts(
    coverage: Mapping[str, Any] | None,
    *,
    total_files: Any = None,
    review_type: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    """由逐文件回执汇总任务级片段数。

    Args:
        coverage: 任务持久化的 ``coverage``；只读取其中的 ``files`` 逐文件台账与
            ``requested_files`` 清单，顶层 ``completed_chunks`` / ``total_chunks`` 被刻意忽略。
        total_files: 任务送审文件总数；用来推算没有台账记录的文件数量。
        review_type: 审查类型；没有逐文件片段回执的类型（圆桌、沙箱、渗透、安全审计）返回 ``None``。
        status: 任务状态；进行中的任务把尚无回执的文件描述为“未产生回执”而不是“缺少回执”。

    Returns:
        汇总字典，``known_*`` 只含有效回执的文件，``files_missing_receipt`` 单列缺回执的文件数；
        没有任何有效回执时 ``known_*`` 为 ``None``（未知），不会是 0。
    """
    if not chunk_receipts_apply(review_type):
        return None
    source = coverage if isinstance(coverage, Mapping) else {}
    ledger = source.get("files") if isinstance(source.get("files"), Mapping) else {}
    declared_total = _count(total_files)
    if declared_total is None:
        declared_total = _count(source.get("total_files"))

    known_total = known_completed = known_failed = 0
    with_receipt = not_applicable = 0
    missing_names: list[str] = []
    ledger_names: set[str] = set()
    for file_id, entry in ledger.items():
        if not isinstance(entry, Mapping):
            continue
        name = _entry_name(file_id, entry)
        ledger_names.add(name)
        receipt = _file_receipt(entry)
        if receipt is not None:
            with_receipt += 1
            known_total += receipt[0]
            known_completed += receipt[1]
            known_failed += receipt[2]
        elif _is_binary_skip(entry):
            not_applicable += 1
        else:
            missing_names.append(name)
    requested = source.get("requested_files")
    if isinstance(requested, list):
        for raw in requested:
            name = str(raw)
            if name not in ledger_names and name not in missing_names:
                missing_names.append(name)

    if declared_total is not None:
        missing = max(declared_total - with_receipt - not_applicable, len(missing_names), 0)
    else:
        missing = len(missing_names)
    has_known = (with_receipt + not_applicable) > 0
    active = str(status or "") in ACTIVE_TASK_STATUSES
    summary: dict[str, Any] = {
        "version": CHUNK_RECEIPT_VERSION,
        "basis": "per_file_receipts",
        "total_files": declared_total,
        "files_with_receipt": with_receipt,
        "files_missing_receipt": missing,
        "files_not_applicable": not_applicable,
        "missing_receipt_files": missing_names[:_MAX_LISTED_NAMES],
        "missing_receipt_files_omitted": max(0, len(missing_names) - _MAX_LISTED_NAMES),
        "known_total_chunks": known_total if has_known else None,
        "known_completed_chunks": known_completed if has_known else None,
        "known_failed_chunks": known_failed if has_known else None,
        "receipts_complete": missing == 0 and with_receipt > 0,
        "in_progress": active,
        "note": CHUNK_RECEIPT_NOTE,
    }
    summary["label"] = _label(
        known_completed=summary["known_completed_chunks"], known_total=summary["known_total_chunks"],
        known_failed=summary["known_failed_chunks"], with_receipt=with_receipt, missing=missing,
        not_applicable=not_applicable, total_files=declared_total, active=active,
    )
    return summary


def _current_file_chunks(coverage: Mapping[str, Any]) -> dict[str, int | None] | None:
    completed = _count(coverage.get("completed_chunks"))
    total = _count(coverage.get("total_chunks"))
    existing = coverage.get("current_file_chunks")
    if completed is None and total is None and isinstance(existing, Mapping):
        completed, total = _count(existing.get("completed")), _count(existing.get("total"))
    if completed is None and total is None:
        return None
    return {"completed": completed, "total": total}


def with_chunk_receipts(
    coverage: Mapping[str, Any] | None,
    *,
    total_files: Any = None,
    review_type: str | None = None,
    status: str | None = None,
) -> Any:
    """返回带 ``chunk_receipts`` 汇总的 coverage 副本，供所有读取出口共用。

    顶层 ``completed_chunks`` / ``total_chunks`` 只对应当前或最后一个文件，永远不会作为
    任务级数字输出：进行中的任务把它们改名为 ``current_file_chunks`` 供进度面板使用，
    终态任务直接移除。入参不会被修改；没有 coverage 或类型不带逐文件回执时原样返回。
    """
    if not isinstance(coverage, Mapping) or not coverage or not chunk_receipts_apply(review_type):
        return coverage
    if isinstance(coverage.get("chunk_receipts"), Mapping) and not isinstance(coverage.get("files"), Mapping):
        # 已是整形后的响应且没有台账可重算，保持其汇总，避免用“未知”覆盖真实合计。
        return coverage
    summary = summarize_chunk_receipts(
        coverage, total_files=total_files, review_type=review_type, status=status,
    )
    shaped = {
        key: value for key, value in coverage.items()
        if key not in (*_AMBIGUOUS_TOP_LEVEL_KEYS, "chunk_receipts", "current_file_chunks")
    }
    shaped["chunk_receipts"] = summary
    if summary is not None and summary["in_progress"]:
        current = _current_file_chunks(coverage)
        if current is not None:
            shaped["current_file_chunks"] = current
    return shaped
