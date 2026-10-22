"""Oversized team projections must preserve the business summary for consumers."""
from __future__ import annotations

import json

from app.services.agent_team_projection import project_team_result


def test_oversized_team_result_keeps_bounded_summary_at_consumer_path() -> None:
    summary = "团队最终整理结论：逐项核验成员结果并保留关键证据。" * 24
    source = {
        "summary": summary,
        "findings": [
            {"title": f"finding-{index}", "evidence": "静态证据" * 100}
            for index in range(40)
        ],
    }
    original = json.dumps(source, ensure_ascii=False)

    projected = project_team_result(source, team_id=374, task_id=9001, budget=8000)

    assert projected["complete"] is False
    assert projected["summary"].startswith("团队最终整理结论：")
    assert projected["summary"] != "结果超过单次上下文预算；此处为结构摘要，完整脱敏结果仍可按引用读取。"
    assert projected["summary"] == summary
    assert projected["projection_summary"].startswith("结果超过单次上下文预算；")
    assert projected["summary_truncated"] is False
    assert projected["result_reference"]["team_id"] == 374
    assert projected["result_reference"]["task_id"] == 9001
    assert len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()) <= 8000
    assert json.dumps(source, ensure_ascii=False) == original


def test_oversized_business_summary_is_truncated_to_projection_budget() -> None:
    summary = "业务结论：" + "关键证据逐项核验并保留可追溯引用。" * 1000
    source = {"summary": summary, "findings": [{"evidence": "x" * 12000}]}

    projected = project_team_result(source, team_id=374, task_id=9002, budget=8000)

    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode()
    assert projected["summary"].startswith("业务结论：")
    assert projected["summary"].endswith("…")
    assert projected["summary_truncated"] is True
    assert projected["projection_summary"].startswith("结果超过单次上下文预算；")
    assert len(encoded) <= 8000


def test_team_summary_reference_does_not_claim_a_member_result_path():
    source = {"completed_tasks": ["task"] * 400, "final_result": {"summary": "已核实" * 500}}
    projected = project_team_result(source, team_id=383, result_scope="team_summary", budget=1024)
    assert projected["result_reference"] == {
        "tool": "get_agent_team", "team_id": 383, "result_scope": "team_summary",
        "result_path": "", "offset": 0, "limit": 20,
    }


def test_small_result_keeps_original_shape():
    source = {"status": "failed", "summary": "权限不足", "errors": [{"code": "forbidden"}]}
    assert project_team_result(source, team_id=383, task_id=879) == source
