"""任务级「已记录的片段处理数」必须来自逐文件回执合计。

生产任务 #231 的形态：48 个文件的逐文件回执合计 54/54 个片段，而 coverage 顶层的
completed_chunks / total_chunks 随审查进度被逐文件覆盖，最终只剩最后一个文件的 1/1。
报告把它和全任务的文件数 48/48 并列展示，造成粒度不一致的误导。这些用例锁定：
任务级数字只来自逐文件回执；缺回执的文件显示「未知」而不是 0 或被忽略。
"""
from __future__ import annotations

import pytest

from app.services.chunk_receipts import summarize_chunk_receipts, with_chunk_receipts


def _receipt(name: str, total: int, *, completed: int | None = None, status: str = "complete",
             failed: list | int | None = None) -> dict:
    done = total if completed is None else completed
    entry = {
        "file_name": name,
        "status": status,
        "total_chunks": total,
        "completed_chunks": done,
        "completed_chunk_indexes": list(range(done)),
        "failed_chunks": failed if failed is not None else [],
    }
    return entry


def _ledger_231() -> dict:
    """48 个文件：42 个文件 1 片，6 个文件 2 片，合计 54 片，且全部完成。"""
    ledger = {}
    for index in range(48):
        total = 2 if index < 6 else 1
        ledger[str(1000 + index)] = _receipt(f"src/file_{index:02d}.php", total)
    return ledger


def _coverage_231() -> dict:
    return {
        "stage": "complete",
        "coverage_status": "complete",
        "completed_files": 48,
        "total_files": 48,
        # 顶层计数只对应最后处理的文件，这是 #231 页面上出现「1/1」的来源。
        "completed_chunks": 1,
        "total_chunks": 1,
        "current_file": None,
        "files": _ledger_231(),
    }


def test_task_level_chunks_are_the_sum_of_per_file_receipts():
    summary = summarize_chunk_receipts(_coverage_231(), total_files=48, review_type="standard")

    assert summary is not None
    assert summary["known_total_chunks"] == 54
    assert summary["known_completed_chunks"] == 54
    assert summary["known_failed_chunks"] == 0
    assert summary["total_files"] == 48
    assert summary["files_with_receipt"] == 48
    assert summary["files_missing_receipt"] == 0
    assert summary["receipts_complete"] is True
    assert summary["missing_receipt_files"] == []
    assert "已记录的片段处理数 54/54" in summary["label"]
    assert "48 个文件均有回执" in summary["label"]


def test_last_file_top_level_counters_never_leak_into_the_task_level_number():
    summary = summarize_chunk_receipts(_coverage_231(), total_files=48, review_type="standard")

    assert summary["known_total_chunks"] != 1
    assert "1/1" not in summary["label"]


def test_files_without_receipt_are_unknown_not_zero_and_not_ignored():
    coverage = _coverage_231()
    files = coverage["files"]
    # 在分片前就失败的文件只有 status/error，没有任何片段计数。
    files["1047"] = {"file_name": "src/file_47.php", "status": "failed", "error": "模型调用失败"}
    # 另一个文件根本没有被处理，只出现在 requested_files 中。
    files.pop("1046")
    coverage["requested_files"] = [entry["file_name"] for entry in _ledger_231().values()]

    summary = summarize_chunk_receipts(coverage, total_files=48, review_type="standard")

    assert summary["files_with_receipt"] == 46
    assert summary["files_missing_receipt"] == 2
    assert summary["receipts_complete"] is False
    assert sorted(summary["missing_receipt_files"]) == ["src/file_46.php", "src/file_47.php"]
    # 已知合计只含有回执的文件：46 个文件里 6 个 2 片 + 40 个 1 片 = 52。
    assert summary["known_total_chunks"] == 52
    assert summary["known_completed_chunks"] == 52
    assert "已记录的片段处理数 52/52" in summary["label"]
    assert "另有 2 个文件缺少回执" in summary["label"]
    assert "片段数未知" in summary["label"]


def test_missing_receipt_count_uses_task_total_when_names_are_not_recorded():
    coverage = {"files": {"1": _receipt("a.py", 3), "2": _receipt("b.py", 2)}}

    summary = summarize_chunk_receipts(coverage, total_files=5, review_type="full")

    assert summary["files_with_receipt"] == 2
    assert summary["files_missing_receipt"] == 3
    assert summary["known_total_chunks"] == 5
    assert summary["missing_receipt_files"] == []


def test_task_without_any_ledger_reports_unknown_instead_of_zero():
    for coverage in (None, {}, {"stage": "complete", "completed_chunks": 3, "total_chunks": 9}):
        summary = summarize_chunk_receipts(coverage, total_files=12, review_type="standard")

        assert summary is not None
        assert summary["known_total_chunks"] is None
        assert summary["known_completed_chunks"] is None
        assert summary["files_with_receipt"] == 0
        assert summary["files_missing_receipt"] == 12
        assert "未知" in summary["label"]
        assert "0/0" not in summary["label"]
        # 历史任务只有顶层计数时同样不得拿来冒充任务级数字。
        assert "3/9" not in summary["label"]


def test_binary_files_skipped_by_design_are_not_counted_as_missing_receipts():
    coverage = {"files": {
        "1": _receipt("a.py", 2),
        "2": {"file_name": "logo.png", "status": "skipped", "reason": "binary_file"},
    }}

    summary = summarize_chunk_receipts(coverage, total_files=2, review_type="standard")

    assert summary["files_missing_receipt"] == 0
    assert summary["files_not_applicable"] == 1
    assert summary["files_with_receipt"] == 1
    assert summary["known_total_chunks"] == 2
    assert "1 个二进制文件不分片" in summary["label"]


@pytest.mark.parametrize("bad", [None, True, -1, "3", 2.5])
def test_malformed_counters_make_the_file_a_missing_receipt(bad):
    coverage = {"files": {
        "1": _receipt("good.py", 2),
        "2": {"file_name": "bad.py", "status": "failed", "total_chunks": bad, "completed_chunks": bad},
    }}

    summary = summarize_chunk_receipts(coverage, total_files=2, review_type="standard")

    assert summary["files_with_receipt"] == 1
    assert summary["files_missing_receipt"] == 1
    assert summary["missing_receipt_files"] == ["bad.py"]
    assert summary["known_total_chunks"] == 2


def test_failed_chunks_stay_visible_in_the_aggregate():
    coverage = {"files": {
        "1": _receipt("a.py", 3, completed=2, status="failed", failed=[{"index": 2, "error": "超时"}]),
        "2": _receipt("b.py", 1, failed=0),
    }}

    summary = summarize_chunk_receipts(coverage, total_files=2, review_type="standard")

    assert summary["known_total_chunks"] == 4
    assert summary["known_completed_chunks"] == 3
    assert summary["known_failed_chunks"] == 1
    assert "已记录的片段处理数 3/4" in summary["label"]
    assert "其中 1 个片段失败" in summary["label"]


@pytest.mark.parametrize("review_type", ["discuss", "pentest", "sandbox_test", "security_scan", "fullchain_audit"])
def test_review_types_without_a_file_chunk_ledger_have_no_summary(review_type):
    coverage = {"completed_chunks": 3, "total_chunks": 9, "files": {"1": _receipt("a.py", 3)}}

    assert summarize_chunk_receipts(coverage, total_files=1, review_type=review_type) is None
    assert with_chunk_receipts(coverage, total_files=1, review_type=review_type) == coverage


def test_label_does_not_claim_review_quality_or_semantic_coverage():
    label = summarize_chunk_receipts(_coverage_231(), total_files=48, review_type="standard")["label"]

    assert label.startswith("已记录的片段处理数")
    for overstated in ("覆盖率", "全部审查", "逐片审查", "已审完", "完整审查"):
        assert overstated not in label


def test_with_chunk_receipts_replaces_ambiguous_counters_without_mutating_input():
    original = _coverage_231()
    snapshot = {key: (dict(value) if isinstance(value, dict) else value) for key, value in original.items()}

    shaped = with_chunk_receipts(original, total_files=48, review_type="standard", status="success")

    assert original == snapshot, "不得原地修改任务持久化的 coverage"
    assert "completed_chunks" not in shaped and "total_chunks" not in shaped
    assert "current_file_chunks" not in shaped
    assert shaped["chunk_receipts"]["known_total_chunks"] == 54
    # 其余覆盖事实原样保留，逐文件回执也仍然可读。
    assert shaped["completed_files"] == 48
    assert shaped["files"] == original["files"]


def test_with_chunk_receipts_keeps_live_progress_of_the_current_file_only_while_running():
    running = {
        "stage": "analyzing", "current_file": "worker.ts", "completed_files": 2, "total_files": 8,
        "completed_chunks": 3, "total_chunks": 11,
        "files": {"1": _receipt("done.ts", 2), "2": _receipt("worker.ts", 11, completed=3, status="running")},
    }

    live = with_chunk_receipts(running, total_files=8, review_type="full", status="running")
    finished = with_chunk_receipts(running, total_files=8, review_type="full", status="success")

    assert live["current_file_chunks"] == {"completed": 3, "total": 11}
    assert "completed_chunks" not in live and "total_chunks" not in live
    assert "current_file_chunks" not in finished
    assert "completed_chunks" not in finished and "total_chunks" not in finished


def test_running_task_describes_files_without_receipt_as_not_started_instead_of_missing():
    coverage = {"stage": "analyzing", "files": {"1": _receipt("a.py", 2), "2": _receipt("b.py", 1)}}

    summary = summarize_chunk_receipts(coverage, total_files=5, review_type="standard", status="running")

    assert summary["known_total_chunks"] == 3
    assert "进行中" in summary["label"]
    assert "尚有 3 个文件未产生回执" in summary["label"]
    assert "缺少回执" not in summary["label"]


def test_with_chunk_receipts_passes_through_missing_coverage():
    assert with_chunk_receipts(None, total_files=3, review_type="standard") is None
