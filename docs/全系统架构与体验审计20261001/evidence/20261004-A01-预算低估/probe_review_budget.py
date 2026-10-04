"""Measure only local synthetic review fixtures; no API, database or network."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
from pathlib import Path

from pytest import MonkeyPatch

from app.agents.context_budget import serialized_chat_input_bytes
from app.agents.contracts import compose_system_prompt
from app.services import review_service as review


def main() -> None:
    evidence = Path(__file__).resolve().parent
    project = evidence.parents[3]
    fixture = project / "backend/tests/unit/services/test_review_non_code_compaction.py"
    spec = importlib.util.spec_from_file_location("local_review_budget_fixture", fixture)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    prepare = review._prepare_bounded_single_agent_prompts
    summary = review._review_context_summary
    reports = []
    for window, experience, context, should_fit in (
        (32768, 5000, 6000, False), (40000, 5000, 6000, True),
        (65536, 5000, 6000, True), (32768, 1200, 1400, True),
    ):
        report = {"window": window, "expected_fit": should_fit, "sections": []}

        def measure_prepare(*args, **kwargs):
            bound = inspect.signature(prepare).bind(*args, **kwargs).arguments
            mandatory_system, mandatory_user = review._render_single_agent_prompts(
                bound["profile"], bound["code"], bound["language"], bound["file_name"],
                bound["rules"], bound["line_offset"],
                {"custom": "", "agent": "(代理上下文已按来源压缩)",
                 "experience": "", "context": "(符号上下文已按来源压缩)"},
            )
            mandatory_bytes = serialized_chat_input_bytes(
                mandatory_user, compose_system_prompt("code_reviewer", mandatory_system),
            )
            output = bound["output_budget"]
            available = window - mandatory_bytes - output - 1024
            report.update(mandatory_serialized_input_bytes=mandatory_bytes,
                          reserved_output_tokens=output, framing_reserve=1024,
                          available_optional_budget_bytes=available,
                          former_divide_by_four_budget=max(512, available // 4))
            try:
                result = prepare(*args, **kwargs)
            except ValueError as error:
                report["explicit_rejection"] = str(error)
                raise
            report["assembled_final_input_bytes"] = serialized_chat_input_bytes(
                result[1], compose_system_prompt("code_reviewer", result[0]),
            )
            return result

        def measure_summary(agent, **kwargs):
            original = kwargs["original"]
            item = {"name": kwargs["source_name"], "original_budget_bytes": review.estimate_tokens(original),
                    "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
                    "quota_budget_bytes": kwargs["target_tokens"], "shared_calls_before": kwargs["calls"][0]}
            report["sections"].append(item)
            try:
                result = summary(agent, **kwargs)
            finally:
                item["shared_calls_after"] = kwargs["calls"][0]
            item["result_budget_bytes"] = review.estimate_tokens(result)
            return result

        review._prepare_bounded_single_agent_prompts = measure_prepare
        review._review_context_summary = measure_summary
        monkeypatch = MonkeyPatch()
        try:
            module.test_default_sequential_review_prepares_source_checked_bounded_prompt(
                monkeypatch, window, experience, context, should_fit,
            )
            report["local_fixture_assertions_passed"] = True
        finally:
            monkeypatch.undo()
            review._prepare_bounded_single_agent_prompts = prepare
            review._review_context_summary = summary
        reports.append(report)
    result = {
        "scope": "Synthetic fixtures and stub compactor; not provider usage or semantic evaluation",
        "production_or_supplier_calls": 0,
        "review_service_sha256": hashlib.sha256(Path(review.__file__).read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "cases": reports,
    }
    (evidence / "审查配额与固定契约-本地实参.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps({"cases": len(reports), "local_assertions_passed": True}))


if __name__ == "__main__":
    main()
