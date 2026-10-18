"""领域报告入口出口闭环与个人语言统计隔离，仅使用本地合成数据。"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import reports as report_api
from app.api.v1 import sandboxes as sandbox_api
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.error_handlers import register_handlers
from app.core.exceptions import ConflictError
from app.core.permission_codes import PermissionCode
from app.models.agent_capability import SandboxArtifact, SandboxEnvironment
from app.models.forum_post import ForumPost
from app.models.pentest import PentestEngagement, PentestFinding
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_issue import ReviewIssue
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import domain_report_exporter, profile_service, report_exporter, review_service
from app.services.domain_report_exporter import build_sandbox_report_markdown
from app.services.report_text import clean_report_markdown, clean_report_text
from app.services.security_report_service import persist_security_report

REPORT_MD = "## 总体结论\n隔离合成报告\n## 问题清单\n- 发现甲\n- 发现乙\n- 发现丙\n"


def test_domain_html_report_wraps_long_code_blocks():
    long_log_line = "execution-log-" + ("x" * 512)

    html, content_type = domain_report_exporter.export_markdown_report(
        "代码审查报告", f"## 执行日志\n\n```text\n{long_log_line}\n```", "html"
    )

    rendered = html.decode("utf-8")
    assert content_type == "text/html"
    assert long_log_line in rendered
    assert "pre{max-width:100%;box-sizing:border-box;overflow-x:auto;white-space:pre-wrap;" in rendered
    assert "overflow-wrap:anywhere;word-break:break-word;}" in rendered


@pytest.fixture
def export_context():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    owner = User(username="export-owner", password="x", role="super_admin", status=1)
    other = User(username="export-other", password="x", role="user", status=1)
    db.add_all([owner, other])
    db.flush()
    project = Project(user_id=owner.id, project_name="出口闭环", language="Python", status="active")
    db.add(project)
    db.commit()
    application = FastAPI()
    register_handlers(application)
    application.include_router(report_api.router, prefix="/api/reports")
    application.include_router(sandbox_api.router, prefix="/api/sandboxes")
    current = {"user": owner}

    def fixture_db():
        yield db

    application.dependency_overrides[get_db] = fixture_db
    application.dependency_overrides[get_current_user] = lambda: current["user"]
    try:
        with TestClient(application) as client:
            yield client, db, owner, other, project, current
    finally:
        db.close()
        engine.dispose()


def _domain_fixture(db, owner, project, source, *, status="success"):
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name=f"领域-{source}",
        review_type=source,
        status=status,
        total_issues=3 if source == "sandbox_test" else 2,
        score=0 if status == "failed" else (100 if source == "sandbox_test" else 84),
        score_version="pentest-v1" if source == "pentest" else None,
        coverage=(
            {
                "verification_status": "complete" if status == "success" else "failed",
                "score_available": status == "success",
                "score_basis": "fixture_source_receipt" if status == "success" else None,
                **({"coverage_status": "complete"} if status == "success" else {}),
            }
            if source == "sandbox_test"
            else None
        ),
        summary=REPORT_MD,
    )
    db.add(task)
    db.flush()
    content = {"source": source, "report_md": REPORT_MD, "public_id": f"sandbox-export-{task.id}"}
    if source == "pentest":
        engagement = PentestEngagement(
            public_id=f"pentest-export-{task.id}",
            user_id=owner.id,
            project_id=project.id,
            target_type="web",
            status="completed",
            report_task_id=task.id,
        )
        db.add(engagement)
        db.flush()
        content = {"source": "pentest", "engagement_public_id": engagement.public_id}
        for position, status in enumerate(["confirmed", "refuted"]):
            db.add(
                PentestFinding(
                    engagement_id=engagement.id,
                    finding_fingerprint=f"export-{task.id}-{position}",
                    title=f"真实领域发现-{position}",
                    severity="高",
                    category="注入",
                    status=status,
                    description="合成领域证据",
                    evidence_json='{"detail":"fixture-evidence"}',
                )
            )
    db.add(
        ReviewReport(
            task_id=task.id,
            user_id=owner.id,
            score=task.score,
            content_json=content,
            create_time=datetime.now(timezone.utc),
        )
    )
    db.commit()
    assert review_service._task_agent_release_summaries(db, task.id) == []
    return task


def _sandbox_artifact(db, owner, project, task):
    environment = SandboxEnvironment(
        public_id=f"sandbox-export-{task.id}",
        owner_id=owner.id,
        project_id=project.id,
        agent_code="test_verifier",
        status="stopped",
        source_sha256="a" * 64,
        runtime="runsc",
        image_ref="synthetic",
        purpose="test",
        language="python",
        resource_policy_json="{}",
        agent_config_json="{}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db.add(environment)
    db.flush()
    content = REPORT_MD.encode("utf-8")
    artifact = SandboxArtifact(
        environment_id=environment.id,
        artifact_type="review_report",
        file_name="sandbox-report.md",
        mime_type="text/markdown",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_ref="inline",
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    db.add(artifact)
    db.commit()
    return environment, artifact


def _security_report_fixture(db, owner, project, source):
    return db.get(
        ReviewTask,
        persist_security_report(
            db,
            owner=owner,
            project_id=project.id,
            report_kind=source,
            task_name=f"领域化-{source}",
            execution_status="success",
            result={
                "summary": "已记录源码安全结论",
                "risk_score": 44,
                "file_count": 1,
                "findings": [
                    {
                        "title": "报告内容校验标记",
                        "category": "安全漏洞",
                        "severity": "高",
                        "file_path": "src/report.py",
                        "line_number": 12,
                        "evidence": "evidence-marker-value",
                        "exploit_scenario": "攻击者可触发报告校验路径。",
                        "fix_suggestion": "先校验输入，再调用敏感操作。",
                        "verification": "plausible",
                    }
                ],
                "fullchain": {
                    "summary": "全链路阶段已生成",
                    "plain_report": {"verdict": "需要人工复核"},
                    "recon": {"framework_hints": ["FastAPI"], "hot_files": ["src/report.py"], "tainted_sink_count": 2},
                    "verification": {
                        "targets": 1,
                        "verified": 0,
                        "sandbox_used": False,
                        "sandbox_script_report_status": "pending_independent_verification",
                    },
                    "board": {
                        "facts": 2,
                        "hypotheses": 1,
                        "confirmed": 0,
                        "refuted": 1,
                        "intents": 1,
                        "open_intents": 1,
                    },
                },
                "audit_board": {"summary": "**协作记录** *已汇总*", "confirmed": []},
            },
            coverage={"coverage_status": "complete", "scanned_file_count": 1, "total_file_count": 1},
        ),
    )


@pytest.mark.parametrize("source,count", [("sandbox_test", 3), ("pentest", 2)])
@pytest.mark.parametrize("endpoint", ["generate", "download"])
def test_domain_json_export_matches_detail_without_standard_issue_fabrication(export_context, source, count, endpoint):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, source)
    if source == "sandbox_test":
        # This fixture represents a verified full-coverage sandbox run.
        task.coverage = {
            "coverage_status": "complete",
            "verification_status": "complete",
            "score_available": True,
            "score_basis": "fixture_source_receipt",
        }
        db.commit()
    if endpoint == "generate":
        response = client.post("/api/reports/generate", json={"task_id": task.id, "format": "json"})
    else:
        response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 200
    exported = response.json()
    detail = client.get(f"/api/reports/{task.id}").json()["data"]

    assert exported["statistics"]["total_issues"] == detail["stats"]["total_issues"] == count
    assert exported["task_info"]["total_issues"] == count
    # Sandbox execution receipts never stand in for code quality or risk scores.
    expected_score = None
    assert exported["score"] == detail["task"]["score"] == expected_score
    assert exported["source"]["type"] == source
    assert "issues" not in exported
    assert db.query(ReviewIssue).filter_by(task_id=task.id).count() == 0
    if source == "sandbox_test":
        report_md = exported["domain_data"]["report_md"]
        assert "总体结论" in report_md
        assert "发现甲" in report_md and "发现乙" in report_md and "发现丙" in report_md
        assert "**" not in report_md
    else:
        assert len(exported["domain_data"]["findings"]) == count
        assert exported["statistics"]["refuted"] == 1
        assert exported["statistics"]["severity"] == {"高": 1}
        assert exported["domain_data"]["findings"][0]["evidence"]["detail"] == "fixture-evidence"


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
@pytest.mark.parametrize(
    "endpoint,format",
    [
        ("generate", "html"),
        ("generate", "pdf"),
        ("generate", "word"),
        ("download", "html"),
        ("download", "pdf"),
        ("download", "word"),
        ("legacy", "pdf"),
        ("legacy", "word"),
        ("preview", "html"),
    ],
)
def test_domain_report_formats_render_the_same_source_data(export_context, source, endpoint, format):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, source)
    if endpoint == "generate":
        response = client.post("/api/reports/generate", json={"task_id": task.id, "format": format})
    elif endpoint == "download":
        response = client.get(f"/api/reports/tasks/{task.id}/export?format={format}")
    elif endpoint == "legacy":
        response = client.get(f"/api/reports/{task.id}/export/{format}")
    else:
        response = client.get(f"/api/reports/tasks/{task.id}")

    assert response.status_code == 200
    if format == "html":
        assert response.headers["content-type"].startswith("text/html")
        assert task.task_name in response.text
        if source == "sandbox_test":
            assert "发现甲" in response.text
        else:
            assert "真实领域发现-0" in response.text
    elif format == "pdf":
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.content.startswith(b"%PDF")
        assert "content-disposition" in response.headers
    else:
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert "content-disposition" in response.headers
        with ZipFile(BytesIO(response.content)) as document:
            xml = document.read("word/document.xml").decode("utf-8")
        assert task.task_name in xml
        if source == "sandbox_test":
            assert "发现甲" in xml
        else:
            assert "真实领域发现-0" in xml


@pytest.mark.parametrize("source", ["security_scan", "fullchain_audit"])
@pytest.mark.parametrize("endpoint", ["generate", "download"])
@pytest.mark.parametrize("format", ["json", "html", "pdf", "word"])
def test_security_and_fullchain_reports_export_the_saved_complete_body(
    export_context, monkeypatch, source, endpoint, format
):
    client, db, owner, _other, project, _current = export_context
    task = _security_report_fixture(db, owner, project, source)
    pdf_text: list[str] = []
    original_append = domain_report_exporter._append_markdown_elements

    def capture_pdf_body(elements, markdown, styles):
        original_append(elements, markdown, styles)
        pdf_text.extend(element.getPlainText() for element in elements if hasattr(element, "getPlainText"))

    monkeypatch.setattr(domain_report_exporter, "_append_markdown_elements", capture_pdf_body)
    if endpoint == "generate":
        response = client.post("/api/reports/generate", json={"task_id": task.id, "format": format})
    else:
        response = client.get(f"/api/reports/tasks/{task.id}/export?format={format}")

    assert response.status_code == 200
    if format == "json":
        payload = response.json()
        assert payload["source"]["type"] == source
        assert "报告内容校验标记" in payload["domain_data"]["report_md"]
        assert "侦察阶段" in payload["domain_data"]["report_md"]
        assert "验证阶段" in payload["domain_data"]["report_md"]
        assert "协作黑板统计" in payload["domain_data"]["report_md"]
        assert "待独立核验" in payload["domain_data"]["report_md"]
        assert "pending_independent_verification" not in payload["domain_data"]["report_md"]
        assert "**" not in json.dumps(payload["domain_data"], ensure_ascii=False)
        assert "*已汇总*" not in json.dumps(payload["domain_data"], ensure_ascii=False)
    elif format == "html":
        assert "报告内容校验标记" in response.text
        assert "FastAPI" in response.text
        assert "待独立核验" in response.text
        assert "pending_independent_verification" not in response.text
    elif format == "pdf":
        assert response.content.startswith(b"%PDF")
        assert "attachment" in response.headers.get("content-disposition", "")
        visible_pdf_text = "\n".join(pdf_text)
        assert "报告内容校验标记" in visible_pdf_text
        assert "FastAPI" in visible_pdf_text
        assert "待独立核验" in visible_pdf_text
        assert "pending_independent_verification" not in visible_pdf_text
    else:
        with ZipFile(BytesIO(response.content)) as document:
            xml = document.read("word/document.xml").decode("utf-8")
        assert "报告内容校验标记" in xml
        assert "FastAPI" in xml
        assert "待独立核验" in xml
        assert "pending_independent_verification" not in xml


def test_security_snapshot_source_mismatch_is_hidden_from_list_and_detail(export_context):
    client, db, owner, _other, project, _current = export_context
    task = _security_report_fixture(db, owner, project, "security_scan")
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    snapshot.content_json = {**snapshot.content_json, "source": "fullchain_audit"}
    db.commit()

    listing = client.get("/api/reports?page=1&page_size=100")
    detail = client.get(f"/api/reports/{task.id}")
    exported = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert listing.status_code == 200
    assert all(item["task_id"] != task.id for item in listing.json()["data"]["items"])
    assert detail.status_code == exported.status_code == 409


@pytest.mark.parametrize("report_body", [" \n  \t", "\u3000", "\u00a0", 123])
def test_security_snapshot_with_invalid_body_is_hidden_from_list_and_detail(export_context, report_body):
    client, db, owner, _other, project, _current = export_context
    task = _security_report_fixture(db, owner, project, "security_scan")
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    snapshot.content_json = {**snapshot.content_json, "report_md": report_body}
    db.commit()

    listing = client.get("/api/reports?page=1&page_size=100")
    detail = client.get(f"/api/reports/{task.id}")
    exported = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert listing.status_code == 200
    assert all(item["task_id"] != task.id for item in listing.json()["data"]["items"])
    assert detail.status_code == exported.status_code == 409


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_domain_reports_without_readable_source_are_hidden_from_every_outlet(export_context, source):
    client, db, owner, _other, project, _current = export_context
    task = _domain_fixture(db, owner, project, source)
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    if source == "sandbox_test":
        snapshot.content_json = {**snapshot.content_json, "report_md": " \n\t"}
    else:
        db.query(PentestEngagement).filter_by(report_task_id=task.id).delete()
    db.commit()

    listing = client.get("/api/reports?page=1&page_size=100")
    detail = client.get(f"/api/reports/{task.id}")
    exported = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 0
    assert all(item["task_id"] != task.id for item in listing.json()["data"]["items"])
    assert detail.status_code == exported.status_code == 409


@pytest.mark.parametrize(
    ("coverage", "stored_score"),
    [
        ({"coverage_status": "partial", "score_available": False}, 0),
        ({"coverage_status": "partial", "score_available": True}, 50),
        ({"coverage_status": "complete", "score_available": True}, 100),
    ],
)
def test_legacy_sandbox_pass_rate_is_not_exposed_as_quality_or_risk_score(
    export_context, coverage, stored_score
):
    client, db, owner, _other, project, _current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test")
    task.score = stored_score
    task.coverage = {"verification_status": coverage["coverage_status"], **coverage}
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    snapshot.score = stored_score
    snapshot.content_json = {
        **snapshot.content_json,
        "coverage": {
            **task.coverage,
            "score_basis": "agent_dynamic_test_pass_rate",
            "score_sample": {"generated": 1, "passed": 1, "failed": 0},
        },
        "score_available": coverage["score_available"],
    }
    db.commit()

    listing = client.get("/api/reports?page=1&page_size=100").json()["data"]
    row = next(item for item in listing["items"] if item["task_id"] == task.id)
    detail = client.get(f"/api/reports/{task.id}").json()["data"]
    exported = client.get(f"/api/reports/tasks/{task.id}/export?format=json").json()

    assert row["score"] is None
    assert detail["task"]["score"] is None
    assert exported["score"] is None
    assert detail["task"]["coverage"]["coverage_status"] == coverage["coverage_status"]
    assert detail["task"]["coverage"]["score_available"] is False
    assert detail["task"]["coverage"]["score_basis"] is None
    assert detail["task"]["coverage"]["score_sample"] is None
    assert "不是代码质量或漏洞风险评分" in detail["task"]["coverage"]["score_unavailable_reason"]
    assert exported["domain_data"]["coverage"]["score_available"] is False
    assert exported["domain_data"]["coverage"]["score_basis"] is None


def test_report_cleanup_preserves_link_targets_and_single_character_emphasis():
    report = (
        "## 证据\n\n"
        "参考 [NVD 公告](https://nvd.nist.gov/vuln/detail/CVE-2026-12345) 和 "
        "[公告](https://example.org/advisory_(v2))，严重度为 *高*。\n\n"
        "计算 `result = a*b*c` 与 `power = 2 ** 3 ** 4`。\n\n"
        "```python\npattern = '*'\n```\n"
    )

    cleaned = clean_report_markdown(report)
    plain_text = clean_report_text(report)

    assert "NVD 公告（https://nvd.nist.gov/vuln/detail/CVE-2026-12345）" in cleaned
    assert "公告（https://example.org/advisory_(v2)）" in cleaned
    assert "严重度为 高。" in cleaned
    assert "`result = a*b*c`" in cleaned
    assert "`power = 2 ** 3 ** 4`" in cleaned
    assert "公告（https://example.org/advisory_(v2)）" in plain_text
    assert "result = a*b*c" in plain_text
    assert "power = 2 ** 3 ** 4" in plain_text
    assert "*高*" not in cleaned
    assert "pattern = '*'" in cleaned


def test_report_cleanup_removes_unclosed_bold_prefix_without_changing_fenced_code():
    report = "**3.- 位置：侦察阶段\n- 证据：entrypoints 为空\n\n```text\n**literal evidence**\n```\n"

    cleaned = clean_report_markdown(report)

    assert "**3.-" not in cleaned
    assert "3.- 位置：侦察阶段" in cleaned
    assert "**literal evidence**" in cleaned


def test_report_cleanup_hides_internal_worker_urls_and_httpx_help_links():
    report = (
        "执行异常：Client error '400 Bad Request' for url 'http://sandbox/execute' "
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400; "
        "Client error '404 Not Found' for url 'http://sandbox/stop' "
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404。"
    )

    cleaned = clean_report_markdown(report)

    assert "沙箱执行接口返回 HTTP 400 Bad Request" in cleaned
    assert "沙箱停止接口返回 HTTP 404 Not Found" in cleaned
    assert "http://sandbox" not in cleaned
    assert "developer.mozilla.org" not in cleaned


def test_sandbox_source_markdown_is_cleaned_while_fenced_code_is_preserved(export_context):
    client, db, owner, _other, project, _current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test")
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    snapshot.content_json = {
        **snapshot.content_json,
        "report_md": (
            "## **报告结论**\n\n* **需要关注**\n\n"
            "**3.- 位置：侦察阶段\n- 证据：entrypoints 为空\n\n"
            "```python\npattern = '*'\n```\n"
        ),
    }
    db.commit()

    json_response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    html_response = client.get(f"/api/reports/tasks/{task.id}/export?format=html")

    assert json_response.status_code == html_response.status_code == 200
    assert "**" not in json_response.text
    assert "**" not in html_response.text
    assert "pattern = '*'" in json_response.json()["domain_data"]["report_md"]
    assert "3.- 位置：侦察阶段" in json_response.json()["source"]["domain_report_md"]
    assert "**" not in json_response.json()["source"]["domain_report_md"]
    assert "**" not in json_response.json()["domain_markdown"]
    assert "报告结论" in html_response.text


def test_sandbox_report_detail_cleans_historical_worker_errors_and_unreliable_duration(export_context):
    client, db, owner, _other, project, _current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test", status="failed")
    task.start_time = datetime.now(timezone.utc) - timedelta(days=11)
    task.end_time = datetime.now(timezone.utc)
    task.duration_ms = 11 * 24 * 60 * 60 * 1000
    task.error_message = (
        "Client error '400 Bad Request' for url 'http://sandbox/execute' "
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400"
    )
    task.coverage = {"verification_status": "failed", "errors": [task.error_message]}
    task.summary = task.error_message
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    snapshot.content_json = {**snapshot.content_json, "report_md": task.error_message}
    db.commit()

    response = client.get(f"/api/reports/{task.id}")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["task"]["duration_ms"] is None
    assert "沙箱执行接口返回 HTTP 400 Bad Request" in body["task"]["error_message"]
    assert "http://sandbox" not in response.text
    assert "developer.mozilla.org" not in response.text
    assert body["source"]["duration_note"] == "历史环境总运行时间不能代表本次测试耗时；单次耗时未可靠记录。"


def test_sandbox_report_detail_exposes_measured_execution_duration(export_context):
    client, db, owner, _other, project, _current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test", status="success")
    task.duration_ms = 1234
    task.coverage = {
        "verification_status": "complete",
        "duration_source": "measured_execution",
    }
    db.commit()

    response = client.get(f"/api/reports/{task.id}")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["task"]["duration_ms"] == 1234
    assert "duration_note" not in body["source"]


@pytest.mark.parametrize("source", ["security_scan", "fullchain_audit"])
def test_security_and_fullchain_report_preview_uses_the_saved_report_body(export_context, source):
    client, db, owner, _other, project, _current = export_context
    task = _security_report_fixture(db, owner, project, source)

    response = client.get(f"/api/reports/tasks/{task.id}")

    assert response.status_code == 200
    assert "报告内容校验标记" in response.text
    assert "侦察阶段" in response.text
    assert "验证阶段" in response.text
    assert "协作黑板统计" in response.text


@pytest.mark.parametrize("source", ["security_scan", "fullchain_audit"])
def test_security_json_export_cleans_domain_markdown(export_context, source):
    client, db, owner, _other, project, _current = export_context
    task = _security_report_fixture(db, owner, project, source)
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    snapshot.content_json = {
        **snapshot.content_json,
        "report_md": "## **审计结论**\n\n* **修复建议**：补充权限校验。\n",
    }
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert response.status_code == 200
    payload = response.json()
    assert "审计结论" in payload["domain_markdown"]
    assert "修复建议" in payload["domain_markdown"]
    assert "**" not in payload["domain_markdown"]
    assert "* **" not in payload["domain_markdown"]
    assert "**" not in json.dumps(payload["domain_data"], ensure_ascii=False)


def test_legacy_sandbox_report_without_coverage_preserves_score_and_marks_unknown_scope(export_context):
    client, db, _owner, _other, project, _current = export_context
    task = _domain_fixture(db, _owner, project, "sandbox_test")
    task.coverage = None
    db.commit()

    detail = client.get(f"/api/reports/{task.id}")
    exported = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    listing = client.get("/api/reports?page=1&page_size=100")

    assert detail.status_code == exported.status_code == listing.status_code == 200
    # The historical database score remains stored, but unknown coverage must not
    # present that score as a verified result in any user-facing report surface.
    assert task.score == 100
    assert detail.json()["data"]["task"]["score"] is None
    assert detail.json()["data"]["task"]["coverage"]["coverage_status"] == "unknown"
    assert detail.json()["data"]["stats"]["score"] is None
    assert exported.json()["score"] is None
    listed = next(item for item in listing.json()["data"]["items"] if item["task_id"] == task.id)
    assert listed["score"] is None
    assert listed["coverage"]["coverage_status"] == "unknown"
    assert listed["coverage"]["score_available"] is False


def test_legacy_nested_mode_scopes_detail_list_and_json_export_without_mutating_snapshot(export_context, monkeypatch):
    client, db, owner, _other, project, _current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test")
    snapshot = db.query(ReviewReport).filter_by(task_id=task.id, user_id=owner.id).one()
    raw_report = (
        "## 总体结论\n黑盒探测发现 /debug 暴露内部信息（白盒未执行）。\n"
        "## 白盒结果\n白盒测试未执行，因此缺少白盒证据。\n"
        "## 问题清单\n### [高] 测试覆盖缺口\n"
        "证据：白盒测试未执行，因此缺少白盒证据。\n"
    )
    snapshot.content_json = {
        "source": "sandbox_test",
        "report_md": raw_report,
        "public_id": f"sandbox-export-{task.id}",
        "coverage": {"requested_mode": None, "verification_status": "partial"},
        "evidence": {"deterministic_test_execution": {"requested_mode": "blackbox"}},
        "findings": [
            {"title": "测试覆盖缺口", "severity": "高", "description": "白盒测试未执行，因此缺少白盒证据。"},
            {"title": "黑盒 /debug 暴露内部信息", "severity": "高", "description": "可匿名读取（白盒未执行）。"},
        ],
    }
    db.commit()
    native_export_calls = []
    monkeypatch.setattr(
        "app.services.report_service._sandbox_native_exports",
        lambda *_args: native_export_calls.append(True) or [{"download_api": "/raw-original"}],
    )

    detail = client.get(f"/api/reports/{task.id}")
    listing = client.get("/api/reports?page=1&page_size=100")
    exported = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert detail.status_code == listing.status_code == exported.status_code == 200
    detail_payload = detail.json()["data"]
    listed = next(item for item in listing.json()["data"]["items"] if item["task_id"] == task.id)
    export_payload = exported.json()
    assert detail_payload["task"]["coverage"]["requested_mode"] == "blackbox"
    assert listed["coverage"]["requested_mode"] == "blackbox"
    assert export_payload["domain_data"]["coverage"]["requested_mode"] == "blackbox"
    for displayed in (
        detail_payload["source"]["domain_report_md"],
        export_payload["domain_markdown"],
    ):
        assert "黑盒探测发现 /debug 暴露内部信息" in displayed
        assert "白盒测试未执行，因此缺少白盒证据" not in displayed
        assert "### [高] 测试覆盖缺口" not in displayed
    assert len(detail_payload["source"]["domain_findings"]) == 1
    assert len(export_payload["domain_data"]["findings"]) == 1
    assert "白盒未执行" not in detail_payload["source"]["domain_findings"][0]["description"]
    assert export_payload["native_exports"] == []
    assert native_export_calls == []
    assert (
        detail_payload["stats"]["total_issues"]
        == listed["total_issues"]
        == export_payload["statistics"]["total_issues"]
        == 0
    )
    db.refresh(snapshot)
    assert snapshot.content_json["report_md"] == raw_report
    assert snapshot.content_json["coverage"]["requested_mode"] is None


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_generic_exporter_cannot_bypass_source_gate(source):
    with pytest.raises(ConflictError):
        report_exporter.export_to_dict({"id": 1, "review_type": source, "total_issues": 3}, [], REPORT_MD, 100)


def test_sandbox_remote_blackbox_report_uses_real_persisted_evidence_fields():
    markdown = build_sandbox_report_markdown(
        {
            "report_md": "测试报告正文",
            "execution_status": "success",
            "verification_status": "partial",
            "coverage": {"verification_status": "partial"},
            "evidence": {
                "remote_blackbox": {
                    "target_origin": "https://example.test/",
                    "resolved_ip": "203.0.113.10",
                    "status_code": 302,
                    "latency_ms": 27,
                    "redirect_location": "https://www.example.test/",
                    "content_type": "text/html",
                    "security_headers": {"strict-transport-security": "max-age=31536000"},
                    "missing_security_headers": ["content-security-policy"],
                    "body_bytes_sampled": 65536,
                }
            },
        },
        task_name="远程有限检查",
        project_name="示例项目",
    )

    for expected in (
        "授权目标：https://example.test/",
        "请求方法：GET",
        "203.0.113.10",
        "HTTP 状态码：302",
        "响应耗时毫秒：27",
        "重定向目标：https://www.example.test/",
        "strict-transport-security",
        "content-security-policy",
        "读取响应字节数：65536",
    ):
        assert expected in markdown


def test_sandbox_report_localizes_status_prose_but_preserves_code_evidence():
    markdown = build_sandbox_report_markdown(
        {
            "report_md": (
                "确定性执行结论：passed\n"
                "AI 动态用例=skipped\n"
                "```text\n"
                "确定性执行结论: passed\n"
                "```"
            ),
            "execution_status": "success",
            "verification_status": "partial",
            "coverage": {"verification_status": "partial"},
        },
        task_name="状态本地化回归",
        project_name="合成项目",
    )

    assert "确定性执行结论：已通过" in markdown
    assert "AI 动态用例=未执行" in markdown
    assert "```text\n确定性执行结论: passed\n```" in markdown
    assert "执行状态：执行完成" in markdown
    assert "验证状态：部分核验" in markdown


def test_sandbox_report_detail_discloses_missing_original_body_without_failing():
    markdown = build_sandbox_report_markdown(
        {
            "report_md": "",
            "execution_status": "success",
            "verification_status": "unknown",
            "coverage": {"verification_status": "unknown", "unscanned": ["原始报告未留存"]},
        },
        task_name="历史正文缺失",
        project_name="合成项目",
    )

    assert "原始测试报告正文缺失" in markdown
    assert "不代表已验证具体测试项" in markdown
    assert "执行状态：执行完成" in markdown
    assert "原始报告未留存" in markdown


def test_sandbox_export_links_to_integrity_checked_native_artifact(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test")
    environment, artifact = _sandbox_artifact(db, owner, project, task)
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert response.status_code == 200
    native = response.json()["native_exports"][0]
    assert native["download_api"] == f"/api/sandboxes/{environment.public_id}/artifacts/{artifact.id}"
    downloaded = client.get(native["download_api"])
    assert downloaded.status_code == 200
    assert downloaded.content.decode("utf-8") == REPORT_MD
    assert hashlib.sha256(downloaded.content).hexdigest() == native["sha256"]


@pytest.mark.parametrize("task_status", ["success", "failed"])
def test_sandbox_report_artifact_requires_report_scope_and_view_permission(export_context, task_status):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test", status=task_status)
    environment, artifact = _sandbox_artifact(db, owner, project, task)
    db.add(ProjectMember(project_id=project.id, user_id=other.id, role_in_project="reviewer"))
    normal_content = b'{"kind":"ordinary-result"}'
    normal_artifact = SandboxArtifact(
        environment_id=environment.id,
        artifact_type="result",
        file_name="result.json",
        mime_type="application/json",
        byte_size=len(normal_content),
        sha256=hashlib.sha256(normal_content).hexdigest(),
        storage_ref="inline",
        content_base64=base64.b64encode(normal_content).decode("ascii"),
    )
    db.add(normal_artifact)
    db.commit()

    current["user"] = other
    peer_environment = client.get(f"/api/sandboxes/{environment.public_id}")
    assert peer_environment.status_code == 200
    peer_artifacts = peer_environment.json()["data"]["artifacts"]
    assert [item["artifact_type"] for item in peer_artifacts] == ["result"]
    assert peer_artifacts[0]["download_allowed"] is True
    ordinary_download = client.get(f"/api/sandboxes/{environment.public_id}/artifacts/{normal_artifact.id}")
    assert ordinary_download.status_code == 200
    assert ordinary_download.content == normal_content

    peer_download = client.get(f"/api/sandboxes/{environment.public_id}/artifacts/{artifact.id}")
    assert peer_download.status_code == 404

    view_role = Role(name="报告查看普通用户", code="user", status="active", is_builtin=0)
    view_permission = Permission(code="report:view", name="查看审查报告", module="report", type="api")
    db.add_all([view_role, view_permission])
    db.flush()
    db.add_all(
        [
            RolePermission(role_id=view_role.id, permission_id=view_permission.id),
            UserRole(user_id=other.id, role_id=view_role.id),
        ]
    )
    db.commit()
    still_peer_environment = client.get(f"/api/sandboxes/{environment.public_id}")
    assert still_peer_environment.status_code == 200
    assert all(item["artifact_type"] != "review_report" for item in still_peer_environment.json()["data"]["artifacts"])
    peer_with_view_download = client.get(f"/api/sandboxes/{environment.public_id}/artifacts/{artifact.id}")
    assert peer_with_view_download.status_code == 404

    task.user_id = other.id
    environment.owner_id = other.id
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    report.user_id = other.id
    db.query(UserRole).filter_by(user_id=other.id, role_id=view_role.id).delete()
    db.commit()
    owner_environment = client.get(f"/api/sandboxes/{environment.public_id}")
    assert all(item["artifact_type"] != "review_report" for item in owner_environment.json()["data"]["artifacts"])
    owner_without_view_download = client.get(f"/api/sandboxes/{environment.public_id}/artifacts/{artifact.id}")
    assert owner_without_view_download.status_code == 403

    db.add(UserRole(user_id=other.id, role_id=view_role.id))
    db.commit()
    owner_with_view_environment = client.get(f"/api/sandboxes/{environment.public_id}")
    report_item = next(
        item
        for item in owner_with_view_environment.json()["data"]["artifacts"]
        if item["artifact_type"] == "review_report"
    )
    assert report_item["download_allowed"] is True
    owner_with_view_download = client.get(f"/api/sandboxes/{environment.public_id}/artifacts/{artifact.id}")
    assert owner_with_view_download.status_code == 200
    assert owner_with_view_download.content.decode("utf-8") == REPORT_MD

    current["user"] = owner
    admin_environment = client.get(f"/api/sandboxes/{environment.public_id}")
    assert any(item["artifact_type"] == "review_report" for item in admin_environment.json()["data"]["artifacts"])
    admin_download = client.get(f"/api/sandboxes/{environment.public_id}/artifacts/{artifact.id}")
    assert admin_download.status_code == 200
    assert admin_download.content.decode("utf-8") == REPORT_MD


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_missing_domain_evidence_is_not_an_empty_successful_export(export_context, source):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, source)
    if source == "sandbox_test":
        db.query(ReviewReport).filter_by(task_id=task.id).delete()
    else:
        db.query(PentestEngagement).filter_by(report_task_id=task.id).update({"report_task_id": None})
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 409
    assert response.json()["message"]


def test_failed_sandbox_json_hides_score_when_coverage_is_unknown(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test", status="failed")
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert response.status_code == 200
    assert response.json()["statistics"]["total_issues"] == 3
    assert response.json()["task_info"]["status"] == "failed"
    assert response.json()["score"] is None
    assert response.json()["domain_data"]["coverage"]["coverage_status"] == "unknown"


@pytest.mark.parametrize("status", ["failed", "cancelled", "expired"])
def test_terminal_code_review_reports_are_listed_readable_and_export_state(export_context, status):
    client, db, owner, other, project, current = export_context
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name=f"终态代码审查-{status}",
        review_type="standard",
        status=status,
        total_files=3,
        processed_files=1,
        total_issues=0,
        score=0,
        summary="任务未完成，当前结果不代表审查通过。",
        error_message="worker timeout",
        coverage={
            "stage": status,
            "completed_files": 1,
            "total_files": 3,
            "excluded_files": [{"file_name": "vendor/generated.py", "reason": "排除规则"}],
            "files": {"1": {"file_name": "src/partial.py", "status": "complete"}},
        },
    )
    db.add(task)
    db.commit()

    listed = client.get("/api/reports").json()["data"]["items"]
    entry = next(item for item in listed if item["task_id"] == task.id)
    detail_response = client.get(f"/api/reports/{task.id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()["data"]
    exported_response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert exported_response.status_code == 200
    exported = exported_response.json()
    html_response = client.get(f"/api/reports/tasks/{task.id}?template_type=detailed")

    assert entry["status"] == status
    assert entry["error_message"] == "worker timeout"
    assert entry["coverage"]["completed_files"] == 1
    assert detail["task"]["status"] == status
    assert detail["task"]["error_message"] == "worker timeout"
    assert detail["task"]["coverage"]["excluded_files"][0]["file_name"] == "vendor/generated.py"
    assert exported["task_info"]["status"] == status
    assert exported["task_info"]["error_message"] == "worker timeout"
    assert exported["task_info"]["coverage"]["total_files"] == 3
    assert html_response.status_code == 200
    assert status in html_response.text
    assert "worker timeout" in html_response.text
    assert "vendor/generated.py" in html_response.text


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_terminal_sandbox_domain_reports_are_readable_and_exportable(export_context, status):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test", status=status)
    task.score = 0
    task.error_message = "执行在授权窗口结束前被停止"
    task.coverage = {"stage": status, "tested": ["/health"], "unscanned": ["其余测试项"]}
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    report.content_json = {
        **report.content_json,
        "execution_status": status,
        "error_message": task.error_message,
        "coverage": task.coverage,
    }
    db.commit()

    detail = client.get(f"/api/reports/{task.id}")
    exported = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert detail.status_code == 200
    assert detail.json()["data"]["task"]["status"] == status
    assert detail.json()["data"]["task"]["coverage"]["unscanned"] == ["其余测试项"]
    assert exported.status_code == 200
    assert exported.json()["task_info"]["status"] == status
    assert exported.json()["domain_data"]["execution_status"] == status
    assert exported.json()["domain_data"]["error_message"] == task.error_message
    assert exported.json()["domain_data"]["coverage"]["unscanned"] == ["其余测试项"]


@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
def test_personal_language_distribution_intersects_visible_projects(db, role):
    owner = User(username=f"language-{role}", password="x", role=role, status=1)
    other = User(username=f"language-other-{role}", password="x", role="user", status=1)
    db.add_all([owner, other])
    db.flush()
    for name, language, status, user_id in [
        ("本人活动", "Python", "active", owner.id),
        ("本人归档", "Go", "archived", owner.id),
        ("隔离测试", "Rust", "quarantined", owner.id),
        ("删除项目", "Java", "deleted", owner.id),
        ("他人项目", "JavaScript", "active", other.id),
    ]:
        project = Project(user_id=user_id, project_name=name, language=language, status=status)
        db.add(project)
        db.flush()
        db.add(ProjectMember(project_id=project.id, user_id=owner.id))
    db.add(ForumPost(user_id=owner.id, title="合成记录", content="文本", status="normal"))
    db.commit()

    profile = profile_service.refresh_implicit(db, owner.id, force=True)
    derived = json.loads(profile.derived_stats)

    assert derived["languages"] == {"Python": 1, "Go": 1}
    assert "Rust" not in profile.derived_summary
    assert derived["forum_posts"] == 1


def _grant_report_permissions(db, user, *, role_code, domain_view=False, export_json=True):
    assert role_code in {"user", "reviewer"}
    user.role = role_code
    role = db.query(Role).filter_by(code=role_code).one_or_none()
    if role is None:
        role = Role(
            name="评审员" if role_code == "reviewer" else "普通用户",
            code=role_code,
            status="active",
            is_builtin=1,
        )
        db.add(role)
        db.flush()
    if db.query(UserRole).filter_by(user_id=user.id, role_id=role.id).count() == 0:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    codes = [PermissionCode.REPORT_VIEW]
    if export_json:
        codes.append(PermissionCode.REPORT_EXPORT_JSON)
    if domain_view:
        codes.append(PermissionCode.PENTEST_VIEW)
    for code in codes:
        permission = db.query(Permission).filter_by(code=code).first()
        if permission is None:
            permission = Permission(code=code, name=code, module="report")
            db.add(permission)
            db.flush()
        if db.query(RolePermission).filter_by(role_id=role.id, permission_id=permission.id).count() == 0:
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.commit()


@pytest.mark.parametrize("domain_view", [False, True])
def test_pentest_json_requires_existing_source_view_permission(export_context, domain_view):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "pentest")
    _grant_report_permissions(db, owner, role_code="reviewer", domain_view=domain_view)

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == (200 if domain_view else 404)


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_domain_export_preserves_format_permission_and_report_owner_scope(export_context, source):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, source)
    _grant_report_permissions(db, owner, role_code="reviewer", domain_view=True, export_json=False)
    assert client.get(f"/api/reports/tasks/{task.id}/export?format=json").status_code == 403
    _grant_report_permissions(db, other, role_code="user", domain_view=True)
    current["user"] = other
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 404
    assert "native_exports" not in response.json()


def test_corrupt_native_artifact_is_not_advertised_as_downloadable(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test")
    environment, artifact = _sandbox_artifact(db, owner, project, task)
    artifact.content_base64 = base64.b64encode(b"tampered").decode("ascii")
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 200
    assert response.json()["statistics"]["total_issues"] == 3
    assert response.json()["native_exports"] == []


@pytest.mark.parametrize("field", ["user_id", "project_id"])
def test_pentest_export_rejects_cross_owner_or_project_link(export_context, field):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "pentest")
    engagement = db.query(PentestEngagement).filter_by(report_task_id=task.id).one()
    setattr(engagement, field, getattr(engagement, field) + 1000)
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 409
    assert "domain_data" not in response.json()


def test_empty_real_pentest_export_remains_valid_without_unknown_coverage_score(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "pentest")
    engagement = db.query(PentestEngagement).filter_by(report_task_id=task.id).one()
    db.query(PentestFinding).filter_by(engagement_id=engagement.id).delete()
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 200
    assert response.json()["statistics"]["total_issues"] == 0
    assert response.json()["domain_data"]["findings"] == []
    assert response.json()["score"] is None


def test_domain_pdf_export_uses_real_source_report_in_production_response(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test")

    response = client.get(f"/api/reports/{task.id}/export/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")


def test_changing_domain_findings_cannot_export_inconsistent_count(export_context, monkeypatch):
    from app.services import pentest_service

    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "pentest")
    original = pentest_service.get_engagement_detail

    def changed_detail(*args, **kwargs):
        data = original(*args, **kwargs)
        data["findings"] = data["findings"][:-1]
        return data

    monkeypatch.setattr(pentest_service, "get_engagement_detail", changed_detail)
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 409
    assert response.json()["code"] == 40943
    assert response.json()["retryable"] is True


@pytest.mark.parametrize("source", ["standard", "discuss"])
@pytest.mark.parametrize("endpoint", ["generate", "download"])
def test_review_issue_exports_keep_standard_contract_and_match_report_detail(export_context, source, endpoint):
    client, db, owner, other, project, current = export_context
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name=f"兼容-{source}",
        review_type=source,
        status="success",
        total_issues=99,
        score=0,
    )
    db.add(task)
    db.flush()
    db.add(ReviewIssue(task_id=task.id, severity="高", issue_type="安全漏洞", description="标准问题"))
    db.commit()

    if endpoint == "generate":
        response = client.post("/api/reports/generate", json={"task_id": task.id, "format": "json"})
    else:
        response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 200
    data = response.json()
    detail = client.get(f"/api/reports/{task.id}").json()["data"]

    assert data["statistics"]["total_issues"] == detail["stats"]["total_issues"] == 1
    assert data["task_info"]["total_issues"] == len(data["issues"]) == 1
    assert data["score"] == detail["task"]["score"]
    assert "domain_data" not in data


@pytest.mark.parametrize("source,status", [("pentest", "failed"), ("sandbox_test", "running")])
def test_failed_pentest_report_is_visible_and_running_sandbox_is_not(export_context, source, status):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, source, status=status)
    if source == "pentest":
        engagement = db.query(PentestEngagement).filter_by(report_task_id=task.id).one()
        engagement.status = "failed"
        engagement.error_message = "检测阶段失败"
        task.error_message = "检测阶段失败"
        task.coverage = {
            "coverage_status": "partial",
            "failed_phases": ["detect"],
            "unscanned_phases": ["report"],
        }
        db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    if source == "pentest":
        assert response.status_code == 200
        payload = response.json()
        assert payload["task_info"]["status"] == "failed"
        assert payload["task_info"]["error_message"] == "检测阶段失败"
        assert payload["domain_data"]["execution_status"] == "failed"
        assert payload["domain_data"]["error_message"] == "检测阶段失败"
        assert payload["domain_data"]["coverage"]["unscanned_phases"] == ["report"]
        assert "issues" not in payload
    else:
        assert response.status_code == 404
        assert "domain_data" not in response.json()


def test_ambiguous_pentest_association_is_explicitly_rejected(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "pentest")
    db.add(
        PentestEngagement(
            public_id=f"ambiguous-{task.id}",
            user_id=owner.id,
            project_id=project.id,
            target_type="web",
            status="completed",
            report_task_id=task.id,
        )
    )
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 409
    assert response.json()["code"] == 40942
    assert "domain_data" not in response.json()
