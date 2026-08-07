"""沙箱多Agent审查报告入库报告中心 + agent 用例失败明细制品回归。"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.review_task import ReviewTask
from app.services.sandbox_service import (
    _artifact_documents,
    _extract_agent_test_failures,
    _publish_sandbox_report,
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
