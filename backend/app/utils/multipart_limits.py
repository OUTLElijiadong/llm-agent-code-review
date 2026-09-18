"""带逐文件上限的流式 multipart 解析。"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from starlette.datastructures import FormData, Headers
from starlette.formparsers import MultiPartException, MultiPartParser

from app.core.exceptions import PayloadTooLargeError, ValidationError

_FILE_TOO_LARGE_MARKER = "PRISM_FILE_TOO_LARGE"
FILE_SPOOL_MEMORY_BYTES = 64 * 1024


class LimitedMultiPartParser(MultiPartParser):
    """在 Starlette 写入临时文件前按 multipart 文件部分累计字节。"""

    # Starlette <=0.27 使用 max_file_size，较新版本改为 spool_max_size。
    max_file_size = FILE_SPOOL_MEMORY_BYTES
    spool_max_size = FILE_SPOOL_MEMORY_BYTES

    def __init__(self, *args, max_file_bytes: int, **kwargs) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须大于 0")
        super().__init__(*args, **kwargs)
        self.max_file_bytes = max_file_bytes
        self._current_file_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_bytes += end - start
            if self._current_file_bytes > self.max_file_bytes:
                raise MultiPartException(_FILE_TOO_LARGE_MARKER)
        super().on_part_data(data, start, end)

    def close_pending_files(self) -> None:
        """关闭解析期间创建的文件，包括上游流异常和任务取消分支。"""
        for temporary_file in self._files_to_close_on_error:
            temporary_file.close()


async def parse_limited_multipart(
    headers: Headers,
    stream: AsyncGenerator[bytes, None],
    *,
    max_file_bytes: int,
    max_files: int,
) -> FormData:
    """解析 multipart，并把逐文件超限转换成统一 413 业务异常。"""
    parser = LimitedMultiPartParser(
        headers,
        stream,
        max_files=max_files,
        max_fields=10,
        max_file_bytes=max_file_bytes,
    )
    try:
        return await parser.parse()
    except MultiPartException as exc:
        if str(exc) == _FILE_TOO_LARGE_MARKER:
            raise PayloadTooLargeError(
                f"目录中存在超过 {max_file_bytes // (1024 * 1024)} MB 上限的文件",
                detail={"max_file_bytes": max_file_bytes},
            ) from exc
        raise ValidationError(f"multipart 上传格式无效: {exc}") from exc
    except BaseException:
        parser.close_pending_files()
        raise
