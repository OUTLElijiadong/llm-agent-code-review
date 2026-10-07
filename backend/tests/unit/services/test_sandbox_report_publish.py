"""沙箱多Agent审查报告入库报告中心 + agent 用例失败明细制品回归。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.review_task import ReviewTask
from app.services.sandbox_service import (
    _DEPLOY_VERIFY_RUNNER,
    _artifact_documents,
    _execution_result_passed,
    _extract_agent_test_failures,
    _extract_agent_tests_result,
    _extract_blackbox_result,
    _publish_sandbox_report,
    _reconcile_blackbox_agent_assertions,
    _remote_blackbox_execution,
    _remote_blackbox_passed,
    _sandbox_verification_coverage,
    _worker_execution_passed,
    _worker_status_is_terminal,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_extract_agent_test_failures_parses_output() -> None:
    log = (
        "agent test failed: ./_agent_tests/test_a.py\n"
        "Traceback: assertion failed\n"
        "agent test failed: ./_agent_tests/test_b.py\n"
        "FileNotFoundError\n"
        "PRISM_AGENT_TESTS_BEGIN {} PRISM_AGENT_TESTS_END\n"
    )
    details = _extract_agent_test_failures(log)
    assert "test_a.py" in details
    assert "assertion failed" in details["test_a.py"]
    assert "test_b.py" in details


def test_artifact_documents_include_agent_test_details() -> None:
    environment = SimpleNamespace(
        public_id="sbx_doc",
        agent_code="test_verifier",
        source_sha256="a" * 64,
        runtime="runsc",
    )
    conclusion = {
        "passed": True,
        "summary": "ok",
        "agent_tests": {
            "generated": 1,
            "passed": 0,
            "failed": 1,
            "details": {"test_a.py": "assert failed"},
        },
        "evidence": {},
    }
    documents = _artifact_documents(environment, conclusion)
    types = {d[0] for d in documents}
    assert "agent_test_details" in types
    detail_doc = next(d for d in documents if d[0] == "agent_test_details")
    assert "test_a.py" in detail_doc[3].decode()


def test_publish_sandbox_report_creates_review_task(db) -> None:
    from app.models.user import User

    owner = User(username="sbx_owner", password="x", role="user", status=1)
    db.add(owner)
    db.commit()
    environment = SimpleNamespace(
        public_id="sbx_pub",
        project_id=1,
        owner_id=owner.id,
        test_mode="blackbox",
    )
    report_md = "## 总体结论\nok\n## 问题清单\n- SQL 注入(建议验证)\n- 越权(建议验证)\n"
    result = _publish_sandbox_report(
        db,
        environment,
        {"passed": True, "summary": "ok"},
        report_md,
    )
    assert result["report_task_id"] > 0
    task = db.get(ReviewTask, result["report_task_id"])
    assert task.task_name == "沙箱黑白盒测试 · sbx_pub"
    assert task.review_type == "sandbox_test"
    assert task.score == 100
    assert task.coverage["verification_status"] == "partial"
    assert task.total_issues == 2
    # 幂等:再次发布不新建
    result2 = _publish_sandbox_report(db, environment, {"passed": True}, report_md)
    assert result2["report_task_id"] == result["report_task_id"]
    assert db.query(ReviewTask).filter(ReviewTask.task_name == "沙箱黑白盒测试 · sbx_pub").count() == 1


def test_partial_sandbox_report_preserves_recorded_score_and_exposes_scope() -> None:
    from app.services.report_service import _build_task_score_facts

    task = SimpleNamespace(
        review_type="sandbox_test",
        score=100,
        score_version=None,
        coverage={"verification_status": "partial"},
    )
    issue_count, score, facts, _severity = _build_task_score_facts(
        task,
        {"severity": {}, "total_issues": 0},
    )
    assert issue_count == 0
    assert score == 100
    assert facts["verification_status"] == "partial"
    task.coverage = None
    unknown = _build_task_score_facts(task, {"severity": {}, "total_issues": 0})
    assert unknown[1] == 100
    assert unknown[2]["verification_status"] == "unknown"
    task.coverage = {"verification_status": "failed"}
    failed = _build_task_score_facts(task, {"severity": {}, "total_issues": 0})
    assert failed[1] == 100
    assert failed[2]["verification_status"] == "failed"
    task.coverage = {"verification_status": "complete"}
    assert _build_task_score_facts(task, {"severity": {}, "total_issues": 0})[1] == 100


def test_sandbox_verification_coverage_distinguishes_complete_partial_and_failed() -> None:
    evidence = {
        "deterministic_test_execution": {"requested_mode": "blackbox", "status": "passed"},
        "agent_test_generation": {"status": "generated", "mode": "blackbox", "count": 1},
        "agent_tests": {"generated": 1, "passed_count": 1, "failed": 0},
        "blackbox_execution": {"status": "passed", "route": "/healthz", "status_code": 204},
    }
    assert _sandbox_verification_coverage({"passed": True, "evidence": evidence})["verification_status"] == "complete"
    partial = {**evidence, "agent_test_generation": {"status": "skipped", "mode": "blackbox"}}
    assert _sandbox_verification_coverage({"passed": True, "evidence": partial})["verification_status"] == "partial"
    assert _sandbox_verification_coverage({"passed": False, "evidence": evidence})["verification_status"] == "failed"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(204, "passed"), (302, "partial"), (401, "partial"), (404, "partial"), (503, "failed")],
)
def test_remote_blackbox_requires_final_2xx_for_functional_pass(status_code: int, expected: str) -> None:
    result = _remote_blackbox_execution(
        {"status_code": status_code, "target_origin": "https://example.test/api/health"}
    )
    assert result["status"] == expected
    assert result["route_passed"] is (expected == "passed")
    assert _remote_blackbox_passed(result) is (expected == "passed")
    deterministic_passed = _worker_execution_passed(
        purpose="test",
        state="succeeded",
        target_status="succeeded",
        conclusion={"exit_code": 0},
    )
    assert deterministic_passed and _remote_blackbox_passed(result) is (expected == "passed")
    coverage = _sandbox_verification_coverage(
        {
            "passed": _remote_blackbox_passed(result),
            "evidence": {
                "deterministic_test_execution": {"requested_mode": "blackbox", "status": "passed"},
                "agent_test_generation": {"status": "skipped", "mode": "blackbox"},
                "agent_tests": {},
                "blackbox_execution": result,
                "remote_blackbox": {"status_code": status_code},
            },
        }
    )
    assert coverage["remote_blackbox_status"] == expected
    if expected == "partial":
        assert coverage["verification_status"] == "partial"
    if expected == "failed":
        assert coverage["verification_status"] == "failed"


def test_blackbox_receipt_fails_closed_on_missing_duplicate_or_invalid_receipt() -> None:
    valid = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":true,"basis":"route_smoke",'
        '"agent_assertions_passed":null,"route_passed":true,'
        '"route":"/healthz","status_code":204}'
    )
    assert _extract_blackbox_result(valid) == {
        "status": "passed",
        "route_passed": True,
        "route": "/healthz",
        "status_code": 204,
        "basis": "route_smoke",
        "agent_assertions_passed": None,
        "failure_kind": None,
    }
    mismatched_basis = valid.replace('"basis":"route_smoke"', '"basis":"route_and_agent_assertions"')
    assert _extract_blackbox_result(mismatched_basis) is None
    assert _extract_blackbox_result(valid + "\n" + valid) is None
    inconsistent = valid.replace('"status_code":204', '"status_code":503')
    assert _extract_blackbox_result(inconsistent) is None
    failed = (
        valid.replace('"passed":true', '"passed":false')
        .replace(
            '"route_passed":true',
            '"route_passed":false',
        )
        .replace('"status_code":204', '"status_code":0')
    )
    assert _extract_blackbox_result(failed)["status"] == "failed"
    dynamic_failure = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":false,"basis":"route_and_agent_assertions",'
        '"agent_assertions_passed":false,"route_passed":true,"route":"/healthz","status_code":204}'
    )
    extracted_dynamic_failure = _extract_blackbox_result(dynamic_failure)
    assert extracted_dynamic_failure["status"] == "failed"
    assert extracted_dynamic_failure["route_passed"] is True
    assert extracted_dynamic_failure["failure_kind"] == "dynamic_assertion"
    assert _extract_blackbox_result("no runner marker") is None


def test_agent_runner_timeout_is_reported_as_execution_timeout_not_protocol_error() -> None:
    result = _extract_agent_tests_result(
        'PRISM_AGENT_TESTS_BEGIN {"protocol_version":2,"generated":1,"passed":0,"failed":1,'
        '"passed_count":0,"files":{"blackbox.py":"fail"},"file_results":{"blackbox.py":'
        '{"status":"fail","phase":"execute","failure_kind":"timeout","exit_code":137,'
        '"output_encoding":"base64","output_base64":"aGFuZ2Vk"}}} PRISM_AGENT_TESTS_END'
    )

    assert result is not None
    assert result["files"] == {"blackbox.py": "fail"}
    assert result["file_results"]["blackbox.py"]["failure_kind"] == "timeout"
    assert result["file_results"]["blackbox.py"]["phase"] == "execute"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(302, "partial"), (401, "partial"), (403, "partial"), (404, "failed"), (500, "failed")],
)
def test_blackbox_redirect_or_auth_receipt_is_not_a_functional_pass(
    status_code: int,
    expected: str,
) -> None:
    receipt = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":false,"basis":"route_smoke",'
        '"agent_assertions_passed":null,"route_passed":false,'
        f'"route":"/healthz","status_code":{status_code}' + "}"
    )
    result = _extract_blackbox_result(receipt)
    assert result is not None
    assert result["status"] == expected
    assert result["route_passed"] is False
    evidence = {
        "deterministic_test_execution": {"requested_mode": "blackbox", "status": "passed"},
        "agent_test_generation": {"status": "skipped", "mode": "blackbox"},
        "blackbox_execution": result,
    }
    expected_coverage = "partial" if expected == "partial" else "failed"
    assert (
        _sandbox_verification_coverage({"passed": False, "evidence": evidence})["verification_status"]
        == expected_coverage
    )


def test_blackbox_receipt_accepts_declared_root_entrypoint_when_it_returns_2xx() -> None:
    receipt = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":true,"basis":"route_smoke",'
        '"agent_assertions_passed":null,"route_passed":true,"route_origin":"source_route",'
        '"route":"/","status_code":204}'
    )
    result = _extract_blackbox_result(receipt)
    assert result is not None
    assert result["status"] == "passed"
    assert result["route"] == "/"


def test_blackbox_receipt_rejects_root_success_without_route_evidence() -> None:
    receipt = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":true,"basis":"route_smoke",'
        '"agent_assertions_passed":null,"route_passed":true,"route":"/","status_code":200}'
    )
    assert _extract_blackbox_result(receipt) is None


def test_blackbox_runner_budget_timeout_is_a_structured_failure() -> None:
    receipt = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":false,"basis":"route_smoke",'
        '"agent_assertions_passed":null,"route_passed":false,"route":"/","status_code":0,'
        '"route_origin":"none","failure_kind":"timeout","failure_reason":"runner_budget_exhausted"}'
    )
    result = _extract_blackbox_result(receipt)
    assert result is not None
    assert result["status"] == "failed"
    assert result["failure_kind"] == "timeout"
    assert result["failure_reason"] == "runner_budget_exhausted"


def test_blackbox_assertions_are_not_claimed_when_generation_was_skipped() -> None:
    """生产 v4.0.77 曾在动态生成跳过后仍把 route-only 标成 AI 断言通过。"""
    worker_receipt = {
        "status": "passed",
        "passed": True,
        "route_passed": True,
        "route": "/health",
        "status_code": 200,
        "basis": "route_and_agent_assertions",
        "agent_assertions_passed": True,
        "failure_kind": None,
    }

    result = _reconcile_blackbox_agent_assertions(
        worker_receipt,
        generation={"status": "skipped", "mode": "blackbox", "count": 0},
        expected_files=set(),
        agent_tests_result=None,
    )

    assert result == {
        "status": "passed",
        "passed": True,
        "route_passed": True,
        "route": "/health",
        "status_code": 200,
        "basis": "route_smoke",
        "agent_assertions_passed": None,
        "failure_kind": None,
    }


def test_skipped_generation_preserves_explicit_runner_assertion_failure() -> None:
    receipt = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":false,'
        '"basis":"route_and_agent_assertions","agent_assertions_passed":false,'
        '"route_passed":true,"route":"/healthz","status_code":204}'
    )
    extracted = _extract_blackbox_result(receipt)
    assert extracted is not None

    result = _reconcile_blackbox_agent_assertions(
        extracted,
        generation={"status": "skipped", "mode": "blackbox", "count": 0},
        expected_files=set(),
        agent_tests_result=None,
    )

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert result["basis"] == "route_and_agent_assertions"
    assert result["agent_assertions_passed"] is False
    assert result["failure_kind"] == "dynamic_assertion"


@pytest.mark.parametrize(
    ("mode", "expected_files"),
    [
        ("blackbox", set()),
        ("blackbox", {"test_only.py"}),
        ("blackbox", {"blackbox.py", "nested/blackbox.extra.py"}),
        ("combined", set()),
        ("combined", {"test_only.py"}),
        ("combined", {"blackbox.py", "nested/blackbox.extra.py"}),
    ],
)
def test_generated_blackbox_modes_without_one_unique_blackbox_file_fail_contract(mode, expected_files) -> None:
    receipt = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":true,"basis":"route_smoke",'
        '"agent_assertions_passed":null,"route_passed":true,"route":"/healthz","status_code":204}'
    )
    extracted = _extract_blackbox_result(receipt)
    assert extracted is not None

    result = _reconcile_blackbox_agent_assertions(
        extracted,
        generation={"status": "generated", "mode": mode, "count": len(expected_files)},
        expected_files=expected_files,
        agent_tests_result={
            "generated": len(expected_files),
            "passed_count": len(expected_files),
            "files": {name: "pass" for name in expected_files},
        },
    )

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert result["basis"] == "route_smoke"
    assert result["agent_assertions_passed"] is None
    assert result["failure_kind"] == "agent_assertion_contract"


def test_extracted_application_startup_failure_survives_assertion_reconciliation() -> None:
    receipt = (
        'PRISM_BLACKBOX_DONE {"executed":true,"passed":false,"basis":"route_smoke",'
        '"agent_assertions_passed":null,"route_passed":false,"route":"/healthz",'
        '"status_code":0,"failure_kind":"application_startup",'
        '"failure_reason":"application_readiness_timeout"}'
    )
    extracted = _extract_blackbox_result(receipt)
    assert extracted is not None

    result = _reconcile_blackbox_agent_assertions(
        extracted,
        generation={"status": "generated", "mode": "blackbox", "count": 1},
        expected_files={"blackbox.py"},
        agent_tests_result={"generated": 1, "passed_count": 1, "files": {"blackbox.py": "pass"}},
    )

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert result["basis"] == "route_smoke"
    assert result["agent_assertions_passed"] is None
    assert result["failure_kind"] == "application_startup"
    assert result["failure_reason"] == "application_readiness_timeout"


@pytest.mark.parametrize(
    ("file_status", "expected_status", "expected_assertions"),
    [("pass", "passed", True), ("fail", "failed", False), (None, "failed", False)],
    ids=["executed-pass", "executed-fail", "missing-runner-receipt"],
)
def test_blackbox_assertion_claim_requires_injected_file_execution_receipt(
    file_status,
    expected_status,
    expected_assertions,
) -> None:
    worker_receipt = {
        "status": "passed",
        "route_passed": True,
        "route": "/health",
        "status_code": 200,
        "basis": "route_smoke",
        "agent_assertions_passed": None,
        "failure_kind": None,
    }
    agent_tests = (
        None
        if file_status is None
        else {
            "generated": 1,
            "passed_count": int(file_status == "pass"),
            "failed": int(file_status == "fail"),
            "files": {"blackbox.py": file_status},
        }
    )

    result = _reconcile_blackbox_agent_assertions(
        worker_receipt,
        generation={"status": "generated", "mode": "blackbox", "count": 1},
        expected_files={"blackbox.py"},
        agent_tests_result=agent_tests,
    )

    assert result["status"] == expected_status
    assert result["basis"] == "route_and_agent_assertions"
    assert result["agent_assertions_passed"] is expected_assertions
    assert result["passed"] is (expected_status == "passed")


def test_blackbox_assertion_receipt_is_independent_from_other_combined_test_files() -> None:
    worker_receipt = {
        "status": "passed",
        "route_passed": True,
        "route": "/health",
        "status_code": 200,
        "basis": "route_smoke",
        "agent_assertions_passed": None,
        "failure_kind": None,
    }
    result = _reconcile_blackbox_agent_assertions(
        worker_receipt,
        generation={"status": "generated", "mode": "combined", "count": 2},
        expected_files={"blackbox.py", "test_extra.py"},
        agent_tests_result={
            "generated": 2,
            "passed_count": 1,
            "failed": 1,
            "files": {"blackbox.py": "pass", "test_extra.py": "fail"},
        },
    )

    assert result["agent_assertions_passed"] is True
    assert result["passed"] is True


@pytest.mark.parametrize(
    ("purpose", "state", "target_status", "exit_code", "expected"),
    [
        ("test", "succeeded", "succeeded", 0, True),
        ("test", "completed", "succeeded", 0, True),
        ("test", "succeeded", "failed", 0, False),
        ("test", "completed", "blocked", 0, False),
        ("test", "succeeded", "succeeded", None, False),
        ("test", "succeeded", "succeeded", "0", False),
        ("test", "running", "ready", None, False),
        ("test", "ready", "failed", 0, False),
        ("deploy", "running", "ready", None, True),
        ("deploy", "succeeded", "succeeded", 0, True),
    ],
)
def test_worker_execution_requires_terminal_explicit_zero_exit_for_tests(
    purpose: str,
    state: str,
    target_status: str,
    exit_code: object,
    expected: bool,
) -> None:
    assert (
        _worker_execution_passed(
            purpose=purpose,
            state=state,
            target_status=target_status,
            conclusion={"exit_code": exit_code},
        )
        is expected
    )


@pytest.mark.parametrize(
    ("test_mode", "evidence", "generation", "agent_result", "remote", "expected"),
    [
        ("whitebox", {}, None, None, None, True),
        ("blackbox", {}, None, None, None, False),
        (
            "blackbox",
            {"blackbox_execution": {"status": "passed", "route_passed": True}},
            None,
            None,
            None,
            True,
        ),
        (
            "blackbox",
            {"blackbox_execution": {"status": "passed", "route_passed": False}},
            None,
            None,
            None,
            False,
        ),
        (
            "combined",
            {"blackbox_execution": {"status": "passed", "route_passed": True}},
            {"status": "generated"},
            None,
            None,
            False,
        ),
        (
            "blackbox",
            {
                "blackbox_execution": {"status": "passed", "route_passed": True},
                "remote_blackbox_execution": {"status": "partial", "route_passed": False},
            },
            None,
            None,
            "https://target.example",
            False,
        ),
        (
            "combined",
            {"remote_blackbox_execution": {"status": "passed", "route_passed": True}},
            None,
            None,
            "https://target.example",
            True,
        ),
        (
            "combined",
            {"remote_blackbox_execution": {"status": "partial", "route_passed": False}},
            None,
            None,
            "https://target.example",
            False,
        ),
    ],
)
def test_final_test_gate_requires_blackbox_and_dynamic_receipts(
    test_mode: str,
    evidence: dict[str, Any],
    generation: dict[str, Any] | None,
    agent_result: dict[str, Any] | None,
    remote: str | None,
    expected: bool,
) -> None:
    """Worker success cannot hide a missing/failed child verification receipt."""
    assert (
        _execution_result_passed(
            purpose="test",
            state="succeeded",
            target_status="succeeded",
            conclusion={"exit_code": 0},
            test_mode=test_mode,
            evidence=evidence,
            agent_test_generation=generation,
            agent_tests_result=agent_result,
            remote_target_url=remote,
        )
        is expected
    )


def test_combined_remote_route_receipt_closes_blackbox_leg_without_overstating_ai_coverage() -> None:
    remote = _remote_blackbox_execution({"status_code": 204, "target_origin": "https://target.example/health"})
    evidence = {
        "deterministic_test_execution": {"requested_mode": "combined", "status": "passed"},
        "agent_test_generation": {"status": "skipped", "mode": "whitebox"},
        "agent_tests": {},
        "remote_blackbox_execution": remote,
        "blackbox_execution": remote,
    }
    assert (
        _execution_result_passed(
            purpose="test",
            state="succeeded",
            target_status="succeeded",
            conclusion={"exit_code": 0},
            test_mode="combined",
            evidence=evidence,
        )
        is True
    )
    coverage = _sandbox_verification_coverage({"passed": True, "evidence": evidence})
    assert coverage["blackbox_status"] == "passed"
    assert coverage["verification_status"] == "partial"


@pytest.mark.parametrize(
    ("purpose", "state", "expected"),
    [
        ("test", "running", False),
        ("test", "ready", False),
        ("test", "succeeded", True),
        ("test", "failed", True),
        ("deploy", "running", True),
        ("deploy", "ready", False),
    ],
)
def test_worker_status_polling_only_treats_live_deploy_as_terminal(
    purpose: str,
    state: str,
    expected: bool,
) -> None:
    assert _worker_status_is_terminal(purpose, state) is expected


@pytest.mark.parametrize(("route_status", "expected_exit"), [(204, 0), (503, 1)])
def test_deploy_verification_runner_probes_api_route_when_root_is_404(
    tmp_path: Path,
    route_status: int,
    expected_exit: int,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "wsgi.py").write_text(
        "# path('api/v1/health', health_view)\n"
        "def application(environ, start_response):\n"
        "    if environ.get('PATH_INFO') == '/api/v1/health':\n"
        f"        status = '{route_status} Test'\n"
        "        body = b''\n"
        "    else:\n"
        "        status = '404 Not Found'\n"
        "        body = b'not found'\n"
        "    start_response(status, [('Content-Length', str(len(body)))])\n"
        "    return [body]\n",
        encoding="utf-8",
    )
    runner = tmp_path / "verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.update(
        {
            "PRISM_WORKSPACE": str(source),
            "PRISM_LANGUAGE": "python",
            "PRISM_PREVIEW_PORT": str(port),
        }
    )
    syntax = subprocess.run(["bash", "-n", str(runner)], capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr
    completed = subprocess.run(
        ["bash", str(runner), "blackbox"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == expected_exit, output
    assert f"blackbox probe /api/v1/health -> {route_status}" in output
    assert "PRISM_BLACKBOX_DONE" in output
    assert f'"status_code":{route_status}' in output


def test_deploy_verification_runner_starts_asgi_entrypoint(tmp_path: Path) -> None:
    """合法的 asgi.py FastAPI 项目应能进入内嵌验证器并探测 API 路由。"""
    pytest.importorskip("uvicorn")
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "asgi.py").write_text(
        "from fastapi import FastAPI, Response\n"
        "application = FastAPI()\n"
        "@application.get('/api/v1/health', status_code=204)\n"
        "def health(): return Response(status_code=204)\n",
        encoding="utf-8",
    )
    runner = tmp_path / "verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    port = _free_port()
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.update(
        {
            "PRISM_WORKSPACE": str(source),
            "PRISM_LANGUAGE": "python",
            "PRISM_PREVIEW_PORT": str(port),
        }
    )
    completed = subprocess.run(
        ["bash", str(runner), "blackbox"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "blackbox probe /api/v1/health -> 204" in output
    assert '"basis":"route_smoke"' in output
