"""项目源码归档与远程归档下载服务回归测试。"""
from __future__ import annotations

import io
import zipfile
from typing import Optional
from unittest.mock import MagicMock

import httpx
import pytest

from app.core.exceptions import ExternalServiceError, ValidationError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import project_source_service
from app.utils.encoding_utils import to_utf8


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
        project_source_service.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValidationError, match="内网或保留地址"):
        project_source_service._assert_public_url("https://example.com/source.zip")


def test_remote_download_revalidates_redirect_and_size(monkeypatch):
    visited = []
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

        def stream(self, method, url):
            assert method == "GET"
            visited.append(url)
            return ResponseContext(responses.pop(0))

    checked = []
    monkeypatch.setattr(project_source_service, "_assert_public_url", checked.append)
    monkeypatch.setattr(project_source_service.httpx, "Client", lambda **kwargs: FakeClient())

    body, name = project_source_service.download_remote_archive(
        "https://example.com/redirect",
    )

    assert body == b"PK00"
    assert name == "source.zip"
    assert checked == visited == [
        "https://example.com/redirect",
        "https://cdn.example.com/source.zip",
    ]


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

        def stream(self, method, _url):
            assert method == "GET"
            return ResponseContext()

    monkeypatch.setattr(project_source_service, "_assert_public_url", lambda _url: None)
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

        def stream(self, method, _url):
            assert method == "GET"
            return ResponseContext()

    monkeypatch.setattr(project_source_service, "MAX_REMOTE_BYTES", 4)
    monkeypatch.setattr(project_source_service, "_assert_public_url", lambda _url: None)
    monkeypatch.setattr(project_source_service.httpx, "Client", lambda **kwargs: FakeClient())

    with pytest.raises(ValidationError, match="超过 20MB"):
        project_source_service.download_remote_archive("https://example.com/source.zip")
