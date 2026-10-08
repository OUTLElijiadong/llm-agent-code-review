"""PHP syntax-repair non-retryable model failure regressions."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.agents.syntax_repair_agent import SyntaxRepairAgent
from app.services.ai_usage_context import UsageAccountingError


def test_usage_accounting_exception_stops_queued_repairs_but_waits_for_inflight(monkeypatch) -> None:
    agent = SyntaxRepairAgent()
    first_started = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    queued_started: list[str] = []

    def call_json(message: str, **_kwargs):
        if "目标文件: first.php" in message:
            first_started.set()
            assert second_started.wait(timeout=3)
            raise UsageAccountingError("用量审计事务提交失败；不能重发模型请求")
        if "目标文件: second.php" in message:
            second_started.set()
            assert release_second.wait(timeout=3)
            return SimpleNamespace(success=True, data={"content": "<?php echo 'fixed';"})
        path = next(candidate for candidate in ("third.php", "fourth.php") if f"目标文件: {candidate}" in message)
        queued_started.append(path)
        return SimpleNamespace(success=True, data={"content": "<?php echo 'fixed';"})

    monkeypatch.setattr(agent, "call_json", call_json)
    result_box: list[dict] = []
    repair_thread = threading.Thread(
        target=lambda: result_box.append(
            agent.repair(
                language="php",
                errors=[
                    {"file": path, "line": 1, "message": "unexpected token"}
                    for path in ("first.php", "second.php", "third.php", "fourth.php")
                ],
                files={path: "<?php echo broken;" for path in ("first.php", "second.php", "third.php", "fourth.php")},
            )
        )
    )
    repair_thread.start()
    try:
        assert first_started.wait(timeout=2)
        assert second_started.wait(timeout=2)
        time.sleep(0.1)
        assert queued_started == []
    finally:
        release_second.set()
        repair_thread.join(timeout=3)

    assert not repair_thread.is_alive()
    assert result_box == [
        {
            "error": "用量审计事务提交失败；不能重发模型请求",
            "failure_kind": "usage_accounting_error",
        }
    ]
    assert queued_started == []


@pytest.mark.parametrize("failure_kind", ["billing_error", "usage_accounting_error"])
def test_thrown_nonretryable_failure_kind_stops_queued_repairs(monkeypatch, failure_kind: str) -> None:
    """A provider/adapter may raise a typed RuntimeError instead of returning AgentResult."""
    agent = SyntaxRepairAgent()
    first_started = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    started: list[str] = []
    paths = ("first.php", "second.php", "third.php")

    def call_json(message: str, **_kwargs):
        path = next(name for name in paths if f"目标文件: {name}" in message)
        started.append(path)
        if path == "first.php":
            first_started.set()
            assert second_started.wait(timeout=3)
            error = RuntimeError("provider rejected request")
            error.failure_kind = failure_kind
            raise error
        if path == "second.php":
            second_started.set()
            assert release_second.wait(timeout=3)
            return SimpleNamespace(success=True, data={"content": "<?php echo 'fixed';"})
        pytest.fail("queued file must not start after a non-retryable model failure")

    monkeypatch.setattr(agent, "call_json", call_json)
    result_box: list[dict] = []
    repair_thread = threading.Thread(
        target=lambda: result_box.append(
            agent.repair(
                language="php",
                errors=[{"file": path, "line": 1, "message": "unexpected token"} for path in paths],
                files={path: "<?php echo broken;" for path in paths},
            )
        )
    )
    repair_thread.start()
    try:
        assert first_started.wait(timeout=2)
        assert second_started.wait(timeout=2)
        time.sleep(0.1)
        assert set(started) == {"first.php", "second.php"}
        assert repair_thread.is_alive(), "must wait for already-started provider work"
    finally:
        release_second.set()
        repair_thread.join(timeout=3)

    assert not repair_thread.is_alive()
    assert result_box[0]["failure_kind"] == failure_kind
    assert set(started) == {"first.php", "second.php"}


@pytest.mark.parametrize("failure_kind", ["usage_accounting_error", "billing_error"])
def test_returned_non_retryable_failure_stops_queued_repairs(monkeypatch, failure_kind: str) -> None:
    agent = SyntaxRepairAgent()
    first_started = threading.Event()
    second_failed = threading.Event()
    release_first = threading.Event()
    started: list[str] = []

    def call_json(message: str, **_kwargs):
        candidates = ("first.php", "second.php", "third.php")
        path = next(candidate for candidate in candidates if f"目标文件: {candidate}" in message)
        started.append(path)
        if path == "first.php":
            first_started.set()
            assert release_first.wait(timeout=3)
            return SimpleNamespace(success=True, data={"content": "<?php echo 'fixed';"})
        if path == "second.php":
            second_failed.set()
            return SimpleNamespace(
                success=False,
                error="模型服务计费或额度拒绝" if failure_kind == "billing_error" else "用量审计记录失败",
                failure_kind=failure_kind,
            )
        return SimpleNamespace(success=True, data={"content": "<?php echo 'fixed';"})

    monkeypatch.setattr(agent, "call_json", call_json)
    result_box: list[dict] = []
    repair_thread = threading.Thread(
        target=lambda: result_box.append(
            agent.repair(
                language="php",
                errors=[
                    {"file": path, "line": 1, "message": "unexpected token"}
                    for path in ("first.php", "second.php", "third.php")
                ],
                files={path: "<?php echo broken;" for path in ("first.php", "second.php", "third.php")},
            )
        )
    )
    repair_thread.start()
    try:
        assert first_started.wait(timeout=2)
        assert second_failed.wait(timeout=2)
        time.sleep(0.1)
        assert "third.php" not in started
        assert repair_thread.is_alive(), "应等待已经开始的第一份源码修复结束"
    finally:
        release_first.set()
        repair_thread.join(timeout=3)

    assert not repair_thread.is_alive()
    expected_error = "模型服务计费或额度拒绝" if failure_kind == "billing_error" else "用量审计记录失败"
    assert result_box[0]["error"] == expected_error
    assert result_box[0]["failure_kind"] == failure_kind
    assert started.count("first.php") == 1
    assert "third.php" not in started
