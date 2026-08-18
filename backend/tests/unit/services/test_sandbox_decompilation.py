"""反编译证据进入沙箱结论和报告的回归测试。"""
from __future__ import annotations

from app.services import sandbox_service


def test_decompilation_marker_requires_exactly_one_valid_record() -> None:
    log = (
        'PRISM_DECOMPILATION_JSON {"status":"succeeded","tool":"jadx",'
        '"tool_version":"1.5.6","candidate_count":1,"output_file_count":2,'
        '"input_sha256":"' + "c" * 64 + '","output_sha256":"' + "a" * 64 + '",'
        '"input_artifact_sha256s":["' + "d" * 64 + '"],'
        '"output_size_bytes":123,"exit_code":0,"log_ref":"worker.log",'
        '"artifact_refs":["decompilation-manifest"]}'
    )
    result = sandbox_service._extract_decompilation_result(log)
    assert result is not None
    assert result["status"] == "succeeded"
    assert result["output_file_count"] == 2
    assert len(result["output_sha256"]) == 64
    assert result["input_sha256"] == "c" * 64
    assert result["input_artifact_sha256s"] == ["d" * 64]
    assert result["exit_code"] == 0
    assert result["artifact_refs"] == ["decompilation-manifest"]

    assert sandbox_service._extract_decompilation_result(log + "\n" + log) is None
    assert sandbox_service._extract_decompilation_result(
        'PRISM_DECOMPILATION_JSON {"status":"unknown","tool":"jadx"}'
    ) is None


def test_fact_gate_report_includes_decompilation_evidence() -> None:
    report = sandbox_service._fact_gate_report(
        "## 总体结论\n\n模型结论。\n\n## 风险\n\n无。",
        {
            "passed": True,
            "summary": "白盒测试通过",
            "evidence": {
                "decompilation": {
                    "status": "succeeded",
                    "tool": "jadx",
                    "tool_version": "1.5.6",
                    "input_sha256": "c" * 64,
                    "input_artifact_sha256s": ["d" * 64],
                    "output_sha256": "b" * 64,
                    "exit_code": 0,
                    "log_ref": "worker.log",
                    "artifact_refs": ["decompilation-manifest"],
                }
            },
        },
    )
    assert "## 反编译证据" in report
    assert "`succeeded`" in report
    assert "`1.5.6`" in report
    assert "b" * 64 in report
    assert "输入清单 SHA-256" in report
    assert "原始制品 SHA-256" in report
    assert "d" * 64 in report
    assert "worker.log" in report


def test_decompilation_marker_rejects_invalid_numeric_fields() -> None:
    log = 'PRISM_DECOMPILATION_JSON {"status":"succeeded","candidate_count":"bad"}'
    assert sandbox_service._extract_decompilation_result(log) is None


def test_succeeded_marker_requires_one_raw_hash_per_candidate() -> None:
    base = (
        'PRISM_DECOMPILATION_JSON {"status":"succeeded","tool":"jadx",'
        '"candidate_count":1,"input_sha256":"' + "c" * 64 + '",'
        '"output_sha256":"' + "a" * 64 + '","exit_code":0'
    )
    assert sandbox_service._extract_decompilation_result(base + "}") is None
    assert sandbox_service._extract_decompilation_result(
        base + ',"input_artifact_sha256s":["not-a-hash"]}'
    ) is None
