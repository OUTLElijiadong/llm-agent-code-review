"""远程源码归档应流式写入临时文件，而不是累计内存分块。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from app.services import project_source_service
from app.utils.public_http import PinnedPublicUrl


def test_remote_archive_streams_to_temporary_file_and_cleans_up(monkeypatch, tmp_path) -> None:
    response = MagicMock(
        status_code=200,
        headers=httpx.Headers({"content-length": "6"}),
    )
    response.iter_bytes.return_value = iter([b"PK", b"00", b"12"])

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
    monkeypatch.setattr(project_source_service.httpx, "Client", lambda **_kwargs: FakeClient())

    persisted_path: Path | None = None
    with project_source_service.download_remote_archive_to_temp(
        "https://example.com/source.zip",
        temp_dir=tmp_path,
    ) as downloaded:
        persisted_path = downloaded.path
        assert downloaded.filename == "source.zip"
        assert downloaded.byte_size == 6
        assert downloaded.path.read_bytes() == b"PK0012"

    assert persisted_path is not None
    assert not persisted_path.exists()


def test_remote_archive_accepts_exact_peak_boundary(monkeypatch, tmp_path) -> None:
    response = MagicMock(status_code=200, headers=httpx.Headers({"content-length": "6"}))
    response.iter_bytes.return_value = iter([b"PK", b"00", b"12"])

    class Context:
        def __enter__(self):
            return response

        def __exit__(self, *_args):
            return None

    class Client(Context):
        def __enter__(self):
            return self

        def stream(self, *_args, **_kwargs):
            return Context()

    monkeypatch.setattr(project_source_service, "MAX_REMOTE_BYTES", 6)
    monkeypatch.setattr(
        project_source_service,
        "_pin_remote_url",
        lambda url: PinnedPublicUrl(url, url, "example.com", "example.com", "93.184.216.34"),
    )
    monkeypatch.setattr(project_source_service.httpx, "Client", lambda **_kwargs: Client())

    with project_source_service.download_remote_archive_to_temp(
        "https://example.com/source.zip",
        temp_dir=tmp_path,
    ) as downloaded:
        assert downloaded.byte_size == 6
        assert downloaded.path.stat().st_size == 6


def test_declared_oversize_response_is_rejected_before_body_read(monkeypatch, tmp_path) -> None:
    response = MagicMock(status_code=200, headers=httpx.Headers({"content-length": "7"}))

    class Context:
        def __enter__(self):
            return response

        def __exit__(self, *_args):
            return None

    class Client(Context):
        def __enter__(self):
            return self

        def stream(self, *_args, **_kwargs):
            return Context()

    monkeypatch.setattr(project_source_service, "MAX_REMOTE_BYTES", 6)
    monkeypatch.setattr(
        project_source_service,
        "_pin_remote_url",
        lambda url: PinnedPublicUrl(url, url, "example.com", "example.com", "93.184.216.34"),
    )
    monkeypatch.setattr(project_source_service.httpx, "Client", lambda **_kwargs: Client())

    with pytest.raises(project_source_service.ValidationError, match="超过"):
        with project_source_service.download_remote_archive_to_temp(
            "https://example.com/source.zip",
            temp_dir=tmp_path,
        ):
            pass

    response.iter_bytes.assert_not_called()
    assert list(tmp_path.iterdir()) == []
