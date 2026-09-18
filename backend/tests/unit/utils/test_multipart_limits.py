from __future__ import annotations

from tempfile import SpooledTemporaryFile

import pytest
from starlette.datastructures import Headers

from app.core.exceptions import PayloadTooLargeError
from app.utils.multipart_limits import FILE_SPOOL_MEMORY_BYTES, parse_limited_multipart


def _multipart_body(files: list[tuple[str, bytes]]) -> tuple[Headers, bytes]:
    boundary = "prism-boundary"
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"project_id\"\r\n\r\n7\r\n".encode()
    ]
    for filename, content in files:
        chunks.extend([
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
                f"filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n"
            ).encode(),
            content,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    return Headers({"content-type": f"multipart/form-data; boundary={boundary}"}), body


async def _stream(body: bytes):
    midpoint = max(1, len(body) // 2)
    yield body[:midpoint]
    yield body[midpoint:]


@pytest.mark.asyncio
async def test_limited_multipart_accepts_multiple_files_under_per_file_limit():
    headers, body = _multipart_body([("a.py", b"123"), ("b.py", b"456")])
    form = await parse_limited_multipart(headers, _stream(body), max_file_bytes=4, max_files=10)
    files = form.getlist("files")
    try:
        assert len(files) == 2
        assert await files[0].read() == b"123"
        assert await files[1].read() == b"456"
    finally:
        for file in files:
            await file.close()


@pytest.mark.asyncio
async def test_limited_multipart_rejects_first_byte_over_per_file_limit():
    headers, body = _multipart_body([("too-large.py", b"12345"), ("later.py", b"ok")])

    with pytest.raises(PayloadTooLargeError) as exc_info:
        await parse_limited_multipart(headers, _stream(body), max_file_bytes=4, max_files=10)

    assert exc_info.value.http_status == 413


@pytest.mark.asyncio
async def test_limited_multipart_spools_larger_small_files_out_of_memory():
    content = b"x" * (FILE_SPOOL_MEMORY_BYTES + 1)
    headers, body = _multipart_body([("medium.py", content)])
    form = await parse_limited_multipart(
        headers,
        _stream(body),
        max_file_bytes=20 * 1024 * 1024,
        max_files=1000,
    )
    upload = form.getlist("files")[0]
    try:
        assert upload._in_memory is False
        assert await upload.read() == content
    finally:
        await form.close()


@pytest.mark.asyncio
async def test_limited_multipart_closes_partial_files_when_upstream_stream_fails(monkeypatch):
    headers, body = _multipart_body([("partial.py", b"123")])
    created_files = []

    def tracking_spooled_file(*args, **kwargs):
        temporary_file = SpooledTemporaryFile(*args, **kwargs)
        created_files.append(temporary_file)
        return temporary_file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking_spooled_file)

    async def interrupted_stream():
        yield body[:-len(b"--prism-boundary--\r\n")]
        raise PayloadTooLargeError("上传请求超过容量上限")

    with pytest.raises(PayloadTooLargeError):
        await parse_limited_multipart(
            headers,
            interrupted_stream(),
            max_file_bytes=20 * 1024 * 1024,
            max_files=1000,
        )

    assert created_files
    assert all(temporary_file.closed for temporary_file in created_files)
