"""远程项目异步导入任务的幂等、租约与失败透明性测试。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.project_import_task import ProjectImportTask
from app.models.project_member import ProjectMember
from app.models.project_source_archive import ProjectSourceArchive
from app.models.project_source_revision import ProjectSourceRevision
from app.models.user import User
from app.schemas.project import ProjectIn
from app.services import project_import_service


def _user(db, username: str) -> User:
    row = User(username=username, password="x", role="user", status=1)
    db.add(row)
    db.commit()
    return row


def _create(db, user: User, *, key: str = "same-request") -> dict:
    return project_import_service.create_import_task(
        db,
        user,
        url="https://github.com/octocat/hello-world",
        project_name="hello-world",
        description="fixture",
        language="python",
        audit_mode=True,
        idempotency_key=key,
    )


def _attach_partial_project(db, user: User, row: ProjectImportTask) -> tuple[Project, CodeFile]:
    project = project_import_service.project_service.create_project(
        db,
        user,
        ProjectIn(project_name="hello-world", description="fixture", language="python"),
        initial_status="importing",
    )
    row.project_id = project.id
    row.result_json = json.dumps(
        {
            "import_metadata": {"provider": "github", "request_id": "req-1"},
            "progress": {"phase": "ingesting", "received_bytes": 128},
        },
        ensure_ascii=False,
    )
    code_file = CodeFile(
        project_id=project.id,
        file_name="main.py",
        file_path="src/main.py",
        language="python",
        size_bytes=12,
        line_count=1,
        version_no=1,
        content="print('ok')",
        status="active",
        raw_size=12,
    )
    db.add(code_file)
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            CodeVersion(
                file_id=code_file.id,
                version_no=1,
                content="print('ok')",
                change_desc="remote import",
                operator_id=user.id,
                create_time=now,
            ),
            ProjectSourceArchive(
                project_id=project.id,
                owner_id=user.id,
                original_filename="source.zip",
                media_type="application/zip",
                archive_sha256="a" * 64,
                compressed_size=64,
                expanded_size=128,
                file_count=1,
                max_member_size=128,
                max_compression_ratio=2.0,
                storage_status="active",
                malware_status="clean",
                audit_status="not_started",
                threat_count=0,
                scan_summary_json="{}",
                archive_blob=b"partial-archive",
            ),
            ProjectSourceRevision(
                project_id=project.id,
                owner_id=user.id,
                revision_no=1,
                source_sha256="b" * 64,
                parent_sha256="a" * 64,
                repaired_files_json="[]",
                repair_notes="partial revision",
                archive_blob=b"partial-revision",
                create_time=now,
                update_time=now,
            ),
        ]
    )
    db.commit()
    return project, code_file


def _assert_project_graph_removed(db, project_id: int, file_id: int) -> None:
    assert db.get(Project, project_id) is None
    assert db.query(CodeVersion).filter_by(file_id=file_id).count() == 0
    assert db.query(CodeFile).filter_by(project_id=project_id).count() == 0
    assert db.query(ProjectSourceArchive).filter_by(project_id=project_id).count() == 0
    assert db.query(ProjectSourceRevision).filter_by(project_id=project_id).count() == 0
    assert db.query(ProjectMember).filter_by(project_id=project_id).count() == 0


def test_create_import_task_is_idempotent_and_rejects_key_reuse(db, monkeypatch) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "import-owner")

    first = _create(db, user)
    second = _create(db, user)

    assert first["task_id"] == second["task_id"]
    assert first["status"] == "queued"
    assert db.query(ProjectImportTask).count() == 1

    with pytest.raises(ConflictError, match="幂等键"):
        project_import_service.create_import_task(
            db,
            user,
            url="https://github.com/octocat/another-repo",
            project_name="another-repo",
            idempotency_key="same-request",
        )


def test_claim_and_expired_lease_recovery_are_restart_safe(db, monkeypatch) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "lease-owner")
    created = _create(db, user, key="lease")

    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    assert claimed["task_id"] == created["task_id"]
    assert claimed["attempt_count"] == 1
    assert claimed["lease_token"]

    row = db.query(ProjectImportTask).filter_by(public_id=created["task_id"]).one()
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    assert project_import_service.recover_expired_leases(db) == 1
    db.refresh(row)
    assert row.status == "queued"
    assert row.lease_token is None

    reclaimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed["attempt_count"] == 2


def test_touch_import_task_merges_progress_without_losing_result_metadata(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "touch-owner")
    _create(db, user, key="touch-progress")
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    row = db.get(ProjectImportTask, claimed["id"])
    row.result_json = json.dumps(
        {
            "import_metadata": {"provider": "github", "request_id": "req-touch"},
            "resume_hint": "keep-me",
            "progress": {"phase": "queued"},
        },
        ensure_ascii=False,
    )
    db.commit()

    assert project_import_service.touch_import_task(
        db,
        row.id,
        lease_token=claimed["lease_token"],
        progress={
            "phase": "downloading",
            "received_bytes": 128,
            "total_bytes": 512,
        },
    ) is True

    db.refresh(row)
    result = json.loads(row.result_json)
    assert result == {
        "import_metadata": {"provider": "github", "request_id": "req-touch"},
        "resume_hint": "keep-me",
        "progress": {
            "phase": "downloading",
            "received_bytes": 128,
            "total_bytes": 512,
        },
    }


def test_expired_lease_cleans_partial_project_graph_but_keeps_unrelated_projects(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "expired-partial-owner")
    _create(db, user, key="expired-partial")
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    row = db.get(ProjectImportTask, claimed["id"])
    partial, code_file = _attach_partial_project(db, user, row)
    partial.status = "import_failed"
    unrelated_active = project_import_service.project_service.create_project(
        db,
        user,
        ProjectIn(project_name="keep-active", language="python"),
        initial_status="active",
    )
    unrelated_importing = project_import_service.project_service.create_project(
        db,
        user,
        ProjectIn(project_name="keep-importing", language="python"),
        initial_status="importing",
    )
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    assert project_import_service.recover_expired_leases(db) == 1

    _assert_project_graph_removed(db, partial.id, code_file.id)
    assert db.get(Project, unrelated_active.id) is not None
    assert db.get(Project, unrelated_importing.id) is not None
    db.refresh(row)
    assert row.status == "queued"
    assert row.project_id is None
    assert row.error_code == "lease_expired"
    assert "自动重试" in row.error_message
    result = json.loads(row.result_json)
    assert result["import_metadata"]["request_id"] == "req-1"
    assert result["progress"] == {"phase": "ingesting", "received_bytes": 128}


def test_retryable_failure_cleans_partial_graph_and_retry_creates_fresh_project(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "retry-partial-owner")
    _create(db, user, key="retry-partial")
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    row = db.get(ProjectImportTask, claimed["id"])
    partial, code_file = _attach_partial_project(db, user, row)

    assert project_import_service.fail_import_task(
        db,
        row.id,
        lease_token=claimed["lease_token"],
        error=ExternalServiceError("GitHub 下载连接中断，可稍后重试", code=50201),
        retryable=True,
    ) is True

    _assert_project_graph_removed(db, partial.id, code_file.id)
    db.refresh(row)
    assert row.status == "queued"
    assert row.project_id is None
    assert row.error_code == "50201"
    assert row.error_message == "GitHub 下载连接中断，可稍后重试"
    result = json.loads(row.result_json)
    assert result["import_metadata"]["request_id"] == "req-1"
    assert result["progress"]["phase"] == "ingesting"

    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    reclaimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert reclaimed is not None
    retry_row = db.get(ProjectImportTask, reclaimed["id"])
    fresh = project_import_service._ensure_import_project(
        db,
        retry_row,
        user,
        project_import_service._load_json(retry_row.request_json),
    )

    assert fresh.status == "importing"
    assert db.query(Project).filter_by(project_name="hello-world").count() == 1
    assert db.query(ProjectMember).filter_by(project_id=fresh.id).count() == 1
    retry_result = json.loads(retry_row.result_json)
    assert retry_result["import_metadata"]["request_id"] == "req-1"
    assert retry_result["progress"] == {"phase": "project_created"}


def test_failure_never_deletes_active_project_even_when_task_points_to_it(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "active-project-owner")
    _create(db, user, key="active-project")
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    row = db.get(ProjectImportTask, claimed["id"])
    active = project_import_service.project_service.create_project(
        db,
        user,
        ProjectIn(project_name="hello-world", language="python"),
        initial_status="active",
    )
    row.project_id = active.id
    db.commit()

    assert project_import_service.fail_import_task(
        db,
        row.id,
        lease_token=claimed["lease_token"],
        error=ExternalServiceError("临时网络错误", code=50201),
        retryable=True,
    ) is True

    assert db.get(Project, active.id) is not None
    assert db.query(ProjectMember).filter_by(project_id=active.id).count() == 1
    db.refresh(row)
    assert row.project_id == active.id


def test_terminal_failure_preserves_actionable_reason_for_owner(db, monkeypatch) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    owner = _user(db, "failure-owner")
    outsider = _user(db, "failure-outsider")
    created = _create(db, owner, key="failure")

    row = db.query(ProjectImportTask).filter_by(public_id=created["task_id"]).one()
    row.max_attempts = 1
    db.commit()
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None

    project_import_service.fail_import_task(
        db,
        claimed["id"],
        lease_token=claimed["lease_token"],
        error=ExternalServiceError("GitHub 返回 HTTP 429，请稍后重试", code=50201),
        retryable=True,
    )

    visible = project_import_service.get_import_task(db, owner, created["task_id"])
    assert visible["status"] == "failed"
    assert visible["error"] == {
        "code": "50201",
        "message": "GitHub 返回 HTTP 429，请稍后重试",
    }
    assert visible["attempt_count"] == 1

    with pytest.raises(NotFoundError):
        project_import_service.get_import_task(db, outsider, created["task_id"])


def test_invalid_download_is_rejected_before_project_creation(db, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "invalid-archive-owner")
    _create(db, user, key="invalid-archive")
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    archive_path = tmp_path / "broken.zip"
    archive_path.write_bytes(b"not-a-zip")

    @contextmanager
    def fake_download(*_args, **_kwargs):
        yield project_import_service.project_source_service.DownloadedRemoteArchive(
            path=Path(archive_path),
            filename="broken.zip",
            byte_size=archive_path.stat().st_size,
            sha256="0" * 64,
        )

    monkeypatch.setattr(
        project_import_service.project_source_service,
        "download_remote_project_archive_to_temp",
        fake_download,
    )

    with pytest.raises(ValidationError, match="损坏"):
        project_import_service.execute_claimed_import(
            db,
            claimed["id"],
            lease_token=claimed["lease_token"],
        )

    assert db.query(Project).count() == 0
    assert db.query(ProjectMember).count() == 0


def test_terminal_failure_removes_empty_internal_project_and_allows_same_name_retry(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    user = _user(db, "cleanup-owner")
    _create(db, user, key="cleanup-first")
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    row = db.get(ProjectImportTask, claimed["id"])
    project = project_import_service.project_service.create_project(
        db,
        user,
        ProjectIn(project_name="hello-world", description="fixture", language="python"),
        initial_status="importing",
    )
    row.project_id = project.id
    row.max_attempts = 1
    db.commit()

    assert project_import_service.fail_import_task(
        db,
        claimed["id"],
        lease_token=claimed["lease_token"],
        error=ValidationError("归档校验失败", code=40001),
        retryable=False,
    ) is True

    assert db.get(Project, project.id) is None
    assert db.query(ProjectMember).filter_by(project_id=project.id).count() == 0
    db.refresh(row)
    assert row.project_id is None
    retry = _create(db, user, key="cleanup-second")
    assert retry["status"] == "queued"
