"""带逐文件上限的流式 multipart 解析。"""
from __future__ import annotations

import re
from collections.abc import AsyncGenerator

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.core.exceptions import PayloadTooLargeError, ValidationError

_FILE_TOO_LARGE_MARKER = "PRISM_FILE_TOO_LARGE"
FILE_SPOOL_MEMORY_BYTES = 64 * 1024
_BOUNDARY = re.compile(rb"[A-Za-z0-9'()+_,./:=? \-]{0,69}[A-Za-z0-9'()+_,./:=?\-]")


def _multipart_boundary(raw_content_type: str) -> bytes:
    """Reject ambiguous boundary parameters without splitting quoted semicolons."""
    segments: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, char in enumerate(raw_content_type):
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ';' and not quoted:
            segments.append(raw_content_type[start:index])
            start = index + 1
    if quoted:
        raise ValidationError('multipart 上传边界无效')
    segments.append(raw_content_type[start:])
    matches = [segment for segment in segments[1:]
               if segment.partition('=')[0].strip().lower() == 'boundary']
    if len(matches) != 1:
        raise ValidationError('multipart 上传边界无效')
    _, full_parameters = parse_options_header(raw_content_type)
    _, selected_parameters = parse_options_header('multipart/form-data; ' + matches[0])
    boundary = full_parameters.get(b'boundary', b'')
    # Starlette uses parse_options_header too. If its interpretation differs
    # from the unique lexical parameter, reject rather than read an ambiguous body.
    if boundary != selected_parameters.get(b'boundary', b'') or _BOUNDARY.fullmatch(boundary) is None:
        raise ValidationError('multipart 上传边界无效')
    return boundary


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
        self._multipart_complete = False

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_bytes += end - start
            if self._current_file_bytes > self.max_file_bytes:
                raise MultiPartException(_FILE_TOO_LARGE_MARKER)
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        super().on_end()
        self._multipart_complete = True

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
    content_types = [value.decode('latin-1') for name, value in headers.raw if name.lower() == b'content-type']
    if len(content_types) != 1:
        raise ValidationError('multipart 上传格式无效')
    raw_content_type = content_types[0]
    content_type = raw_content_type.split(';', 1)[0].strip().lower()
    if content_type != 'multipart/form-data':
        raise ValidationError('multipart 上传格式无效')
    # RFC 2046 §5.1.1: 1–70 permitted ASCII characters, no trailing space.
    # Validate before reading the body; overlong boundaries otherwise cause a
    # python-multipart FormParserError, which is not a MultipartParseError.
    _multipart_boundary(raw_content_type)
    parser = LimitedMultiPartParser(
        headers,
        stream,
        max_files=max_files,
        max_fields=10,
        max_file_bytes=max_file_bytes,
    )
    try:
        form = await parser.parse()
        attached = {id(value.file) for _, value in form.multi_items() if isinstance(value, UploadFile)}
        unattached = any(id(file) not in attached for file in parser._files_to_close_on_error)
        if not parser._multipart_complete or unattached:
            parser.close_pending_files()
            raise ValidationError('multipart 上传未完整结束')
        return form
    except MultiPartException as exc:
        parser.close_pending_files()
        if str(exc) == _FILE_TOO_LARGE_MARKER:
            raise PayloadTooLargeError(
                f"目录中存在超过 {max_file_bytes // (1024 * 1024)} MB 上限的文件",
                detail={"max_file_bytes": max_file_bytes},
            ) from exc
        raise ValidationError(f"multipart 上传格式无效: {exc}") from exc
    except MultipartParseError:
        parser.close_pending_files()
        raise ValidationError('multipart 上传格式无效') from None
    except BaseException:
        parser.close_pending_files()
        raise
