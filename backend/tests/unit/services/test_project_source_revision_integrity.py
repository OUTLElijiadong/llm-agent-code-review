"""源码副本读取、摘要和内容必须保持同一 SHA-256 身份。"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest

from app.core.exceptions import ConflictError
from app.services import project_source_revision_service as revisions


def _archive_base64() -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("main.py", "print('revision')\n")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_revision_snapshot_returns_verified_identity_and_bytes(db, monkeypatch) -> None:
    monkeypatch.setattr(revisions, "require_project_access", lambda *_args, **_kwargs: None)
    row = revisions.save_revision(
        db,
        project_id=9,
        owner_id=7,
        repaired_source_base64=_archive_base64(),
        repaired_files=["main.py"],
        parent_sha256="parent",
    )
    assert row is not None

    snapshot = revisions.get_revision_archive_snapshot(db, SimpleNamespace(id=7), row.id, 9)

    assert snapshot["archive"] == bytes(row.archive_blob)
    assert snapshot["revision_id"] == row.id
    assert snapshot["revision_no"] == 1
    assert snapshot["parent_sha256"] == "parent"
    assert snapshot["source_sha256"] == hashlib.sha256(snapshot["archive"]).hexdigest()
    assert revisions.get_revision_archive(db, SimpleNamespace(id=7), row.id, 9) == snapshot["archive"]


def test_revision_snapshot_fails_closed_when_blob_is_tampered(db, monkeypatch) -> None:
    monkeypatch.setattr(revisions, "require_project_access", lambda *_args, **_kwargs: None)
    row = revisions.save_revision(
        db,
        project_id=9,
        owner_id=7,
        repaired_source_base64=_archive_base64(),
        repaired_files=["main.py"],
        parent_sha256="parent",
    )
    assert row is not None
    row.archive_blob = bytes(row.archive_blob) + b"tampered"
    db.flush()

    with pytest.raises(ConflictError, match="SHA-256"):
        revisions.get_revision_archive_snapshot(db, SimpleNamespace(id=7), row.id, 9)


def test_duplicate_revision_is_idempotent_but_corrupt_latest_revision_is_not_reused(db, monkeypatch) -> None:
    monkeypatch.setattr(revisions, "require_project_access", lambda *_args, **_kwargs: None)
    arguments = {
        "project_id": 9,
        "owner_id": 7,
        "repaired_source_base64": _archive_base64(),
        "repaired_files": ["main.py"],
        "parent_sha256": "parent",
    }
    first = revisions.save_revision(db, **arguments)
    duplicate = revisions.save_revision(db, **arguments)
    assert first is not None and duplicate is not None
    assert duplicate.id == first.id

    first.archive_blob = b"corrupted"
    db.flush()
    with pytest.raises(ConflictError, match="SHA-256"):
        revisions.save_revision(db, **arguments)
