from io import BytesIO

import pytest

from app.core.exceptions import PayloadTooLargeError
from app.utils.upload_reader import READ_CHUNK_BYTES, read_limited


class RecordingStream(BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def test_read_limited_reads_in_bounded_chunks() -> None:
    stream = RecordingStream(b"a" * (READ_CHUNK_BYTES + 3))
    result = read_limited(stream, max_bytes=READ_CHUNK_BYTES + 3, filename="source.zip")
    assert result == b"a" * (READ_CHUNK_BYTES + 3)
    assert stream.read_sizes
    assert max(stream.read_sizes) <= READ_CHUNK_BYTES


def test_read_limited_rejects_first_byte_over_limit() -> None:
    stream = RecordingStream(b"12345")
    with pytest.raises(PayloadTooLargeError) as exc_info:
        read_limited(stream, max_bytes=4, filename="source.zip")
    assert exc_info.value.http_status == 413
    assert stream.read_sizes == [5]
