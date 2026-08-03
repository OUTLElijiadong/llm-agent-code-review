"""统一的源码归档识别与安全解包。

归档格式由 libarchive 自动识别。上传大小不设置业务上限；仍保留文件数量、
路径、文件类型和解压倍率约束，防止路径穿越、链接写出和压缩炸弹。
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

import libarchive
from libarchive.exception import ArchiveError

from app.core.exceptions import ValidationError


MAX_EXTRACTED_FILES = 10_000
MAX_COMPRESSION_RATIO = 1_000
MIN_RATIO_GUARD_BYTES = 256 * 1024 * 1024

# libarchive 常见源码归档格式。复合后缀必须排在单后缀之前。
ARCHIVE_EXTENSIONS = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz", ".tar.lzma",
    ".tar.lzip", ".tgz", ".tbz2", ".txz", ".tzst", ".tlz",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".zst",
    ".lz", ".lzma", ".lzip", ".z", ".cpio", ".cab", ".ar", ".xar",
    ".lha", ".lzh", ".iso",
)

_RAW_COMPRESSED_EXTENSIONS = (
    ".gz", ".bz2", ".xz", ".zst", ".lz", ".lzma", ".lzip", ".z",
)

_SENSITIVE_FILE_RE = re.compile(
    r"(^|/)(\.env|\.ssh|\.aws|\.gitconfig|id_rsa|id_dsa|credentials)(/|$)",
    re.IGNORECASE,
)
_ARCHIVE_METADATA_DIRS = re.compile(
    r"(^|/)(\.git|\.svn|\.hg|__pycache__|\.idea|\.vscode)(/|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArchiveMember:
    """经过路径和文件类型校验的归档成员。"""

    path: str
    content: bytes


@dataclass
class ExtractedFile:
    """供 CodeFile 入库使用的归档成员。"""

    name: str
    path: str
    content: str = ""
    raw_bytes: Optional[bytes] = None
    language: str = "plaintext"
    size: int = 0
    is_binary: bool = False


def is_archive(filename: str) -> bool:
    """根据文件名判断是否属于支持的源码归档格式。"""
    lower = (filename or "").strip().lower()
    return bool(lower) and lower.endswith(ARCHIVE_EXTENSIONS)


def read_archive_members(
    raw: bytes,
    filename: str,
    *,
    filter_sensitive: bool = True,
    strict_paths: bool = False,
) -> tuple[list[ArchiveMember], dict]:
    """用 libarchive 读取归档并返回安全成员及解包摘要。"""
    if not raw:
        raise ValidationError("压缩包内容为空", code=40001)
    if not is_archive(filename):
        raise ValidationError(f"不支持的压缩包格式: {filename}", code=41500)

    raw_stream = _is_raw_compressed(filename)
    format_name = "raw" if raw_stream else "all"
    results: list[ArchiveMember] = []
    seen: set[str] = set()
    regular_count = 0
    expanded_size = 0
    max_member_size = 0
    ratio_guard = max(MIN_RATIO_GUARD_BYTES, len(raw) * MAX_COMPRESSION_RATIO)

    try:
        with libarchive.memory_reader(raw, format_name=format_name) as archive:
            for entry in archive:
                if entry.isdir:
                    continue
                if (
                    not (entry.isfile or entry.isreg)
                    or entry.issym
                    or entry.islnk
                    or entry.linkpath
                ):
                    raise ValidationError("压缩包包含链接或特殊文件", code=40001)

                regular_count += 1
                _check_file_count(regular_count)
                original_path = str(entry.pathname or "")
                if raw_stream and original_path in {"", "data"}:
                    original_path = _raw_member_name(filename)
                safe_path = _validate_path(
                    original_path,
                    filter_sensitive=filter_sensitive,
                    strict=strict_paths,
                )
                if safe_path is None:
                    # libarchive 会在读取下一条记录时跳过当前成员数据。
                    continue

                collision_key = unicodedata.normalize("NFC", safe_path).casefold()
                if collision_key in seen:
                    raise ValidationError("压缩包包含重复或大小写冲突路径", code=40001)
                seen.add(collision_key)

                chunks: list[bytes] = []
                member_size = 0
                for block in entry.get_blocks():
                    member_size += len(block)
                    expanded_size += len(block)
                    if expanded_size > ratio_guard:
                        raise ValidationError(
                            f"压缩包解压倍率超过安全上限 {MAX_COMPRESSION_RATIO}x",
                            code=40001,
                        )
                    chunks.append(block)
                declared_size = entry.size
                if declared_size is not None and declared_size >= 0 and member_size != declared_size:
                    raise ValidationError(
                        f"压缩包成员大小与声明不一致: {safe_path}",
                        code=40001,
                    )
                content = b"".join(chunks)
                max_member_size = max(max_member_size, member_size)
                results.append(ArchiveMember(path=safe_path, content=content))
    except ValidationError:
        raise
    except (ArchiveError, OSError, ValueError) as exc:
        detail = str(exc).lower()
        if "passphrase" in detail or "encrypted" in detail or "encryption" in detail:
            message = "不支持加密压缩包"
        else:
            message = f"压缩包已损坏或当前运行库不支持该格式: {filename}"
        raise ValidationError(message, code=40001) from exc

    if not results:
        raise ValidationError("压缩包内没有可用的文件(可能全部被安全过滤)", code=40001)
    return results, {
        "file_count": len(results),
        "expanded_size": expanded_size,
        "max_member_size": max_member_size,
        "max_compression_ratio": round(expanded_size / max(1, len(raw)), 4),
    }


def extract_archive(raw: bytes, filename: str) -> List[ExtractedFile]:
    """解包并转换为可直接写入 CodeFile 的文件对象。"""
    members, _ = read_archive_members(raw, filename)
    return [_build_extracted_file(member.path, member.content) for member in members]


def _is_raw_compressed(filename: str) -> bool:
    lower = filename.lower()
    if lower.endswith((
        ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz",
        ".tar.lzma", ".tar.lzip",
    )):
        return False
    return lower.endswith(_RAW_COMPRESSED_EXTENSIONS)


def _raw_member_name(filename: str) -> str:
    base = os.path.basename(filename.replace("\\", "/"))
    lower = base.lower()
    for suffix in _RAW_COMPRESSED_EXTENSIONS:
        if lower.endswith(suffix):
            candidate = base[: -len(suffix)]
            return candidate or "data"
    return base or "data"


def _validate_path(
    name: str,
    *,
    filter_sensitive: bool = True,
    strict: bool = False,
) -> Optional[str]:
    """校验成员路径，拒绝绝对路径、设备路径和路径穿越。"""
    if not name or "\x00" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValidationError("压缩包包含非法路径", code=40001)
    if strict and "\\" in name:
        raise ValidationError("压缩包包含非法或绝对路径", code=40001)
    normalized = unicodedata.normalize("NFC", name.replace("\\", "/"))
    if normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized):
        raise ValidationError(
            f"压缩包包含绝对路径(可能是 zip slip 攻击): {name}", code=40001
        )
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(
            f"压缩包包含不安全路径(可能是 zip slip 攻击): {name}", code=40001
        )
    if len(normalized.encode("utf-8")) > 2_048:
        raise ValidationError("压缩包成员路径超过 2048 字节", code=40001)
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise ValidationError("压缩包成员路径段超过 255 字节", code=40001)
    if re.fullmatch(r"[A-Za-z]:", parts[0]) or any(":" in part for part in parts):
        raise ValidationError("压缩包包含盘符或设备路径", code=40001)
    if filter_sensitive and (
        _ARCHIVE_METADATA_DIRS.search(normalized) or _SENSITIVE_FILE_RE.search(normalized)
    ):
        return None
    return normalized


def _build_extracted_file(safe_path: str, data: bytes) -> ExtractedFile:
    from app.ai.language_detector import detect_language
    from app.utils.encoding_utils import to_utf8

    name = os.path.basename(safe_path)
    language = detect_language(name)
    is_binary = _is_binary_data(data)
    if is_binary:
        return ExtractedFile(
            name=name,
            path=safe_path,
            content=to_utf8(data),
            raw_bytes=data,
            language=language,
            size=len(data),
            is_binary=True,
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        import chardet

        guess = chardet.detect(data)
        text = data.decode(guess.get("encoding") or "utf-8", errors="replace")
    return ExtractedFile(
        name=name,
        path=safe_path,
        content=text,
        language=language,
        size=len(data),
        is_binary=False,
    )


def _is_binary_data(data: bytes) -> bool:
    return bool(data) and b"\x00" in data[:8192]


def _check_file_count(count: int) -> None:
    if count > MAX_EXTRACTED_FILES:
        raise ValidationError(
            f"压缩包内文件数量 {count} 超过上限 {MAX_EXTRACTED_FILES}",
            code=40001,
        )
