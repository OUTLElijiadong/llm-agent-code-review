"""项目源码归档与远程归档下载服务回归测试。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import inspect

from app.core.exceptions import ConflictError, ExternalServiceError, ValidationError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.user import User
from app.services import code_file_service, project_source_service
from app.utils.encoding_utils import to_utf8
from app.utils.malware_scanner import ScanResult
from app.utils.public_http import PinnedPublicUrl
from app.utils.source_archive_gate import source_archive_workload


def _file(file_id: int, path: str, content: str = "", *, binary: Optional[bytes] = None) -> CodeFile:
    return CodeFile(
        id=file_id,
        project_id=7,
        file_name=path.rsplit("/", 1)[-1],
        file_path=path,
        language="binary" if binary is not None else "python",
        content=to_utf8(binary) if binary is not None else content,
        status="active",
        is_binary=1 if binary is not None else 0,
        original_blob=binary,
        size_bytes=len(binary or content.encode()),
        raw_size=len(binary or content.encode()),
        line_count=0 if binary is not None else content.count("\n") + 1,
        version_no=1,
    )


def test_build_source_archive_preserves_current_text_and_binary(monkeypatch):
    db = MagicMock()
    project = Project(id=7, user_id=1, project_name="demo/source\nname", status="active")
    # 同一路径按 id 降序返回；服务应保留最新活动记录并忽略旧副本。
    rows = [
        _file(3, "src/main.py", "print('new')\n"),
        _file(2, "src/main.py", "print('old')\n"),
        _file(1, "assets/logo.bin", binary=b"\x00\x01logo"),
        _file(4, "../escape.py", "bad\n"),
    ]
    db.get.return_value = project
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = rows
    monkeypatch.setattr(project_source_service, "require_project_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_source_service, "_active_source_archive", lambda *args, **kwargs: None)

    content, filename = project_source_service.build_source_archive(
        db, User(id=1, role="user", username="u", status=1), 7,
    )

    assert filename == "demo_source_name.zip"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert sorted(archive.namelist()) == ["assets/logo.bin", "src/main.py"]
        assert archive.read("src/main.py") == b"print('new')\n"
        assert archive.read("assets/logo.bin") == b"\x00\x01logo"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/source.zip",
        "https://user:secret@example.com/source.zip",
        "https://example.com:444/source.zip",
        "https://example.com:invalid/source.zip",
    ],
)
def test_public_url_rejects_unsafe_schemes_credentials_and_ports(url):
    with pytest.raises(ValidationError):
        project_source_service._assert_public_url(url)


def test_public_url_rejects_private_dns_result(monkeypatch):
    monkeypatch.setattr(
        "app.utils.public_http.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValidationError, match="内网或保留地址"):
        project_source_service._assert_public_url("https://example.com/source.zip")


def test_remote_download_revalidates_redirect_and_size(monkeypatch):
    visited = []
    checked = []
    responses = [
        MagicMock(
            status_code=302,
            headers=httpx.Headers({"location": "https://cdn.example.com/source.zip"}),
            content=b"",
        ),
        MagicMock(
            status_code=200,
            headers=httpx.Headers({"content-length": "4"}),
            content=b"PK00",
        ),
    ]

    class ResponseContext:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            self.response.iter_bytes.return_value = iter([self.response.content])
            return self.response

        def __exit__(self, *_args):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, url, **kwargs):
            assert method == "GET"
            visited.append((url, kwargs))
            return ResponseContext(responses.pop(0))

    def pin(url):
        checked.append(url)
        host = "example.com" if len(checked) == 1 else "cdn.example.com"
        ip = "93.184.216.34" if len(checked) == 1 else "93.184.216.35"
        path = "/redirect" if len(checked) == 1 else "/source.zip"
        return PinnedPublicUrl(url, f"https://{ip}{path}", host, host, ip)

    client_kwargs = {}
    monkeypatch.setattr(project_source_service, "_pin_remote_url", pin)

    def client_factory(**kwargs):
        client_kwargs.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(project_source_service.httpx, "Client", client_factory)

    body, name = project_source_service.download_remote_archive(
        "https://example.com/redirect",
    )

    assert body == b"PK00"
    assert name == "source.zip"
    assert checked == [
        "https://example.com/redirect",
        "https://cdn.example.com/source.zip",
    ]
    assert [item[0] for item in visited] == [
        "https://93.184.216.34/redirect",
        "https://93.184.216.35/source.zip",
    ]
    assert [item[1]["headers"]["Host"] for item in visited] == [
        "example.com",
        "cdn.example.com",
    ]
    assert visited[0][1]["extensions"] == {"sni_hostname": "example.com"}
    assert client_kwargs["trust_env"] is False


def test_remote_download_rejects_invalid_content_length(monkeypatch):
    response = MagicMock(
        status_code=200,
        headers=httpx.Headers({"content-length": "invalid"}),
        content=b"PK",
    )

    class ResponseContext:
        def __enter__(self):
            return response

        def __exit__(self, *_args):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, _url, **_kwargs):
            assert method == "GET"
            return ResponseContext()

    monkeypatch.setattr(
        project_source_service,
        "_pin_remote_url",
        lambda url: PinnedPublicUrl(url, url, "example.com", "example.com", "93.184.216.34"),
    )
    monkeypatch.setattr(project_source_service.httpx, "Client", lambda **kwargs: FakeClient())

    with pytest.raises(ExternalServiceError, match="响应长度无效"):
        project_source_service.download_remote_archive("https://example.com/source.zip")


def test_remote_download_stops_when_stream_exceeds_limit(monkeypatch):
    response = MagicMock(
        status_code=200,
        headers=httpx.Headers({}),
    )
    response.iter_bytes.return_value = iter([b"PK0", b"123"])

    class ResponseContext:
        def __enter__(self):
            return response

        def __exit__(self, *_args):
            return None

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, _url, **_kwargs):
            assert method == "GET"
            return ResponseContext()

    monkeypatch.setattr(project_source_service, "MAX_REMOTE_BYTES", 4)
    monkeypatch.setattr(
        project_source_service,
        "_pin_remote_url",
        lambda url: PinnedPublicUrl(url, url, "example.com", "example.com", "93.184.216.34"),
    )
    monkeypatch.setattr(project_source_service.httpx, "Client", lambda **kwargs: FakeClient())

    with pytest.raises(ValidationError, match="超过 20MB"):
        project_source_service.download_remote_archive("https://example.com/source.zip")


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return out.getvalue()


def _stored_archive(db, *, username: str = "audit_state_owner"):
    user = User(username=username, password="x", role="user", status=1)
    db.add(user)
    db.flush()
    project = Project(user_id=user.id, project_name=username, status="active")
    db.add(project)
    db.flush()
    raw = _zip_bytes({"src/main.php": b"<?php echo 1;\n"})
    row = ProjectSourceArchive(
        project_id=project.id,
        owner_id=user.id,
        original_filename="source.zip",
        media_type="application/zip",
        archive_sha256=hashlib.sha256(raw).hexdigest(),
        compressed_size=len(raw),
        expanded_size=14,
        file_count=1,
        max_member_size=14,
        max_compression_ratio=1.0,
        storage_status="active",
        malware_status="clean",
        audit_status="not_started",
        threat_count=0,
        scan_summary_json="{}",
        archive_blob=raw,
    )
    db.add(row)
    db.commit()
    return user, project, row, raw


def test_quarantine_zip_rejects_posix_absolute_path():
    raw = _zip_bytes({"/etc/evil.php": b"<?php echo 1;"})

    with pytest.raises(ValidationError, match="非法或绝对路径"):
        project_source_service._strict_zip_members(raw)


def test_quarantine_zip_rejects_overlong_path_and_segment():
    overlong_segment = _zip_bytes({f"src/{'a' * 256}.php": b"<?php echo 1;"})
    with pytest.raises(ValidationError, match="路径段超过 255"):
        project_source_service._strict_zip_members(overlong_segment)

    parts = [f"segment-{index:03d}-" + ("b" * 40) for index in range(45)]
    overlong_path = _zip_bytes({"/".join(parts) + ".php": b"<?php echo 1;"})
    with pytest.raises(ValidationError, match="路径超过 2048"):
        project_source_service._strict_zip_members(overlong_path)


def test_ingest_quarantined_archive_preserves_original_blob_and_virtual_files(db, monkeypatch):
    user = User(
        username="archive_owner",
        password="x",
        email="archive@example.com",
        role="user",
        status=1,
    )
    db.add(user)
    db.flush()
    project = Project(
        user_id=user.id,
        project_name="unsafe source",
        language="php",
        status="active",
    )
    db.add(project)
    db.commit()
    raw = _zip_bytes({
        "src/index.php": b"<?php echo 'ok';\n",
        "src/shell.php": b"<?php eval(base64_decode('eA=='));\n",
    })
    monkeypatch.setattr(
        project_source_service.MalwareScanner,
        "scan_clamav",
        lambda self, content, filename: ScanResult(engine="clamav", result="clean"),
    )

    def yara_scan(self, content, filename):
        if filename.endswith("shell.php"):
            return ScanResult(engine="yara", result="infected", threat_name="php_webshell")
        return ScanResult(engine="yara", result="clean")

    monkeypatch.setattr(project_source_service.MalwareScanner, "scan_yara", yara_scan)

    result = project_source_service.ingest_source_archive_bytes(
        db,
        user,
        project.id,
        raw=raw,
        filename="unsafe.zip",
    )

    assert result["malware_status"] == "infected"
    assert result["quarantined"] is True
    assert result["threat_count"] == 1
    assert result["file_count"] == 2
    assert db.query(CodeFile).filter(CodeFile.project_id == project.id).count() == 0

    db.expire_all()
    stored = db.query(ProjectSourceArchive).filter_by(project_id=project.id).one()
    assert "archive_blob" in inspect(stored).unloaded
    downloaded, filename = project_source_service.build_source_archive(db, user, project.id)
    assert filename == "unsafe.zip"
    assert downloaded == raw
    projected = project_source_service.load_project_source_files(db, user, project.id)
    assert [item.file_path for item in projected] == ["src/index.php", "src/shell.php"]
    assert all(item.id < 0 for item in projected)


def test_quarantined_binary_projection_omits_duplicate_payload_but_preserves_sizes(db):
    user, project, row, _raw = _stored_archive(db, username="binary_projection_owner")
    binary = b"\x89PNG\r\n\x1a\n\x00\xffpayload"
    raw = _zip_bytes({"assets/evidence.bin": binary})
    row.archive_blob = raw
    row.archive_sha256 = hashlib.sha256(raw).hexdigest()
    row.compressed_size = len(raw)
    row.expanded_size = len(binary)
    row.file_count = 1
    row.max_member_size = len(binary)
    db.commit()

    projected = project_source_service.load_project_source_files(db, user, project.id)

    assert len(projected) == 1
    item = projected[0]
    assert item.file_path == "assets/evidence.bin"
    assert item.is_binary == 1
    assert item.content == ""
    assert item.original_blob is None
    assert item.size_bytes == len(binary)
    assert item.raw_size == len(binary)
    assert item.line_count == 0


def test_quarantined_archive_scan_error_is_not_reported_clean(db, monkeypatch):
    user = User(
        username="archive_error_owner",
        password="x",
        email="archive-error@example.com",
        role="user",
        status=1,
    )
    db.add(user)
    db.flush()
    project = Project(user_id=user.id, project_name="scan error", status="active")
    db.add(project)
    db.commit()
    raw = _zip_bytes({"src/index.php": b"<?php echo 1;\n"})
    monkeypatch.setattr(
        project_source_service.MalwareScanner,
        "scan_clamav",
        lambda self, content, filename: ScanResult(
            engine="clamav", result="error", degraded=True, detail="timeout",
        ),
    )
    monkeypatch.setattr(
        project_source_service.MalwareScanner,
        "scan_yara",
        lambda self, content, filename: ScanResult(engine="yara", result="clean"),
    )

    result = project_source_service.ingest_source_archive_bytes(
        db,
        user,
        project.id,
        raw=raw,
        filename="source.zip",
    )

    assert result["malware_status"] == "error"
    assert result["quarantined"] is True


def test_quarantined_yara_total_deadline_is_fail_closed_with_accurate_summary(monkeypatch):
    raw = _zip_bytes({
        "src/first.php": b"<?php echo 1;\n",
        "src/second.php": b"<?php echo 2;\n",
        "src/third.php": b"<?php echo 3;\n",
    })
    members, _envelope = project_source_service._strict_zip_members(raw)
    scanner = MagicMock()
    scanner.scan_clamav.return_value = ScanResult(engine="clamav", result="clean")
    scanner.scan_yara.return_value = ScanResult(engine="yara", result="clean")
    scanner_factory = MagicMock(return_value=scanner)
    monotonic_values = iter([100.0, 100.0, 106.0, 108.0])
    monkeypatch.setattr(project_source_service, "MalwareScanner", scanner_factory)
    monkeypatch.setattr(project_source_service.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(project_source_service.settings, "source_archive_yara_total_timeout", 5.0)

    malware_status, threat_count, summary_json = project_source_service._scan_quarantined_zip(
        raw,
        "source.zip",
        members,
    )

    summary = json.loads(summary_json)
    member_summary = summary["member_yara"]
    assert malware_status == "error"
    assert threat_count == 0
    assert member_summary["total"] == 3
    assert member_summary["scanned"] == 1
    assert member_summary["skipped_due_deadline"] == 2
    assert member_summary["skipped_due_deadline"] > 0
    assert member_summary["scanned"] + member_summary["skipped_due_deadline"] == member_summary["total"]
    assert member_summary["result_counts"] == {
        "clean": 1,
        "degraded": 0,
        "error": 0,
        "infected": 0,
        "timeout": 0,
    }
    assert member_summary["duration_ms"] == 8000
    scanner_factory.assert_called_once_with(
        clamav_timeout=project_source_service.settings.source_archive_clamav_timeout,
    )
    scanner.scan_clamav.assert_called_once_with(raw, "source.zip")
    scanner.scan_yara.assert_called_once_with(b"<?php echo 1;\n", "src/first.php")


def test_quarantined_archive_rejects_pathological_text_line_count(monkeypatch):
    raw = _zip_bytes({
        "src/line-bomb.php": b"x\n" * project_source_service.MAX_AUDIT_TEXT_LINES_PER_FILE,
    })
    members, _envelope = project_source_service._strict_zip_members(raw)
    scanner = MagicMock()
    scanner.scan_clamav.return_value = ScanResult(engine="clamav", result="clean")
    monkeypatch.setattr(project_source_service, "MalwareScanner", MagicMock(return_value=scanner))

    with pytest.raises(ValidationError, match="文本成员超过 .* 行"):
        project_source_service._scan_quarantined_zip(raw, "source.zip", members)

    scanner.scan_yara.assert_not_called()


def test_source_archive_gate_rejects_parallel_heavy_workload() -> None:
    with source_archive_workload():
        with pytest.raises(ConflictError, match="已有整包源码审计"):
            with source_archive_workload():
                pass


def test_source_archive_audit_state_rejects_live_run_and_recovers_stale_run(db) -> None:
    user, project, row, _raw = _stored_archive(db)
    first_run_id = project_source_service.begin_source_archive_audit(db, project.id)
    assert isinstance(first_run_id, str) and first_run_id
    with pytest.raises(ConflictError, match="已有白盒审计"):
        project_source_service.begin_source_archive_audit(db, project.id)

    row.audit_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db.commit()
    second_run_id = project_source_service.begin_source_archive_audit(db, project.id)
    assert isinstance(second_run_id, str) and second_run_id != first_run_id
    assert project_source_service.finish_source_archive_audit(
        db,
        project.id,
        "failed",
        {"summary": "stale"},
        audit_run_id=first_run_id,
    ) is False
    db.refresh(row)
    assert row.audit_status == "running"
    assert row.audit_run_id == second_run_id
    assert project_source_service.finish_source_archive_audit(
        db,
        project.id,
        "succeeded",
        {"summary": "done", "findings": []},
        audit_run_id=second_run_id,
    ) is True

    stored = project_source_service.get_source_archive_audit_result(db, user, project.id)
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["result"]["source_archive_sha256"] == row.archive_sha256
    assert stored["result"]["audit_run_id"] == second_run_id
    assert stored["result"]["summary"] == "done"


def test_mixed_archive_and_editable_files_fail_closed(db) -> None:
    user, project, _row, _raw = _stored_archive(db, username="mixed_source_owner")
    mixed = _file(999, "src/mixed.py", "print('mixed')\n")
    mixed.project_id = project.id
    db.add(mixed)
    db.commit()

    with pytest.raises(RuntimeError, match="同时存在隔离归档与可编辑源码"):
        project_source_service.load_project_source_files(db, user, project.id)
    with pytest.raises(RuntimeError, match="同时存在隔离归档与可编辑源码"):
        project_source_service.build_source_archive(db, user, project.id)


def test_code_file_write_access_uses_project_row_lock() -> None:
    db = MagicMock()
    project = Project(id=7, user_id=1, project_name="locked", status="active")
    project_query = MagicMock()
    project_query.filter.return_value.with_for_update.return_value.first.return_value = project
    archive_query = MagicMock()
    archive_query.filter.return_value.first.return_value = None
    db.query.side_effect = [project_query, archive_query]

    result = code_file_service._check_project_access(
        db,
        User(id=1, username="owner", role="user", status=1),
        7,
    )

    assert result is project
    project_query.filter.return_value.with_for_update.assert_called_once_with()
