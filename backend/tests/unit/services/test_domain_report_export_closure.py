"""领域报告入口出口闭环与个人语言统计隔离，仅使用本地合成数据。"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

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
from app.services import profile_service, report_exporter, review_service

REPORT_MD = "## 总体结论\n隔离合成报告\n## 问题清单\n- 发现甲\n- 发现乙\n- 发现丙\n"


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
        user_id=owner.id, project_id=project.id, task_name=f"领域-{source}",
        review_type=source, status=status, total_issues=3 if source == "sandbox_test" else 2,
        score=0 if status == "failed" else (100 if source == "sandbox_test" else 84),
        score_version="pentest-v1" if source == "pentest" else None,
        summary=REPORT_MD,
    )
    db.add(task)
    db.flush()
    content = {"source": source, "report_md": REPORT_MD, "public_id": f"sandbox-export-{task.id}"}
    if source == "pentest":
        engagement = PentestEngagement(
            public_id=f"pentest-export-{task.id}", user_id=owner.id, project_id=project.id,
            target_type="web", status="completed", report_task_id=task.id,
        )
        db.add(engagement)
        db.flush()
        content = {"source": "pentest", "engagement_public_id": engagement.public_id}
        for position, status in enumerate(["confirmed", "refuted"]):
            db.add(PentestFinding(
                engagement_id=engagement.id, finding_fingerprint=f"export-{task.id}-{position}",
                title=f"真实领域发现-{position}", severity="高", category="注入", status=status,
                description="合成领域证据", evidence_json='{"detail":"fixture-evidence"}',
            ))
    db.add(ReviewReport(
        task_id=task.id, user_id=owner.id, score=task.score, content_json=content,
        create_time=datetime.now(timezone.utc),
    ))
    db.commit()
    assert review_service._task_agent_release_summaries(db, task.id) == []
    return task


def _sandbox_artifact(db, owner, project, task):
    environment = SandboxEnvironment(
        public_id=f"sandbox-export-{task.id}", owner_id=owner.id, project_id=project.id,
        agent_code="test_verifier", status="stopped", source_sha256="a" * 64,
        runtime="runsc", image_ref="synthetic", purpose="test", language="python",
        resource_policy_json="{}", agent_config_json="{}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db.add(environment)
    db.flush()
    content = REPORT_MD.encode("utf-8")
    artifact = SandboxArtifact(
        environment_id=environment.id, artifact_type="review_report", file_name="sandbox-report.md",
        mime_type="text/markdown", byte_size=len(content), sha256=hashlib.sha256(content).hexdigest(),
        storage_ref="inline", content_base64=base64.b64encode(content).decode("ascii"),
    )
    db.add(artifact)
    db.commit()
    return environment, artifact


@pytest.mark.parametrize("source,count", [("sandbox_test", 3), ("pentest", 2)])
@pytest.mark.parametrize("endpoint", ["generate", "download"])
def test_domain_json_export_matches_detail_without_standard_issue_fabrication(export_context, source, count, endpoint):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, source)
    if endpoint == "generate":
        response = client.post("/api/reports/generate", json={"task_id": task.id, "format": "json"})
    else:
        response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 200
    exported = response.json()
    detail = client.get(f"/api/reports/{task.id}").json()["data"]

    assert exported["statistics"]["total_issues"] == detail["stats"]["total_issues"] == count
    assert exported["task_info"]["total_issues"] == count
    assert exported["score"] == task.score
    assert exported["source"]["type"] == source
    assert "issues" not in exported
    assert db.query(ReviewIssue).filter_by(task_id=task.id).count() == 0
    if source == "sandbox_test":
        assert exported["domain_data"]["report_md"] == REPORT_MD
    else:
        assert len(exported["domain_data"]["findings"]) == count
        assert exported["statistics"]["refuted"] == 1
        assert exported["statistics"]["severity"] == {"高": 1}
        assert exported["domain_data"]["findings"][0]["evidence"]["detail"] == "fixture-evidence"


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
@pytest.mark.parametrize("endpoint,format", [
    ("generate", "html"), ("generate", "pdf"), ("generate", "word"),
    ("download", "html"), ("download", "pdf"), ("download", "word"),
    ("legacy", "pdf"), ("legacy", "word"), ("preview", "html"),
])
def test_non_equivalent_domain_format_is_explicitly_rejected(export_context, source, endpoint, format):
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

    assert response.status_code == 409
    assert "领域" in response.json()["message"]
    assert f"/api/reports/tasks/{task.id}/export?format=json" in response.json()["next_action"]
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize("source", ["sandbox_test", "pentest"])
def test_generic_exporter_cannot_bypass_source_gate(source):
    with pytest.raises(ConflictError):
        report_exporter.export_to_dict({"id": 1, "review_type": source, "total_issues": 3}, [], REPORT_MD, 100)


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


def test_failed_sandbox_json_preserves_zero_score_and_report_body(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test", status="failed")
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")

    assert response.status_code == 200
    assert response.json()["statistics"]["total_issues"] == 3
    assert response.json()["task_info"]["status"] == "failed"
    assert response.json()["score"] == 0


@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
def test_personal_language_distribution_intersects_visible_projects(db, role):
    owner = User(username=f"language-{role}", password="x", role=role, status=1)
    other = User(username=f"language-other-{role}", password="x", role="user", status=1)
    db.add_all([owner, other])
    db.flush()
    for name, language, status, user_id in [
        ("本人活动", "Python", "active", owner.id), ("本人归档", "Go", "archived", owner.id),
        ("隔离测试", "Rust", "quarantined", owner.id), ("删除项目", "Java", "deleted", owner.id),
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
    assert response.status_code == (200 if domain_view else 403)


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


def test_empty_real_pentest_export_remains_valid_and_preserves_score(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "pentest")
    engagement = db.query(PentestEngagement).filter_by(report_task_id=task.id).one()
    db.query(PentestFinding).filter_by(engagement_id=engagement.id).delete()
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 200
    assert response.json()["statistics"]["total_issues"] == 0
    assert response.json()["domain_data"]["findings"] == []
    assert response.json()["score"] == 84


def test_domain_export_errors_keep_recovery_link_in_production_response(export_context, monkeypatch):
    from app.core import error_handlers

    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "sandbox_test")
    monkeypatch.setattr(error_handlers, "_is_prod", lambda: True)

    response = client.get(f"/api/reports/{task.id}/export/pdf")
    assert response.status_code == 409
    assert "detail" not in response.json()
    assert f"/api/reports/tasks/{task.id}/export?format=json" in response.json()["next_action"]


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
        user_id=owner.id, project_id=project.id, task_name=f"兼容-{source}",
        review_type=source, status="success", total_issues=99, score=0,
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
def test_unavailable_domain_report_does_not_export_stale_evidence(export_context, source, status):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, source, status=status)
    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 404
    assert "domain_data" not in response.json()


def test_ambiguous_pentest_association_is_explicitly_rejected(export_context):
    client, db, owner, other, project, current = export_context
    task = _domain_fixture(db, owner, project, "pentest")
    db.add(PentestEngagement(
        public_id=f"ambiguous-{task.id}", user_id=owner.id, project_id=project.id,
        target_type="web", status="completed", report_task_id=task.id,
    ))
    db.commit()

    response = client.get(f"/api/reports/tasks/{task.id}/export?format=json")
    assert response.status_code == 409
    assert response.json()["code"] == 40942
    assert "domain_data" not in response.json()
