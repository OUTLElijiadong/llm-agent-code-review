"""远程导入取消、阶段租约与旧 Worker 隔离回归测试。"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import ConflictError, NotFoundError
from app.models.project import Project
from app.models.project_import_task import ProjectImportTask
from app.models.user import User
from app.services import project_import_service


def _user(db, username: str) -> User:
    row = User(username=username, password="x", role="user", status=1)
    db.add(row)
    db.commit()
    return row


def _create(db, user: User, monkeypatch, *, key: str) -> dict:
    monkeypatch.setattr(project_import_service, "validate_remote_project_url", lambda _url: None)
    return project_import_service.create_import_task(
        db,
        user,
        url="https://example.com/source.zip",
        project_name=f"project-{key}",
        idempotency_key=key,
    )


@pytest.mark.parametrize("phase", ["queued", "downloading", "scanning", "ingesting"])
def test_owner_can_cancel_every_active_phase_with_readable_reason(
    db,
    monkeypatch,
    phase: str,
) -> None:
    owner = _user(db, f"owner-{phase}")
    outsider = _user(db, f"outsider-{phase}")
    created = _create(db, owner, monkeypatch, key=phase)
    row = db.query(ProjectImportTask).filter_by(public_id=created["task_id"]).one()
    if phase != "queued":
        claimed = project_import_service.claim_next_task(db, lease_seconds=60)
        assert claimed is not None
        row.status = phase
        db.commit()

    with pytest.raises(NotFoundError):
        project_import_service.cancel_import_task(
            db,
            outsider,
            created["task_id"],
            reason="越权取消",
        )

    cancelled = project_import_service.cancel_import_task(
        db,
        owner,
        created["task_id"],
        reason="用户不再需要该项目",
    )

    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_reason"] == "用户不再需要该项目"
    assert cancelled["error"] == {
        "code": "cancelled",
        "message": "用户不再需要该项目",
    }
    db.refresh(row)
    assert row.lease_token is None
    assert row.lease_expires_at is None
    assert row.completed_at is not None


def test_cancelled_task_is_idempotent_and_stale_worker_cannot_complete(db, monkeypatch) -> None:
    owner = _user(db, "cancel-race-owner")
    created = _create(db, owner, monkeypatch, key="cancel-race")
    claimed = project_import_service.claim_next_task(db, lease_seconds=60)
    assert claimed is not None

    first = project_import_service.cancel_import_task(
        db,
        owner,
        created["task_id"],
        reason="停止导入",
    )
    second = project_import_service.cancel_import_task(
        db,
        owner,
        created["task_id"],
        reason="重复点击",
    )

    assert second == first
    assert project_import_service.complete_import_task(
        db,
        claimed["id"],
        lease_token=claimed["lease_token"],
        result={"id": 99},
    ) is False


def test_cancelling_ingest_removes_task_owned_partial_project(db, monkeypatch) -> None:
    owner = _user(db, "cancel-partial-owner")
    created = _create(db, owner, monkeypatch, key="cancel-partial")
    claimed = project_import_service.claim_next_task(db, lease_seconds=60)
    assert claimed is not None
    row = db.get(ProjectImportTask, claimed["id"])
    project = project_import_service._ensure_import_project(
        db,
        row,
        owner,
        project_import_service._load_json(row.request_json),
    )
    row.status = "ingesting"
    db.commit()

    project_import_service.cancel_import_task(
        db,
        owner,
        created["task_id"],
        reason="停止半成品导入",
    )

    assert db.get(Project, project.id) is None
    db.refresh(row)
    assert row.status == "cancelled"
    assert row.project_id is None


@pytest.mark.parametrize("phase", ["running", "downloading", "scanning", "ingesting"])
def test_recovery_handles_every_leased_phase_and_invalidates_old_worker(
    db,
    monkeypatch,
    phase: str,
) -> None:
    owner = _user(db, f"recover-{phase}-owner")
    _create(db, owner, monkeypatch, key=f"recover-{phase}")
    claimed = project_import_service.claim_next_task(db, lease_seconds=30)
    assert claimed is not None
    row = db.get(ProjectImportTask, claimed["id"])
    row.status = phase
    row.lease_expires_at = project_import_service._utcnow() - timedelta(seconds=1)
    db.commit()

    assert project_import_service.recover_expired_leases(db) == 1
    assert project_import_service.complete_import_task(
        db,
        claimed["id"],
        lease_token=claimed["lease_token"],
        result={"id": 88},
    ) is False
    db.refresh(row)
    assert row.status == "queued"


def test_claim_and_touch_publish_real_phase_and_heartbeat(db, monkeypatch) -> None:
    owner = _user(db, "phase-owner")
    _create(db, owner, monkeypatch, key="phase-heartbeat")

    claimed = project_import_service.claim_next_task(db, lease_seconds=60)

    assert claimed is not None
    assert claimed["status"] == "downloading"
    row = db.get(ProjectImportTask, claimed["id"])
    claimed_heartbeat = row.heartbeat_at
    assert claimed_heartbeat is not None

    assert project_import_service.touch_import_task(
        db,
        row.id,
        lease_token=claimed["lease_token"],
        status="scanning",
        progress={"phase": "scanning"},
    ) is True
    db.refresh(row)
    assert row.status == "scanning"
    assert row.heartbeat_at >= claimed_heartbeat


def test_cancel_during_scan_is_seen_before_project_write(db, monkeypatch, tmp_path) -> None:
    owner = _user(db, "cancel-before-write-owner")
    created = _create(db, owner, monkeypatch, key="cancel-before-write")
    claimed = project_import_service.claim_next_task(db, lease_seconds=60)
    assert claimed is not None
    archive_path = tmp_path / "source.zip"
    archive_path.write_bytes(b"placeholder")

    @contextmanager
    def fake_download(*_args, **_kwargs):
        yield project_import_service.project_source_service.DownloadedRemoteArchive(
            path=Path(archive_path),
            filename="source.zip",
            byte_size=archive_path.stat().st_size,
            sha256="a" * 64,
        )

    def cancel_while_scanning(*_args, **_kwargs):
        project_import_service.cancel_import_task(
            db,
            owner,
            created["task_id"],
            reason="扫描阶段取消",
        )
        return []

    monkeypatch.setattr(
        project_import_service.project_source_service,
        "download_remote_project_archive_to_temp",
        fake_download,
    )
    monkeypatch.setattr(
        project_import_service.project_source_service,
        "read_archive_members",
        cancel_while_scanning,
    )

    with pytest.raises(ConflictError, match="取消|租约"):
        project_import_service.execute_claimed_import(
            db,
            claimed["id"],
            lease_token=claimed["lease_token"],
        )

    assert db.query(Project).count() == 0
    visible = project_import_service.get_import_task(db, owner, created["task_id"])
    assert visible["status"] == "cancelled"
    assert visible["cancel_reason"] == "扫描阶段取消"


def test_background_lease_heartbeat_repeats_until_stopped(monkeypatch) -> None:
    beats: list[tuple[int, str]] = []

    class FakeSession:
        def close(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        project_import_service,
        "touch_import_task",
        lambda _db, task_id, *, lease_token, **_kwargs: (
            beats.append((task_id, lease_token)) or True
        ),
    )
    heartbeat = project_import_service._ImportLeaseHeartbeat(
        task_db_id=17,
        lease_token="lease-token",
        interval_seconds=0.01,
        session_factory=FakeSession,
    )

    heartbeat.start()
    time.sleep(0.045)
    heartbeat.stop()

    assert len(beats) >= 2
    heartbeat.assert_healthy()


def test_background_heartbeat_renews_real_cross_session_lease(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'heartbeat.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    setup = Session()
    observer = Session()
    try:
        owner = _user(setup, "cross-session-heartbeat-owner")
        _create(setup, owner, monkeypatch, key="cross-session-heartbeat")
        claimed = project_import_service.claim_next_task(setup, lease_seconds=30)
        assert claimed is not None
        row = setup.get(ProjectImportTask, claimed["id"])
        initial_expiry = row.lease_expires_at

        heartbeat = project_import_service._ImportLeaseHeartbeat(
            task_db_id=row.id,
            lease_token=claimed["lease_token"],
            interval_seconds=0.01,
            session_factory=Session,
        )
        heartbeat.start()
        time.sleep(0.045)
        heartbeat.stop()
        heartbeat.assert_healthy()

        observer.expire_all()
        renewed = observer.get(ProjectImportTask, row.id)
        assert renewed.heartbeat_at is not None
        comparable_initial_expiry = initial_expiry.replace(tzinfo=None)
        assert renewed.lease_expires_at > comparable_initial_expiry
        assert project_import_service.recover_expired_leases(
            observer,
            now=comparable_initial_expiry + timedelta(seconds=1),
        ) == 0
    finally:
        setup.close()
        observer.close()
        engine.dispose()
