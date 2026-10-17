from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "reconcile_confirmed_cleanup_report.py"
SPEC = importlib.util.spec_from_file_location("reconcile_confirmed_cleanup_report_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
repair_rejection_reason = MODULE.repair_rejection_reason


def _candidate() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    environment = SimpleNamespace(
        public_id="sbx_9b0ba2cc75b943599b8d86c9",
        owner_id=5,
        project_id=17,
        purpose="test",
        test_mode="whitebox",
        status="failed",
        error="沙箱心跳超时，Worker 已确认资源终止",
        result_json=json.dumps({
            "cleanup_confirmed": True,
            "summary": "沙箱执行失败，Worker 资源回收待重试",
        }),
    )
    owner = SimpleNamespace(id=5, username="outle")
    task = SimpleNamespace(review_type="sandbox_test", status="failed", user_id=5, project_id=17)
    report = SimpleNamespace(user_id=5, content_json={
        "source": "sandbox_test",
        "public_id": environment.public_id,
    })
    return environment, owner, task, report


def test_reconcile_script_accepts_only_matching_confirmed_cleanup_report() -> None:
    environment, owner, task, report = _candidate()

    assert repair_rejection_reason(
        environment, owner, task, report,
        expected_public_id=environment.public_id,
        expected_username="outle",
    ) is None


def test_reconcile_script_rejects_owner_or_terminal_evidence_mismatch() -> None:
    environment, owner, task, report = _candidate()
    owner.username = "another_user"
    assert repair_rejection_reason(
        environment, owner, task, report,
        expected_public_id=environment.public_id,
        expected_username="outle",
    ) == "报告所有者账号不匹配"

    owner.username = "outle"
    environment.error = "timeout"
    assert repair_rejection_reason(
        environment, owner, task, report,
        expected_public_id=environment.public_id,
        expected_username="outle",
    ) == "环境错误字段未确认 Worker 终止"
