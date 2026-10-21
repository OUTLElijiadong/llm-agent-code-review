"""关闭或不可用的通用日志源不得触发陈旧 CSV 全表扫描。"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("db_log_source_executor", Path(__file__).resolve().parents[1] / "prism_ops_executor.py")
assert SPEC and SPEC.loader
executor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(executor)


@pytest.mark.parametrize(("status", "reason_code", "enabled"), [
    ("0\tTABLE\n", "general_log_disabled", False),
    ("0\tFILE,TABLE\n", "general_log_disabled", False),
    ("1\tFILE\n", "general_log_table_output_disabled", True),
    ("1\tNONE\n", "general_log_table_output_disabled", True),
    ("1\tTABLE,NONE\n", "general_log_table_output_disabled", True),
])
def test_unavailable_source_never_scans_historical_log(monkeypatch, status, reason_code, enabled):
    calls = []

    def run(args, **kwargs):
        calls.append(args[-1])
        assert "mysql.general_log" not in args[-1], "源不可用时不能读取历史表"
        return {"exit_code": 0, "stdout": status, "stderr": ""}

    monkeypatch.setattr(executor, "run", run)
    result = executor._db_threat_signals({"limit": 1})
    assert len(calls) == 1
    assert "@@GLOBAL.general_log" in calls[0]
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["reason_code"] == reason_code
    assert result["source_logging_enabled"] is enabled
    assert result["history_not_scanned"] is True
    assert result["source_timed_out"] is False
    assert "destructive_total" not in result
    assert "sampled_rows" not in result


@pytest.mark.parametrize("status", ["", "yes\tTABLE", "1\tUNKNOWN", "1\tTABLE,TABLE", "1\tTABLE\n0\tTABLE"])
def test_invalid_source_metadata_fails_closed_without_sampling(monkeypatch, status):
    calls = []
    monkeypatch.setattr(executor, "run", lambda args, **kw: calls.append(args[-1]) or {
        "exit_code": 0, "stdout": status, "stderr": "",
    })
    result = executor._db_threat_signals({"limit": 1})
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["reason_code"] == "general_log_source_invalid"
    assert len(calls) == 1
    assert "destructive_total" not in result


@pytest.mark.parametrize("output", ["TABLE", "FILE,TABLE", "TABLE,FILE"])
def test_available_table_source_is_sampled_with_existing_bounds(monkeypatch, output):
    calls = []

    def run(args, **kwargs):
        calls.append((args[-1], kwargs))
        text = f"1\t{output}\n" if len(calls) == 1 else "726f6f74\t44524f50205441424c452078\t2026-10-22 01:00:00\t0\n"
        return {"exit_code": 0, "stdout": text, "stderr": ""}

    monkeypatch.setattr(executor, "run", run)
    result = executor._db_threat_signals({"limit": 1})
    assert len(calls) == 2
    assert "@@GLOBAL.general_log" in calls[0][0]
    assert "MAX_EXECUTION_TIME(5000)" in calls[1][0]
    assert calls[1][0].endswith("LIMIT 2")
    assert 0 < calls[1][1]["timeout"] <= 20
    assert result["ok"] is True
    assert result["sampled_rows"] == 1
    assert result["destructive_total"] == 1
    assert result["source_logging_enabled"] is True
    assert result["source_log_output"] == output


def test_source_query_error_does_not_publish_empty_statistics(monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "run", lambda args, **kw: calls.append(args[-1]) or {
        "exit_code": 1, "stdout": "private partial data", "stderr": "metadata unavailable",
    })
    result = executor._db_threat_signals({"limit": 1})
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["reason_code"] == "general_log_source_query_failed"
    assert len(calls) == 1
    assert "private" not in str(result)
    assert "destructive_total" not in result


def test_source_query_timeout_preserves_unknown_termination(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"private data")

    monkeypatch.setattr(executor, "run", run)
    result = executor._db_threat_signals({"limit": 1})
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["source_timed_out"] is True
    assert result["termination_confirmed"] is False
    assert "private" not in str(result)


def test_source_probe_and_sampling_share_host_time_budget(monkeypatch):
    ticks = iter([100.0, 104.0])
    monkeypatch.setattr(executor.time, "monotonic", lambda: next(ticks))
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs["timeout"])
        return {"exit_code": 0, "stdout": "1\tTABLE\n" if len(calls) == 1 else "", "stderr": ""}

    monkeypatch.setattr(executor, "run", run)
    result = executor._db_threat_signals({"limit": 1})
    assert result["ok"] is True
    assert calls == [20, 16]


def test_exhausted_source_probe_budget_never_starts_sample(monkeypatch):
    ticks = iter([100.0, 121.0])
    monkeypatch.setattr(executor.time, "monotonic", lambda: next(ticks))
    calls = []
    monkeypatch.setattr(executor, "run", lambda args, **kw: calls.append(args[-1]) or {
        "exit_code": 0, "stdout": "1\tTABLE\n", "stderr": "",
    })
    result = executor._db_threat_signals({"limit": 1})
    assert len(calls) == 1
    assert result["ok"] is False
    assert result["degraded"] is True
    assert result["source_timed_out"] is True
    assert result["termination_confirmed"] is False
    assert "sampled_rows" not in result
