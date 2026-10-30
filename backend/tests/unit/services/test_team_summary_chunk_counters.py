"""团队汇总的 ``completed_chunks < total_chunks`` 判断与顶层覆盖计数口径的关系。

团队正式审查把 ``ReviewTask.coverage`` 原样放进成员结果（agent_team_review._review_result）。
任务 coverage 的顶层 completed_chunks / total_chunks 在审查过程中会随当前文件被覆盖，
这里验证它们进入团队汇总后的真实行为：

* 完成的任务：顶层计数是最后一个文件的 n/n，``completed < total`` 不会成立，不会误判为部分覆盖；
* 终止在某个文件中途的任务：顶层计数小于总数，且任务 coverage 同时带 ``stage != complete`` /
  ``failed_files`` 等信号，判断本来就成立，结论与逐文件口径一致。
"""
from __future__ import annotations

from app.services.agent_team_summary import (
    _incomplete_coverage,
    dependency_coverage_summary,
)


def _review_member_result(coverage: dict) -> dict:
    """与 agent_team_review._review_result 同形态：ReviewTask.coverage 原样放入成员结果。"""
    return {
        "status": "completed", "task_id": 7, "project_id": 3, "coverage": coverage,
        "evidence": [{"source": "review_task", "data": {"task_id": 7, "coverage": coverage}}],
    }


def _finished_231_coverage() -> dict:
    return {
        "stage": "complete", "coverage_status": "complete", "completed_files": 48, "total_files": 48,
        "completed_chunks": 1, "total_chunks": 1,
        "files": {str(i): {"file_name": f"f{i}.php", "status": "complete", "total_chunks": 2 if i < 6 else 1,
                           "completed_chunks": 2 if i < 6 else 1, "failed_chunks": []} for i in range(48)},
    }


def test_finished_review_is_not_flagged_partial_by_the_last_file_counters():
    projected = dependency_coverage_summary(_review_member_result(_finished_231_coverage()))

    assert projected["completed_chunks"] == 1 and projected["total_chunks"] == 1
    assert _incomplete_coverage(projected) is False


def test_review_stopped_mid_file_is_partial_and_the_chunk_comparison_agrees_with_the_ledger():
    coverage = {
        "stage": "cancelled", "completed_files": 20, "total_files": 48,
        "completed_chunks": 1, "total_chunks": 3,
        "files": {"1": {"file_name": "a.php", "status": "cancelled", "total_chunks": 3, "completed_chunks": 1,
                        "failed_chunks": []}},
    }
    projected = dependency_coverage_summary(_review_member_result(coverage))

    assert projected["completed_chunks"] < projected["total_chunks"]
    assert _incomplete_coverage(projected) is True
    # 同一结论也能由逐文件回执独立得到：未完成的文件在台账里就是 1/3。
    from app.services.chunk_receipts import summarize_chunk_receipts

    receipts = summarize_chunk_receipts(coverage, total_files=48, review_type="full")
    assert receipts["known_completed_chunks"] < receipts["known_total_chunks"]


def test_failed_chunk_in_an_earlier_file_is_not_hidden_by_a_clean_last_file():
    """顶层计数只看最后一个文件；更早文件的失败片段由 failed_files/partial 状态与逐文件回执承担。"""
    coverage = {
        "stage": "complete", "coverage_status": "partial", "completed_files": 47, "total_files": 48,
        "failed_files": ["early.php"], "completed_chunks": 1, "total_chunks": 1,
        "files": {
            "1": {"file_name": "early.php", "status": "failed", "total_chunks": 2, "completed_chunks": 1,
                  "failed_chunks": [{"index": 1, "error": "超时"}]},
            "2": {"file_name": "last.php", "status": "complete", "total_chunks": 1, "completed_chunks": 1,
                  "failed_chunks": []},
        },
    }
    projected = dependency_coverage_summary(_review_member_result(coverage))

    # 顶层计数 1/1 看不出问题，但 coverage_status 已经把它判为部分覆盖。
    assert projected["completed_chunks"] == projected["total_chunks"] == 1
    assert _incomplete_coverage(projected) is True
