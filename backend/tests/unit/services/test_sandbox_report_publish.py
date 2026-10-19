"""沙箱多Agent审查报告入库报告中心 + agent 用例失败明细制品回归。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.models.agent_capability import SandboxArtifact, SandboxEnvironment
from app.models.project import Project
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import sandbox_service
from app.services.sandbox_service import (
    _DEPLOY_VERIFY_RUNNER,
    _append_bounded_browser_coverage,
    _artifact_documents,
    _build_deterministic_test_report,
    _execution_result_passed,
    _extract_agent_test_failures,
    _extract_agent_tests_result,
    _extract_blackbox_result,
    _merge_report_facts,
    _publish_sandbox_report,
    _publish_terminal_sandbox_report,
    _reconcile_blackbox_agent_assertions,
    _remote_blackbox_execution,
    _remote_blackbox_passed,
    _sandbox_execution_disclosure,
    _sandbox_result_score,
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


def test_sandbox_html_report_wraps_unbroken_execution_log() -> None:
    environment = SimpleNamespace(
        public_id="sbx_long_log",
        agent_code="test_verifier",
        source_sha256="a" * 64,
        runtime="runsc",
    )
    long_log_token = "A" * 512
    documents = _artifact_documents(
        environment,
        {
            "passed": False,
            "summary": "测试未通过",
            "evidence": {"worker_result": {"logs": {"text": long_log_token}}},
        },
    )
    html_report = next(document[3].decode("utf-8") for document in documents if document[0] == "html")

    assert long_log_token in html_report
    assert "white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;" in html_report
    assert "max-width:100%;box-sizing:border-box;" in html_report


def test_deterministic_fallback_report_discloses_remote_probe_facts_and_scope() -> None:
    report = _build_deterministic_test_report({
        "execution_status": "failed",
        "passed": False,
        "summary": "远程黑盒响应未通过",
        "agent_tests": {"generated": 0, "passed_count": 0, "failed": 0},
        "coverage": {"requested_mode": "blackbox"},
        "evidence": {
            "remote_blackbox": {
                "target_origin": "https://example.test/",
                "status_code": 302,
                "latency_ms": 42,
                "content_type": "text/html",
                "redirect_location": "/login",
                "body_bytes_sampled": 8192,
                "missing_security_headers": ["content-security-policy", "referrer-policy"],
                "security_headers": {
                    "content-security-policy": "",
                    "strict-transport-security": "max-age=31536000",
                    "x-content-type-options": "nosniff",
                    "referrer-policy": "",
                },
            },
            "remote_blackbox_execution": {"status_code": 302, "status": "partial"},
        },
    })
    assert "远程网站检查范围" in report
    assert "https://example.test" in report
    assert "HTTP 302" in report
    assert "42 ms" in report
    assert "text/html" in report
    assert "未跟随重定向" in report
    assert "content-security-policy" in report
    assert "referrer-policy" in report
    assert "未分析或保存正文内容" in report
    assert "用于探测判断" not in report
    assert "完整网站漏洞覆盖" in report


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
        started_at=datetime.now(timezone.utc) - timedelta(days=11),
        stopped_at=datetime.now(timezone.utc),
    )
    report_md = "## 总体结论\nok\n## 问题清单\n- SQL 注入(建议验证)\n- 越权(建议验证)\n"
    result = _publish_sandbox_report(
        db,
        environment,
        {"passed": False, "summary": "黑盒未通过", "duration_ms": 1234, "evidence": {
            "deterministic_test_execution": {"requested_mode": "blackbox", "status": "failed"},
            "blackbox_execution": {
                "status": "failed", "route": "/", "status_code": 404,
                "route_origin": "none", "failure_kind": "route_or_dynamic_assertion",
            },
        }},
        report_md,
    )
    assert result["report_task_id"] > 0
    task = db.get(ReviewTask, result["report_task_id"])
    assert task.task_name == "沙箱黑盒测试 · sbx_pub"
    assert task.review_type == "sandbox_test"
    assert task.score == 0
    assert task.coverage["verification_status"] == "failed"
    assert task.coverage["duration_source"] == "measured_execution"
    assert task.duration_ms == 1234
    assert task.start_time == task.end_time - timedelta(milliseconds=1234)
    assert "HTTP 404" in task.error_message
    assert "未找到可用应用路由" in task.coverage["reason"]
    assert task.total_issues == 2
    # 幂等:再次发布不新建
    result2 = _publish_sandbox_report(db, environment, {"passed": False}, report_md)
    assert result2["report_task_id"] == result["report_task_id"]
    assert db.query(ReviewTask).filter(ReviewTask.task_name == "沙箱黑盒测试 · sbx_pub").count() == 1


def test_publish_sandbox_report_does_not_use_long_lived_environment_as_task_duration(db) -> None:
    owner = User(username="sbx_unknown_duration_owner", password="x", role="user", status=1)
    db.add(owner)
    db.commit()
    environment = SimpleNamespace(
        public_id="sbx_unknown_duration",
        project_id=1,
        owner_id=owner.id,
        test_mode="whitebox",
        started_at=datetime.now(timezone.utc) - timedelta(days=11),
        stopped_at=datetime.now(timezone.utc),
    )

    result = _publish_sandbox_report(db, environment, {"passed": False}, "## 执行结果\n未完成")

    task = db.get(ReviewTask, result["report_task_id"])
    assert task.duration_ms == 0
    assert task.start_time is None
    assert task.coverage["duration_source"] == "unknown"


def test_report_publish_failure_is_explicit_and_logged(db, monkeypatch) -> None:
    from app.models.user import User

    owner = User(username="sbx_publish_failure_owner", password="x", role="user", status=1)
    db.add(owner)
    db.commit()
    environment = SimpleNamespace(
        public_id="sbx_publish_failure", project_id=1, owner_id=owner.id, test_mode="whitebox"
    )
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("private database details")))
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sandbox_service.logger,
        "exception",
        lambda message, public_id: logged.append((message, public_id)),
    )

    result = _publish_sandbox_report(db, environment, {"passed": True}, "## 总体结论\n通过")

    assert result["report_persisted"] is False
    assert result["failure_code"] == "report_persistence_failed"
    assert "private database details" not in str(result)
    assert logged == [("sandbox report publication failed public_id={}", "sbx_publish_failure")]


def test_report_publish_retries_transient_database_failure(db, monkeypatch) -> None:
    owner = User(username="sbx_publish_retry_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="sandbox publish retry", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_publish_retry",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="succeeded",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="d" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        result_json='{"passed":true,"summary":"测试完成"}',
    )
    db.add(environment)
    db.commit()
    commit = db.commit
    calls = 0

    def commit_after_transient_error():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("commit", {}, RuntimeError("database temporarily unavailable"))
        commit()

    monkeypatch.setattr(db, "commit", commit_after_transient_error)

    report_md = "## 总体结论\n通过"
    result = _publish_sandbox_report(
        db,
        environment,
        {"passed": True, "execution_status": "succeeded"},
        report_md,
        _sync_terminal_artifact=True,
    )

    assert result["report_persisted"] is True
    assert result["report_task_id"] > 0
    assert calls == 2
    report = db.query(ReviewReport).filter_by(task_id=result["report_task_id"]).one()
    assert db.query(ReviewReport).filter_by(task_id=result["report_task_id"]).count() == 1
    task = db.get(ReviewTask, result["report_task_id"])
    artifact = db.query(SandboxArtifact).filter_by(
        environment_id=environment.id, artifact_type="review_report"
    ).one()
    report_bytes = report.content_json["report_md"].encode("utf-8")
    assert report_bytes == report_md.encode("utf-8")
    assert task.summary == report.content_json["report_md"]
    assert base64.b64decode(artifact.content_base64, validate=True) == report_bytes
    assert artifact.byte_size == len(report_bytes)
    assert artifact.sha256 == hashlib.sha256(report_bytes).hexdigest()


def test_deterministic_sandbox_report_discloses_terminal_status_scope_and_errors() -> None:
    report = _build_deterministic_test_report(
        {
            "passed": True,
            "execution_status": "stopped",
            "summary": "用户停止了黑盒测试",
            "coverage": {
                "tested": ["/health"],
                "unscanned": ["登录后接口", "上传流程"],
                "excluded": ["third_party/"],
                "skipped": ["POST /admin"],
            },
            "evidence": {"error": "worker timeout"},
        }
    )

    assert "已停止" in report
    assert "/health" in report
    assert "登录后接口" in report
    assert "third_party/" in report
    assert "POST /admin" in report
    assert "worker timeout" in report
    assert "系统验证门禁：部分通过" in report or "系统验证门禁：未通过" in report


def test_publish_sandbox_report_preserves_stopped_status_and_coverage(db) -> None:
    owner = User(username="sbx_stopped_owner", password="x", role="user", status=1)
    db.add(owner)
    db.commit()
    environment = SimpleNamespace(
        public_id="sbx_stopped", project_id=1, owner_id=owner.id, test_mode="blackbox"
    )
    conclusion = {
        "passed": True,
        "execution_status": "stopped",
        "summary": "用户停止，测试未完成",
        "coverage": {"tested": ["/health"], "unscanned": ["其他路由"]},
        "evidence": {"error": "user requested stop"},
    }
    result = _publish_sandbox_report(db, environment, conclusion, "## 终态\nstopped\n")

    task = db.get(ReviewTask, result["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    assert task.status == "cancelled"
    assert task.score == 0
    assert task.error_message == "user requested stop"
    assert task.coverage["unscanned"] == ["其他路由"]
    assert report.content_json["execution_status"] == "stopped"
    assert report.content_json["coverage"]["tested"] == ["/health"]


def test_stopped_sandbox_terminal_state_gets_report_center_and_artifact(db) -> None:
    owner = User(username="sbx_terminal_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="terminal sandbox", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_terminal_report",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="stopped",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="a" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        error="user requested stop",
        result_json=(
            '{"passed":false,"summary":"stopped","coverage":{"tested":["/health"]},'
            '"evidence":{"error":"user requested stop"}}'
        ),
    )
    db.add(environment)
    db.commit()

    environment.status = "failed"
    environment.error = "stale callback error"
    environment.result_json = (
        '{"passed":false,"summary":"stale callback",'
        '"evidence":{"stale_callback":true}}'
    )
    # A delayed callback may carry an older terminal label; the persisted environment
    # state is the current source of truth for the report.
    published = _publish_terminal_sandbox_report(db, environment, execution_status="failed")
    assert published and published["report_persisted"] is True
    task = db.get(ReviewTask, published["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    stored_environment = db.get(SandboxEnvironment, environment.id)

    assert task.status == "cancelled"
    assert task.error_message == "user requested stop"
    assert task.coverage["unscanned"]
    assert report.content_json["execution_status"] == "stopped"
    assert "stopped" in report.content_json["report_md"]
    assert "user requested stop" in report.content_json["report_md"]
    assert stored_environment.status == "stopped"
    assert stored_environment.error == "user requested stop"
    assert json.loads(stored_environment.result_json)["evidence"]["stale_callback"] is True
    # Terminal report artifact creation is best effort because artifact writes retain the
    # production execution-authorization check; report-center persistence is still verified.
    assert db.query(SandboxArtifact).filter_by(
        environment_id=environment.id, artifact_type="review_report"
    ).count() in {0, 1}


def test_same_status_stale_callback_cannot_overwrite_persisted_success(db) -> None:
    owner = User(username="sbx_same_status_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="same status callback", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_same_status_report",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="succeeded",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="a" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        result_json=json.dumps(
            {
                "passed": True,
                "summary": "persisted latest outcome",
                "ai_review_status": "complete",
                "agent_tests": {"generated": 1, "passed_count": 1, "failed": 0},
                "coverage": {"coverage_status": "complete", "tested": ["/health"], "unscanned": []},
                "evidence": {
                    "persisted_receipt": "latest",
                    "deterministic_test_execution": {"requested_mode": "blackbox", "status": "passed"},
                    "agent_test_generation": {"status": "generated", "mode": "blackbox", "count": 1},
                    "agent_tests": {"generated": 1, "passed_count": 1, "failed": 0},
                    "blackbox_execution": {"status": "passed", "route": "/health", "status_code": 200},
                },
            }
        ),
    )
    db.add(environment)
    db.commit()

    original_ai_report = _publish_sandbox_report(
        db,
        environment,
        {
            "passed": True,
            "execution_status": "succeeded",
            "summary": "persisted latest outcome",
            "ai_review_status": "complete",
            "agent_tests": {"generated": 1, "passed_count": 1, "failed": 0},
            "coverage": {"coverage_status": "complete", "tested": ["/health"], "unscanned": []},
            "evidence": {
                "persisted_receipt": "latest",
                "deterministic_test_execution": {"requested_mode": "blackbox", "status": "passed"},
                "agent_test_generation": {"status": "generated", "mode": "blackbox", "count": 1},
                "agent_tests": {"generated": 1, "passed_count": 1, "failed": 0},
                "blackbox_execution": {"status": "passed", "route": "/health", "status_code": 200},
            },
        },
        "## 多角色审查意见\n已完成 AI 审查并核实执行证据。\n",
    )
    assert original_ai_report["report_persisted"] is True
    original_task = db.get(ReviewTask, original_ai_report["report_task_id"])
    # The database column is non-nullable; zero remains a storage placeholder.
    # User-facing report payloads must treat it as unavailable.
    assert original_task.score == 0
    assert original_task.coverage["coverage_status"] == "complete"
    assert original_task.coverage["score_available"] is False
    assert original_task.coverage["score_basis"] is None

    # Same terminal status does not mean the callback carries the latest snapshot.
    environment.error = "stale callback failure"
    environment.result_json = (
        '{"passed":false,"summary":"stale same-status callback",'
        '"error":"stale callback result_json failure",'
        '"ai_review_status":"incomplete",'
        '"ai_review_failure_reason":"stale callback review failure",'
        '"coverage":{"tested":["/stale"]},'
        '"evidence":{"callback_only":"preserve"}}'
    )
    published = _publish_terminal_sandbox_report(db, environment, execution_status="succeeded")
    assert published and published["report_persisted"] is True
    task = db.get(ReviewTask, published["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    stored_environment = db.get(SandboxEnvironment, environment.id)
    stored_result = json.loads(stored_environment.result_json)

    assert task.status == "success"
    assert task.score == 0
    assert "已完成 AI 审查并核实执行证据。" in report.content_json["report_md"]
    assert "## 后续验证更新（Prism 系统）" in report.content_json["report_md"]
    assert "stale same-status callback" not in report.content_json["report_md"]
    assert "stale callback failure" not in report.content_json["report_md"]
    assert "stale callback result_json failure" not in report.content_json["report_md"]
    assert "stale callback review failure" not in report.content_json["report_md"]
    assert "失败说明：" not in report.content_json["report_md"]
    assert report.content_json["execution_status"] == "succeeded"
    assert report.content_json["ai_review"]["status"] == "complete"
    assert report.content_json["ai_review"]["failure_reason"] is None
    assert report.content_json["evidence"]["persisted_receipt"] == "latest"
    assert report.content_json["evidence"]["callback_only"] == "preserve"
    assert task.summary == report.content_json["report_md"]
    assert stored_result["passed"] is True
    assert stored_result["summary"] == "persisted latest outcome"
    assert "error" not in stored_result
    assert stored_result["ai_review_status"] == "complete"
    assert "ai_review_failure_reason" not in stored_result
    assert stored_environment.error is None
    assert stored_result["coverage"]["tested"] == ["/health", "/stale"]
    artifact = db.query(SandboxArtifact).filter_by(
        environment_id=environment.id, artifact_type="review_report"
    ).one()
    report_bytes = report.content_json["report_md"].encode("utf-8")
    assert base64.b64decode(artifact.content_base64, validate=True) == report_bytes
    assert artifact.byte_size == len(report_bytes)
    assert artifact.sha256 == hashlib.sha256(report_bytes).hexdigest()


def test_ai_review_metadata_merge_is_monotonic() -> None:
    assert sandbox_service._merge_ai_review_metadata(
        {"status": "complete", "failure_reason": None},
        {"status": "incomplete", "failure_reason": "stale"},
    ) == ({"status": "complete", "failure_reason": None}, True)
    assert sandbox_service._merge_ai_review_metadata(
        {"status": "incomplete", "failure_reason": "prior failure"},
        {"status": "complete", "failure_reason": "stale"},
    ) == ({"status": "complete", "failure_reason": None}, False)
    assert sandbox_service._merge_ai_review_metadata(
        {"status": "incomplete", "failure_reason": "prior failure"},
        {"status": "unknown", "failure_reason": None},
    ) == ({"status": "incomplete", "failure_reason": "prior failure"}, True)


def test_repeated_stop_preserves_existing_full_sandbox_report(db) -> None:
    owner = User(username="sbx_repeat_stop_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="repeat stop", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_repeat_stop_report",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="succeeded",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="c" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        started_at=datetime.utcnow() - timedelta(minutes=2),
        stopped_at=datetime.utcnow(),
        result_json='{"passed":true,"summary":"完整测试结果"}',
    )
    db.add(environment)
    db.commit()

    original_report = (
        "## 测试结论\n所有约定用例均已完成。\n\n"
        "## 发现的问题\n未发现可复现问题。\n\n"
        "## 验证范围\n覆盖登录、权限和核心业务流程。\n"
    )
    published = _publish_sandbox_report(
        db,
        environment,
        {
            "passed": True,
            "execution_status": "succeeded",
            "summary": "完整测试结果",
            "coverage": {"verification_status": "complete", "tested": ["登录", "权限", "核心业务流程"]},
            "evidence": {"successful_cases": 12},
        },
        original_report,
    )
    task = db.get(ReviewTask, published["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    original_content = dict(report.content_json)
    original_summary = task.summary

    sandbox_service.stop_environment(db, owner, environment.public_id)

    db.refresh(task)
    db.refresh(report)
    assert task.summary == original_summary
    assert report.content_json == original_content
    assert report.content_json["report_md"] == original_report


def test_new_terminal_failure_evidence_refreshes_existing_sandbox_report(db) -> None:
    owner = User(username="sbx_new_failure_evidence_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="new failure evidence", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_new_failure_evidence",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="succeeded",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="d" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        result_json='{"passed":true,"summary":"测试完成"}',
    )
    db.add(environment)
    db.commit()
    published = _publish_sandbox_report(
        db,
        environment,
        {
            "passed": True,
            "execution_status": "succeeded",
            "summary": "测试完成",
            "ai_review_status": "complete",
            "agent_tests": {"generated": 1, "passed_count": 1},
            "coverage": {"tested": ["账号归属检查"]},
            "evidence": {"deterministic_test_execution": {"status": "passed", "case_count": 3}},
        },
        "## 多角色审查意见\nAI 已确认核心鉴权流程存在越权风险，需按用户身份校验资源归属。\n\n"
        "## 问题清单\n### [高] 资源访问缺少归属校验\n证据：跨账号访问返回了受限数据。\n",
    )
    task = db.get(ReviewTask, published["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    ai_analysis = "AI 已确认核心鉴权流程存在越权风险，需按用户身份校验资源归属。"

    environment.status = "failed"
    environment.error = "浏览器黑盒断言失败"
    environment.result_json = (
        '{"passed":false,"summary":"浏览器黑盒断言失败",'
        '"coverage":{"tested":["/healthz"],"unscanned":["登录后路由"]},'
        '"evidence":{"browser_blackbox_runs":[{"status":"failed","route":"/healthz"}]}}'
    )
    stale_report_bytes = b"stale report content"
    for _ in range(2):
        db.add(
            SandboxArtifact(
                environment_id=environment.id,
                artifact_type="review_report",
                file_name="old-report.md",
                mime_type="text/markdown",
                byte_size=len(stale_report_bytes),
                sha256=hashlib.sha256(stale_report_bytes).hexdigest(),
                storage_ref=f"database://sandbox-artifact/{environment.public_id}-review_report",
                content_base64=base64.b64encode(stale_report_bytes).decode("ascii"),
            )
        )
    db.commit()

    refreshed = _publish_terminal_sandbox_report(db, environment, execution_status="failed")

    db.refresh(task)
    db.refresh(report)
    assert refreshed["report_persisted"] is True
    assert task.status == "failed"
    assert task.coverage["execution_status"] == "failed"
    assert report.content_json["execution_status"] == "failed"
    assert report.content_json["evidence"]["browser_blackbox_runs"][0]["route"] == "/healthz"
    assert "浏览器黑盒断言失败" in report.content_json["report_md"]
    assert ai_analysis in report.content_json["report_md"]
    assert report.content_json["ai_review"]["status"] == "complete"
    assert report.content_json["report_issue_summary"]["total"] == 1
    assert report.content_json["evidence"]["deterministic_test_execution"]["case_count"] == 3
    assert report.content_json["coverage"]["tested"] == ["账号归属检查", "/healthz"]
    artifacts = (
        db.query(SandboxArtifact)
        .filter_by(environment_id=environment.id, artifact_type="review_report")
        .all()
    )
    report_bytes = report.content_json["report_md"].encode("utf-8")
    assert len(artifacts) == 2
    for artifact in artifacts:
        assert base64.b64decode(artifact.content_base64, validate=True) == report_bytes
        assert artifact.byte_size == len(report_bytes)
        assert artifact.sha256 == hashlib.sha256(report_bytes).hexdigest()

    repeated = _publish_terminal_sandbox_report(db, environment, execution_status="failed")

    db.refresh(report)
    artifacts = (
        db.query(SandboxArtifact)
        .filter_by(environment_id=environment.id, artifact_type="review_report")
        .all()
    )
    assert repeated["report_persisted"] is True
    assert report.content_json["report_md"].count("## 后续验证更新（Prism 系统）") == 1
    assert len(report.content_json["evidence"]["browser_blackbox_runs"]) == 1
    for artifact in artifacts:
        artifact_bytes = base64.b64decode(artifact.content_base64, validate=True)
        assert artifact_bytes == report.content_json["report_md"].encode("utf-8")


def test_new_terminal_success_evidence_preserves_existing_ai_sandbox_report(db) -> None:
    owner = User(username="sbx_new_success_evidence_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="new success evidence", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_new_success_evidence",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="succeeded",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="e" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        result_json='{"passed":true,"summary":"测试完成"}',
    )
    db.add(environment)
    db.commit()
    published = _publish_sandbox_report(
        db,
        environment,
        {"passed": True, "execution_status": "succeeded", "ai_review_status": "complete"},
        "## 审查结论\nAI 审查确认未发现高危问题，认证与授权路径均有充分验证。\n",
    )
    task = db.get(ReviewTask, published["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()

    environment.result_json = (
        '{"passed":true,"execution_status":"succeeded",'
        '"coverage":{"tested":["/healthz"]},'
        '"evidence":{"browser_blackbox_runs":[{"status":"passed","route":"/healthz"}]}}'
    )
    db.commit()

    refreshed = _publish_terminal_sandbox_report(db, environment, execution_status="succeeded")

    db.refresh(report)
    assert refreshed["report_persisted"] is True
    assert "AI 审查确认未发现高危问题" in report.content_json["report_md"]
    assert "## 后续验证更新（Prism 系统）" in report.content_json["report_md"]
    artifact = (
        db.query(SandboxArtifact)
        .filter_by(environment_id=environment.id, artifact_type="review_report")
        .one()
    )
    assert base64.b64decode(artifact.content_base64, validate=True) == report.content_json["report_md"].encode("utf-8")
    assert report.content_json["evidence"]["browser_blackbox_runs"][0]["status"] == "passed"
    assert report.content_json["ai_review"]["status"] == "complete"


def test_terminal_incomplete_ai_state_replaces_stale_complete_report(db) -> None:
    owner = User(username="sbx_terminal_ai_incomplete_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="terminal AI incomplete", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_terminal_ai_incomplete",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="succeeded",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="f" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        result_json='{"passed":true,"execution_status":"succeeded"}',
    )
    db.add(environment)
    db.commit()

    published = _publish_sandbox_report(
        db,
        environment,
        {"passed": True, "execution_status": "succeeded", "ai_review_status": "complete"},
        "## 审查结论\n旧报告错误地将 AI 审查标记为完成。\n",
    )
    task = db.get(ReviewTask, published["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()

    environment.result_json = (
        '{"passed":true,"execution_status":"succeeded",'
        '"ai_review_status":"incomplete",'
        '"ai_review_failure_reason":"核验节点未完成",'
        '"coverage":{"tested":["/healthz"]}}'
    )
    db.commit()

    refreshed = _publish_terminal_sandbox_report(db, environment, execution_status="succeeded")

    db.refresh(task)
    db.refresh(report)
    assert refreshed and refreshed["report_persisted"] is True
    assert report.content_json["ai_review"] == {
        "status": "incomplete",
        "failure_reason": "核验节点未完成",
    }
    report_md = report.content_json["report_md"]
    assert "多 Agent AI 审查：未完成；原因：核验节点未完成。" in report_md
    assert "本报告是确定性执行结果兜底，不代表 AI 审查已完成。" in report_md
    assert "旧报告错误地将 AI 审查标记为完成" not in report_md
    assert task.summary == report_md
    artifact = db.query(SandboxArtifact).filter_by(
        environment_id=environment.id, artifact_type="review_report"
    ).one()
    report_bytes = report_md.encode("utf-8")
    assert base64.b64decode(artifact.content_base64, validate=True) == report_bytes
    assert artifact.byte_size == len(report_bytes)
    assert artifact.sha256 == hashlib.sha256(report_bytes).hexdigest()


def test_report_evidence_merge_deduplicates_and_caps_browser_history() -> None:
    previous_runs = [{"run": index} for index in range(15)]
    current_runs = [{"run": index} for index in range(10, 30)]

    merged = _merge_report_facts(
        {"browser_blackbox_runs": previous_runs},
        {"browser_blackbox_runs": current_runs},
        list_keys={"browser_blackbox_runs"},
    )

    assert len(merged["browser_blackbox_runs"]) == 20
    assert merged["browser_blackbox_runs"] == [{"run": index} for index in range(10, 30)]


def test_browser_coverage_keeps_full_nonbrowser_scope_and_caps_browser_history() -> None:
    tested = ["登录流程", {"scope": "管理员路径"}, "权限边界"] + [
        f"授权目标浏览器黑盒探测 (bbx-{index})" for index in range(30)
    ]

    merged = _append_bounded_browser_coverage(tested, "bbx-latest")

    assert merged[:3] == ["登录流程", {"scope": "管理员路径"}, "权限边界"]
    assert len(merged) == 23
    assert merged[-20:] == [f"授权目标浏览器黑盒探测 (bbx-{index})" for index in range(11, 30)] + [
        "授权目标浏览器黑盒探测 (bbx-latest)"
    ]


def test_missing_terminal_sandbox_coverage_is_explicitly_unknown() -> None:
    report = _sandbox_execution_disclosure({"passed": False, "execution_status": "expired"})
    assert "已过期" in report
    assert "执行器未提供可核验的逐项范围" in report
    assert "终止时尚未执行的剩余测试项" in report


def test_expiry_keeps_terminal_state_visible_when_report_publication_fails(db, monkeypatch) -> None:
    owner = User(username="sbx_expiry_report_failure_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    project = Project(user_id=owner.id, project_name="expiry report", language="python", status="active")
    db.add(project)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_expiry_report_failure",
        project_id=project.id,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        status="ready",
        runtime="runsc",
        image_ref="synthetic",
        source_sha256="b" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.utcnow() - timedelta(minutes=1),
        result_json='{"passed":true,"summary":"ended"}',
    )
    db.add(environment)
    db.commit()

    monkeypatch.setattr(sandbox_service, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        sandbox_service,
        "_publish_terminal_sandbox_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("report database unavailable")),
    )

    assert sandbox_service.expire_due_environments() == 1
    db.expire_all()
    row = db.query(SandboxEnvironment).filter_by(id=environment.id).one()
    assert row.status == "expired"
    assert "报告发布失败" in row.error


def test_remote_probe_exception_still_creates_failure_report(db, monkeypatch) -> None:
    from contextlib import nullcontext
    from datetime import datetime, timedelta

    from sqlalchemy.orm import Session

    from app.models.agent_capability import SandboxEnvironment
    from app.models.user import User

    owner = User(username="sbx_remote_exception_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_remote_probe_exception",
        project_id=1,
        owner_id=owner.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="blackbox",
        status="queued",
        runtime="runsc",
        image_ref="remote-only",
        source_sha256="0" * 64,
        resource_policy_json="{}",
        agent_config_json='{"remote_only": true, "worker_mode": "blackbox"}',
        execution_token="probe-lease",
        remote_target_url="https://authorized.example.test/healthz",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    db.add(environment)
    db.commit()

    monkeypatch.setattr(
        sandbox_service,
        "SessionLocal",
        lambda: Session(bind=db.get_bind(), expire_on_commit=False),
    )
    monkeypatch.setattr(sandbox_service, "usage_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(sandbox_service, "_require_execution_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sandbox_service,
        "_probe_remote_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("synthetic transport timeout")),
    )
    monkeypatch.setattr(
        sandbox_service.strategy_learning_service,
        "observe_sandbox_outcome",
        lambda *_args, **_kwargs: None,
    )

    sandbox_service._execute_environment(
        environment.id,
        source_archive_base64="",
        execution_token="probe-lease",
    )

    db.refresh(environment)
    task = (
        db.query(ReviewTask)
        .filter(ReviewTask.task_name == "沙箱黑盒测试 · sbx_remote_probe_exception")
        .one_or_none()
    )
    assert environment.status == "failed"
    assert task is not None
    assert task.status == "failed"
    assert task.coverage["verification_status"] == "failed"
    assert "执行异常" in task.coverage["reason"]
    assert task.summary and "未完成验证" in task.summary


def test_worker_execute_exception_still_creates_failure_report(db, monkeypatch) -> None:
    import base64
    import hashlib
    import io
    import zipfile
    from contextlib import nullcontext
    from datetime import datetime, timedelta

    from sqlalchemy.orm import Session

    from app.models.agent_capability import SandboxEnvironment, SandboxWorker
    from app.models.user import User

    owner = User(username="sbx_worker_exception_owner", password="x", role="user", status=1)
    db.add(owner)
    db.flush()
    source_buffer = io.BytesIO()
    with zipfile.ZipFile(source_buffer, "w") as source_zip:
        source_zip.writestr("main.py", "print('ok')\n")
    source_archive = source_buffer.getvalue()
    worker = SandboxWorker(
        code="sbx_worker_exception",
        name="Worker exception test",
        worker_type="local",
        transport="unix",
        endpoint="/tmp/unused-worker.sock",
        supported_languages_json='["python"]',
        supported_modes_json='["whitebox"]',
        runtime="runsc",
        status="healthy",
        enabled=1,
    )
    db.add(worker)
    db.flush()
    environment = SandboxEnvironment(
        public_id="sbx_worker_execute_exception",
        project_id=1,
        owner_id=owner.id,
        worker_id=worker.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="queued",
        runtime="runsc",
        image_ref="prism-sandbox-python:test",
        source_sha256=hashlib.sha256(source_archive).hexdigest(),
        source_archive_blob=source_archive,
        resource_policy_json="{}",
        agent_config_json="{}",
        execution_token="worker-exception-lease",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    db.add(environment)
    db.commit()

    monkeypatch.setattr(
        sandbox_service,
        "SessionLocal",
        lambda: Session(bind=db.get_bind(), expire_on_commit=False),
    )
    monkeypatch.setattr(sandbox_service, "usage_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(sandbox_service, "_require_execution_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_generate_deployment_patch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_generate_agent_test_cases", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sandbox_service,
        "_call_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("synthetic worker timeout")),
    )
    monkeypatch.setattr(sandbox_service, "_stop_registered_worker_requests", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sandbox_service.strategy_learning_service,
        "observe_sandbox_outcome",
        lambda *_args, **_kwargs: None,
    )

    sandbox_service._execute_environment(
        environment.id,
        source_archive_base64=base64.b64encode(source_archive).decode("ascii"),
        execution_token="worker-exception-lease",
    )

    db.refresh(environment)
    task = (
        db.query(ReviewTask)
        .filter(ReviewTask.task_name == "沙箱白盒测试 · sbx_worker_execute_exception")
        .one_or_none()
    )
    assert environment.status == "failed"
    assert task is not None
    assert task.status == "failed"
    assert task.coverage["verification_status"] == "failed"
    assert "执行器" in task.coverage["reason"]
    assert task.summary and "未完成验证" in task.summary


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


@pytest.mark.parametrize(
    ("agent_tests", "generation"),
    [
        ({"generated": 2, "passed_count": 1, "failed": 1}, {"status": "generated", "count": 2}),
        ({"generated": 1, "passed_count": 1, "failed": 0}, {"status": "generated", "count": 1}),
        ({"generated": 0, "passed_count": 0, "failed": 0}, {"status": "skipped"}),
        ({"generated": 2, "passed_count": 1, "failed": 0}, {"status": "generated", "count": 2}),
    ],
)
def test_dynamic_test_receipts_are_not_misrepresented_as_code_or_vulnerability_score(
    agent_tests, generation
):
    result = _sandbox_result_score({
        "passed": True,
        "evidence": {
            "agent_test_generation": generation,
            "agent_tests": agent_tests,
        },
    })

    assert result["score"] is None
    assert result["score_available"] is False
    assert result["score_basis"] is None
    assert result["sample"] is None
    assert "不是代码质量或漏洞风险评分" in result["unavailable_reason"]


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


def test_failed_blackbox_coverage_explains_404_without_route() -> None:
    coverage = _sandbox_verification_coverage({
        "passed": False,
        "evidence": {
            "deterministic_test_execution": {"requested_mode": "blackbox", "status": "failed"},
            "blackbox_execution": {
                "status": "failed", "route": "/", "status_code": 404,
                "route_origin": "none", "failure_kind": "route_or_dynamic_assertion",
            },
        },
    })

    assert coverage["verification_status"] == "failed"
    assert "HTTP 404" in coverage["reason"]
    assert "未找到可用应用路由" in coverage["reason"]


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


def test_exact_http_scope_receipt_matches_the_persisted_allowlist_and_never_saves_body() -> None:
    scope = [
        {"method": "GET", "path": "/", "query": {}, "expected_statuses": [200]},
        {"method": "GET", "path": "/about.php", "query": {"page": "1"}, "expected_statuses": [404]},
    ]
    receipt = {
        "executed": True,
        "passed": True,
        "basis": "exact_http_allowlist",
        "scope_enforced": True,
        "route_passed": True,
        "route": "/",
        "status_code": 200,
        "agent_assertions_passed": None,
        "request_count": 2,
        "response_count": 2,
        "all_status_assertions_configured": True,
        "requests": [
            {
                "method": "GET", "target": "/", "status_code": 200,
                "response_bytes": 12, "response_truncated": False, "elapsed_ms": 5,
                "expected_statuses": [200], "assertion_not_configured": False,
                "assertion_status": "matched", "request_passed": True,
            },
            {
                "method": "GET", "target": "/about.php?page=1", "status_code": 404,
                "response_bytes": 65_536, "response_truncated": True, "elapsed_ms": 11,
                "expected_statuses": [404], "assertion_not_configured": False,
                "assertion_status": "matched", "request_passed": True,
            },
        ],
    }

    result = _extract_blackbox_result(
        "PRISM_BLACKBOX_DONE " + json.dumps(receipt),
        expected_http_scope=scope,
    )

    assert result is not None
    assert result["status"] == "passed"
    assert result["requests"][1]["target"] == "/about.php?page=1"
    assert result["requests"][1]["response_truncated"] is True
    assert result["response_bodies_saved"] is False
    assert "body" not in result and "response_body" not in result
    assert _extract_blackbox_result(
        "PRISM_BLACKBOX_DONE " + json.dumps(receipt),
        expected_http_scope=scope,
        logs_truncated=True,
    ) is None

    for mutate in (
        lambda value: value["requests"].append(value["requests"][0]),
        lambda value: value["requests"][1].update(target="/unlisted.php"),
        lambda value: value["requests"][0].update(response_body="sensitive"),
        lambda value: value.update(request_count=1),
        lambda value: value.update(basis="route_smoke"),
    ):
        invalid = json.loads(json.dumps(receipt))
        mutate(invalid)
        output = "PRISM_BLACKBOX_DONE " + json.dumps(invalid)
        assert _extract_blackbox_result(output, expected_http_scope=scope) is None


def test_exact_http_scope_is_reported_as_partial_with_explicit_coverage_gap() -> None:
    execution = {
        "status": "passed", "passed": True, "route_passed": True, "route": "/", "status_code": 200,
        "basis": "exact_http_allowlist", "scope_enforced": True,
        "requests": [{
            "method": "GET", "target": "/", "status_code": 200, "response_bytes": 3,
            "response_truncated": False, "elapsed_ms": 2, "expected_statuses": [200],
            "assertion_not_configured": False, "assertion_status": "matched", "request_passed": True,
        }],
        "request_count": 1, "response_count": 1, "all_status_assertions_configured": True,
        "response_bodies_saved": False,
    }
    result = {
        "passed": True,
        "summary": "黑盒请求全部符合预期",
        "evidence": {
            "deterministic_test_execution": {"requested_mode": "blackbox", "status": "passed"},
            "agent_test_generation": {"status": "skipped", "mode": "blackbox"},
            "blackbox_execution": execution,
        },
    }
    coverage = _sandbox_verification_coverage(result)

    assert coverage["verification_status"] == "partial"
    assert coverage["http_scope_enforced"] is True
    report = _sandbox_execution_disclosure({"passed": True, "evidence": result["evidence"]})
    assert "## 精确 HTTP 请求范围" in report
    assert "`GET /`：HTTP 200" in report
    assert "不保存或输出 HTTP 响应正文" in report
    assert "未执行白名单之外的路径发现" in report


def test_exact_http_scope_report_skips_all_ai_calls_and_publishes_deterministic_content(monkeypatch) -> None:
    published: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    events: list[tuple[Any, ...]] = []
    commits: list[bool] = []

    def forbidden_ai_call(*_args, **_kwargs):
        raise AssertionError("精确白名单黑盒不应配置或调用 AI Agent")

    def publish(_db, _environment, conclusion, report_md, **kwargs):
        published.append((conclusion, report_md, kwargs))
        return {"report_persisted": True, "artifact_id": "artifact-1", "report_task_id": 77, "issues": 0}

    monkeypatch.setattr(sandbox_service, "configure_subagent", forbidden_ai_call)
    monkeypatch.setattr(sandbox_service, "_publish_sandbox_report", publish)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args: events.append(args))
    conclusion = {
        "passed": True,
        "execution_status": "succeeded",
        "summary": "指定 HTTP 请求符合状态断言",
        "evidence": {
            "deterministic_test_execution": {"requested_mode": "blackbox", "status": "passed"},
            "agent_test_generation": {"status": "skipped", "mode": "blackbox"},
            "blackbox_execution": {
                "basis": "exact_http_allowlist",
                "scope_enforced": True,
                "status": "passed",
                "passed": True,
                "route_passed": True,
                "route": "/",
                "status_code": 200,
                "requests": [{
                    "method": "GET", "target": "/", "status_code": 200,
                    "response_bytes": 2, "response_truncated": False, "elapsed_ms": 1,
                    "expected_statuses": [200], "assertion_not_configured": False,
                    "assertion_status": "matched", "request_passed": True,
                }],
                "request_count": 1,
                "response_count": 1,
                "all_status_assertions_configured": True,
                "response_bodies_saved": False,
            },
        },
    }
    environment = SimpleNamespace(owner_id=5, project_id=9, public_id="scope-smoke", test_mode="blackbox")
    db = SimpleNamespace(commit=lambda: commits.append(True))

    summary = sandbox_service._run_test_review_report(db, environment, conclusion)

    assert summary is not None
    assert summary["ai_review_status"] == "skipped"
    assert summary["review_mode"] == "deterministic_allowlist"
    assert summary["roles_executed"] == summary["roles_total"] == 0
    assert summary["report_persisted"] is True
    assert conclusion["ai_review_status"] == "skipped"
    assert len(published) == 1
    report_md = published[0][1]
    assert "多 Agent AI 审查：按执行策略跳过" in report_md
    assert "没有调用模型" in report_md
    assert "未执行白名单之外的路径发现" in report_md
    assert "**" not in report_md
    assert published[0][2]["_authoritative_ai_review"] is True
    assert events and events[0][1:4] == (environment, "complete", "multi_agent_review")
    assert commits == [True]


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
