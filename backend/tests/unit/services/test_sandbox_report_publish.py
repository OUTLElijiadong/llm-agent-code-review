"""沙箱多Agent审查报告入库报告中心 + agent 用例失败明细制品回归。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.agent_capability import SandboxArtifact, SandboxEnvironment
from app.models.project import Project
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.sandbox_service import (
    _artifact_documents,
    _build_deterministic_test_report,
    _extract_agent_test_failures,
    _publish_sandbox_report,
    _publish_terminal_sandbox_report,
)


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
        public_id="sbx_doc", agent_code="test_verifier",
        source_sha256="a" * 64, runtime="runsc",
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
        public_id="sbx_pub", project_id=1, owner_id=owner.id, test_mode="blackbox",
    )
    report_md = "## 总体结论\nok\n## 问题清单\n- SQL 注入(建议验证)\n- 越权(建议验证)\n"
    result = _publish_sandbox_report(
        db, environment, {"passed": True, "summary": "ok"}, report_md,
    )
    assert result["report_task_id"] > 0
    task = db.get(ReviewTask, result["report_task_id"])
    assert task.task_name == "沙箱黑白盒测试 · sbx_pub"
    assert task.review_type == "sandbox_test"
    assert task.score == 100
    assert task.total_issues == 2
    # 幂等:再次发布不新建
    result2 = _publish_sandbox_report(db, environment, {"passed": True}, report_md)
    assert result2["report_task_id"] == result["report_task_id"]
    assert db.query(ReviewTask).filter(ReviewTask.task_name == "沙箱黑白盒测试 · sbx_pub").count() == 1


def test_deterministic_sandbox_report_discloses_terminal_status_scope_and_errors() -> None:
    report = _build_deterministic_test_report({
        "passed": False,
        "execution_status": "stopped",
        "summary": "用户停止了黑盒测试",
        "coverage": {
            "tested": ["/health"],
            "unscanned": ["登录后接口", "上传流程"],
            "excluded": ["third_party/"],
            "skipped": ["POST /admin"],
        },
        "evidence": {"error": "worker timeout"},
    })

    assert "stopped" in report
    assert "/health" in report
    assert "登录后接口" in report
    assert "third_party/" in report
    assert "POST /admin" in report
    assert "worker timeout" in report
    assert "未完成" in report or "未通过" in report


def test_publish_sandbox_report_preserves_stopped_status_and_coverage(db) -> None:
    from app.models.review_report import ReviewReport
    from app.models.user import User

    owner = User(username="sbx_stopped_owner", password="x", role="user", status=1)
    db.add(owner)
    db.commit()
    environment = SimpleNamespace(
        public_id="sbx_stopped", project_id=1, owner_id=owner.id, test_mode="blackbox",
    )
    conclusion = {
        "passed": False,
        "execution_status": "stopped",
        "summary": "用户停止，测试未完成",
        "coverage": {"tested": ["/health"], "unscanned": ["其他路由"]},
        "evidence": {"error": "user requested stop"},
    }
    result = _publish_sandbox_report(db, environment, conclusion, "## 终态\nstopped\n")

    task = db.get(ReviewTask, result["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    assert task.status == "cancelled"
    assert task.error_message == "user requested stop"
    assert task.coverage["unscanned"] == ["其他路由"]
    assert report.content_json["execution_status"] == "stopped"
    assert report.content_json["coverage"]["tested"] == ["/health"]


def test_sandbox_report_publication_failure_is_not_silently_swallowed(db, monkeypatch) -> None:
    from app.models.user import User

    owner = User(username="sbx_publish_failure_owner", password="x", role="user", status=1)
    db.add(owner)
    db.commit()
    environment = SimpleNamespace(
        public_id="sbx_publish_failure", project_id=1, owner_id=owner.id, test_mode="blackbox",
    )

    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="database unavailable"):
        _publish_sandbox_report(db, environment, {"passed": False}, "## 测试报告\n")


def test_stopped_sandbox_terminal_state_gets_report_center_and_markdown_artifact(db) -> None:
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
        expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None),
        error="user requested stop",
        result_json=(
            '{"passed":false,"summary":"stopped","coverage":{"tested":["/health"]},'
            '"evidence":{"error":"user requested stop"}}'
        ),
    )
    db.add(environment)
    db.commit()

    published = _publish_terminal_sandbox_report(db, environment, execution_status="stopped")
    task = db.get(ReviewTask, published["report_task_id"])
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    artifact = db.query(SandboxArtifact).filter_by(
        environment_id=environment.id, artifact_type="review_report",
    ).one()

    assert task.status == "cancelled"
    assert task.error_message == "user requested stop"
    assert task.coverage["unscanned"]
    assert report.content_json["execution_status"] == "stopped"
    assert "stopped" in report.content_json["report_md"]
    assert "user requested stop" in report.content_json["report_md"]
    assert artifact.file_name.endswith(".md")


def test_missing_terminal_sandbox_coverage_is_explicitly_unknown(db) -> None:
    from app.services.sandbox_service import _sandbox_execution_disclosure

    report = _sandbox_execution_disclosure({"passed": False, "execution_status": "expired"})
    assert "expired" in report
    assert "执行器未提供可核验的逐项范围" in report
    assert "未执行的剩余测试项" in report


def test_expiry_keeps_terminal_state_visible_when_report_publication_fails(db, monkeypatch) -> None:
    from app.services import sandbox_service

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
