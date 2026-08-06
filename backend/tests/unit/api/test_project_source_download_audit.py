"""完整源码包下载的结构化审计契约。"""

from __future__ import annotations

import json

from starlette.requests import Request

from app.api.v1 import projects
from app.models.audit_log import AuditLog


def _request(
    real_ip: str | None = "203.0.113.25",
    *,
    peer_ip: str = "172.19.0.8",
) -> Request:
    headers = [] if real_ip is None else [(b"x-real-ip", real_ip.encode())]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/projects/82/source-archive",
            "headers": headers,
            "client": (peer_ip, 43120),
            "server": ("backend", 8000),
            "scheme": "https",
        }
    )


def test_download_source_archive_persists_structured_audit(
    db,
    admin_user,
    monkeypatch,
) -> None:
    content = b"PK\x03\x04whole-source"
    metadata = {
        "archive_sha256": "a" * 64,
        "malware_status": "infected",
        "threat_count": 2,
    }
    monkeypatch.setattr(
        projects.project_source_service,
        "build_source_archive",
        lambda *_args: (content, "完整源码.zip"),
    )
    monkeypatch.setattr(
        projects.project_source_service,
        "get_source_archive_metadata",
        lambda *_args: metadata,
    )

    response = projects.download_project_source(82, _request(), db, admin_user)

    assert response.body == content
    row = db.query(AuditLog).filter(AuditLog.action == "project_source_download").one()
    detail = json.loads(row.detail)
    assert row.actor_id == admin_user.id
    assert row.target_type == "project"
    assert row.target_id == "82"
    assert row.ip == "203.0.113.25"
    assert detail == {
        "archive_sha256": "a" * 64,
        "byte_size": len(content),
        "filename": "完整源码.zip",
        "malware_status": "infected",
        "project_id": 82,
        "result": "prepared",
        "threat_count": 2,
    }


def test_download_source_archive_remains_available_when_audit_cannot_commit(
    db,
    admin_user,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        projects.project_source_service,
        "build_source_archive",
        lambda *_args: (b"source", "source.zip"),
    )
    monkeypatch.setattr(
        projects.project_source_service,
        "get_source_archive_metadata",
        lambda *_args: None,
    )
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("audit offline")))

    response = projects.download_project_source(82, _request(), db, admin_user)

    assert response.body == b"source"


def test_generated_project_archive_audits_computed_digest_and_direct_peer(
    db,
    admin_user,
    monkeypatch,
) -> None:
    content = b"PK\x03\x04generated-source"
    monkeypatch.setattr(
        projects.project_source_service,
        "build_source_archive",
        lambda *_args: (content, "source.zip"),
    )
    monkeypatch.setattr(
        projects.project_source_service,
        "get_source_archive_metadata",
        lambda *_args: None,
    )

    response = projects.download_project_source(
        82,
        _request("1.1.1.1", peer_ip="8.8.8.8"),
        db,
        admin_user,
    )

    assert response.body == content
    row = db.query(AuditLog).filter(AuditLog.action == "project_source_download").one()
    detail = json.loads(row.detail)
    assert row.target_type == "project"
    assert row.target_id == "82"
    assert row.ip == "8.8.8.8"
    assert detail == {
        "archive_sha256": projects.hashlib.sha256(content).hexdigest(),
        "byte_size": len(content),
        "filename": "source.zip",
        "malware_status": "not_scanned",
        "project_id": 82,
        "result": "prepared",
        "threat_count": 0,
    }
