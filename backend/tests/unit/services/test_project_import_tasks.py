"""远程项目异步导入任务的幂等、租约与失败透明性测试。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError, ValidationError
from app.models.project import Project
from app.models.project_import_task import ProjectImportTask
from app.models.project_member import ProjectMember
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
