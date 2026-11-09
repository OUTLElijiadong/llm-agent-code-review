from __future__ import annotations

from tempfile import SpooledTemporaryFile

import pytest
from python_multipart.exceptions import FileError
from starlette.datastructures import Headers

from app.core.exceptions import PayloadTooLargeError, ValidationError
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


@pytest.mark.asyncio
async def test_limited_multipart_propagates_unexpected_stream_error_and_closes_file(monkeypatch):
    headers, body = _multipart_body([('partial.py', b'123')])
    created_files = []

    def tracking_spooled_file(*args, **kwargs):
        temporary_file = SpooledTemporaryFile(*args, **kwargs)
        created_files.append(temporary_file)
        return temporary_file

    monkeypatch.setattr('starlette.formparsers.SpooledTemporaryFile', tracking_spooled_file)

    async def interrupted_stream():
        yield body[:-len(b'--prism-boundary--\r\n')]
        raise RuntimeError('unexpected transport failure')

    with pytest.raises(RuntimeError, match='unexpected transport failure'):
        await parse_limited_multipart(headers, interrupted_stream(), max_file_bytes=1024, max_files=10)
    assert created_files and all(file.closed for file in created_files)


@pytest.mark.asyncio
async def test_limited_multipart_rejects_complete_file_followed_by_unfinished_file(monkeypatch):
    headers, complete = _multipart_body([('complete.py', b'123'), ('truncated.py', b'456')])
    body = complete[:-len(b'--prism-boundary--\r\n')]
    created = []
    def tracking_spool(*args, **kwargs):
        temporary_file = SpooledTemporaryFile(*args, **kwargs)
        created.append(temporary_file)
        return temporary_file
    monkeypatch.setattr('starlette.formparsers.SpooledTemporaryFile', tracking_spool)
    with pytest.raises(ValidationError, match='未完整结束'):
        await parse_limited_multipart(headers, _stream(body), max_file_bytes=10, max_files=10)
    assert len(created) == 2
    assert all(item.closed for item in created)


@pytest.mark.asyncio
async def test_limited_multipart_rejects_truncated_fields_without_any_files():
    headers = Headers({'content-type': 'multipart/form-data; boundary=abc'})
    body = b'--abc\r\nContent-Disposition: form-data; name="project_id"\r\n\r\n7'
    with pytest.raises(ValidationError, match='未完整结束'):
        await parse_limited_multipart(headers, _stream(body), max_file_bytes=10, max_files=10)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [b'a', b'a' * 70, b'a b', b"'()+_,-./:=?9"])
async def test_valid_multipart_boundaries_include_quoted_spaces_and_seventy_bytes(boundary):
    headers = Headers(raw=[(b'content-type', b'multipart/form-data; boundary="' + boundary + b'"')])
    body = (b'--' + boundary + b'\r\nContent-Disposition: form-data; name="files"; '
            b'filename="a.py"\r\n\r\ndata\r\n--' + boundary + b'--\r\n')
    form = await parse_limited_multipart(headers, _stream(body), max_file_bytes=10, max_files=2)
    try:
        assert await form.getlist('files')[0].read() == b'data'
    finally:
        await form.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [b'', b'a' * 71, b'a' * 257, b'a ', b'a@b', b'a\tb', b'\xff', b'a\x00b'])
async def test_invalid_multipart_boundary_is_client_error_without_echo(boundary, caplog):
    headers = Headers(raw=[(b'content-type', b'multipart/form-data; boundary="' + boundary + b'"')])
    async def must_not_read():
        raise AssertionError('invalid boundary reached request body')
        yield b'private-payload-marker'
    with pytest.raises(ValidationError) as exc:
        await parse_limited_multipart(headers, must_not_read(), max_file_bytes=10, max_files=2)
    assert 'private-payload-marker' not in str(exc.value) and 'private-payload-marker' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type', [
    'multipart/form-data; boundary=alpha; boundary=beta',
    'multipart/form-data; boundary=alpha; BOUNDARY=alpha',
    'multipart/form-data; boundary=alpha; BOUNDARY=',
    'multipart/form-data; x="semi; boundary=not-a-parameter"; boundary=alpha; boundary=beta',
    'multipart/form-data; x="quoted \\\"; boundary=inside"; boundary=alpha; BOUNDARY=beta',
])
async def test_duplicate_boundary_rejected_before_reading_body(content_type):
    async def must_not_read():
        raise AssertionError('ambiguous boundary reached request body')
        yield b'unreachable'
    with pytest.raises(ValidationError):
        await parse_limited_multipart(Headers({'content-type': content_type}), must_not_read(),
                                      max_file_bytes=10, max_files=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('second', [b'multipart/form-data; boundary=abc', b'multipart/form-data; boundary=xyz'])
@pytest.mark.parametrize('field_name', [b'content-type', b'Content-Type'])
async def test_duplicate_content_type_fields_rejected_before_reading_body(second, field_name):
    async def must_not_read():
        raise AssertionError('ambiguous content-type reached request body')
        yield b'unreachable'
    headers = Headers(raw=[(b'content-type', b'multipart/form-data; boundary=abc'), (field_name, second)])
    with pytest.raises(ValidationError):
        await parse_limited_multipart(headers, must_not_read(), max_file_bytes=10, max_files=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type', [
    'multipart/form-data; boundary=abc; x="semi; boundary=not-a-parameter"',
    'multipart/form-data; x="quoted \\\"; boundary=inside"; boundary=abc',
    "multipart/form-data; boundary*=utf-8''ignored; boundary=abc",
])
async def test_unrelated_quoted_or_extended_parameter_is_not_duplicate(content_type):
    body = (b'--abc\r\nContent-Disposition: form-data; name="files"; filename="a.py"\r\n\r\n'
            b'data\r\n--abc--\r\n')
    form = await parse_limited_multipart(Headers({'content-type': content_type}), _stream(body),
                                         max_file_bytes=10, max_files=2)
    try:
        assert await form.getlist('files')[0].read() == b'data'
    finally:
        await form.close()


@pytest.mark.asyncio
async def test_parser_file_error_is_not_misreported_as_malformed_input(monkeypatch):
    from app.utils.multipart_limits import LimitedMultiPartParser

    async def fail(_parser):
        raise FileError('synthetic file failure')
    monkeypatch.setattr(LimitedMultiPartParser, 'parse', fail)
    headers = Headers({'content-type': 'multipart/form-data; boundary=abc'})
    with pytest.raises(FileError, match='synthetic file failure'):
        await parse_limited_multipart(headers, _stream(b''), max_file_bytes=10, max_files=2)
