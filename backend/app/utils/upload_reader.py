"""上传文件的有界读取工具。"""
from __future__ import annotations

from typing import BinaryIO

from app.core.exceptions import PayloadTooLargeError

READ_CHUNK_BYTES = 1024 * 1024


def read_limited(stream: BinaryIO, *, max_bytes: int, filename: str = "") -> bytes:
    """分块读取并在超过上限时立即拒绝，避免无界 ``read()``。"""
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(READ_CHUNK_BYTES, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            label = filename or "上传文件"
            raise PayloadTooLargeError(
                f"{label} 超过 {max_bytes // (1024 * 1024)} MB 上传上限",
                detail={"max_bytes": max_bytes},
            )
        chunks.append(chunk)
    return b"".join(chunks)
