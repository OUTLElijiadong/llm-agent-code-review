"""
压缩包自动解压工具模块

支持 zip / tar / tar.gz / tgz / tar.bz2 / tar.xz 格式的安全解压。
严格防护:
1. zip slip 路径穿越攻击(拒绝 `..` 与绝对路径)
2. 解压文件数量上限(默认 100)
3. 解压总大小上限(500MB)
4. 压缩包成员大小上限(100MB；普通文件直传仍保持 10MB)
5. 隐藏文件/敏感目录过滤(.git/、.svn/、__pycache__/ 等)
"""
import io
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass
from typing import List, Optional

from app.core.exceptions import ValidationError
# ============ 解压限制配置 ============
MAX_EXTRACTED_FILES = 10_000     # 源码仓库允许的解压后文件数量上限
MAX_TOTAL_SIZE = 500 * 1024 * 1024  # 与项目总大小上限一致(500MB)
MAX_SINGLE_FILE_SIZE = 100 * 1024 * 1024  # 100MB 压缩包成员上限

# 支持的压缩包扩展名(按优先级匹配,带复合扩展名优先)
ARCHIVE_EXTENSIONS = (
    ".zip",
    ".tar.gz", ".tgz",
    ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz",
    ".tar",
)

# 敏感文件名正则:防止解压出可执行脚本等危险文件(仅警告,不拒绝)
_SENSITIVE_FILE_RE = re.compile(
    r"(^|/)(\.env|\.ssh|\.aws|\.gitconfig|id_rsa|id_dsa|credentials)(/|$)",
    re.IGNORECASE,
)

# 归档导入要保持源码树完整，不能复用普通单文件上传对 dist/build/target
# 的过滤规则；这些名称在真实项目中经常是业务源码目录。这里只过滤明确的
# 版本库、解释器缓存和编辑器元数据。
_ARCHIVE_METADATA_DIRS = re.compile(
    r"(^|/)(\.git|\.svn|\.hg|__pycache__|\.idea|\.vscode)(/|$)",
    re.IGNORECASE,
)


@dataclass
class ExtractedFile:
    """解压后的单个文件描述

    Attributes:
        name: 文件名(含扩展名,已去除路径)
        path: 逻辑相对路径(已校验安全,如 "src/main.py")
        content: 文件文本内容(UTF-8);若为二进制则填充空字符串
        raw_bytes: 原始字节(仅二进制文件填充,文本文件为 None)
        language: 推断的编程语言标识
        size: 文件字节数
        is_binary: 是否二进制文件
    """
    name: str
    path: str
    content: str = ""
    raw_bytes: Optional[bytes] = None
    language: str = "plaintext"
    size: int = 0
    is_binary: bool = False


def is_archive(filename: str) -> bool:
    """判断文件名是否为支持的压缩包格式

    Args:
        filename: 文件名(含扩展名)

    Returns:
        bool: True 表示是支持的压缩包格式
    """
    if not filename:
        return False
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def extract_archive(raw: bytes, filename: str) -> List[ExtractedFile]:
    """解压压缩包字节流,返回安全的文件列表

    Args:
        raw: 压缩包原始字节
        filename: 压缩包文件名(用于判断格式)

    Returns:
        List[ExtractedFile]: 解压后的文件列表

    Raises:
        ValidationError: 解压失败、文件数量超限、大小超限、路径不安全
    """
    if not raw:
        raise ValidationError("压缩包内容为空", code=40001)

    lower = filename.lower()
    if lower.endswith(".zip"):
        files = _extract_zip(raw)
    elif lower.endswith((".tar.gz", ".tgz")):
        files = _extract_tar(raw, mode="r:gz")
    elif lower.endswith((".tar.bz2", ".tbz2")):
        files = _extract_tar(raw, mode="r:bz2")
    elif lower.endswith((".tar.xz", ".txz")):
        files = _extract_tar(raw, mode="r:xz")
    elif lower.endswith(".tar"):
        files = _extract_tar(raw, mode="r:")
    else:
        raise ValidationError(f"不支持的压缩包格式: {filename}", code=41500)

    if not files:
        raise ValidationError("压缩包内没有可用的文件(可能全部被安全过滤)", code=40001)
    return files


# ============ 内部实现 ============

def _extract_zip(raw: bytes) -> List[ExtractedFile]:
    """解压 zip 格式

    Args:
        raw: zip 字节流

    Returns:
        List[ExtractedFile]: 解压后的文件列表

    Raises:
        ValidationError: zip 损坏/路径不安全/超限
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise ValidationError(f"zip 文件已损坏: {e}", code=40001)

    infos = [i for i in zf.infolist() if not i.is_dir()]
    _check_file_count(len(infos))

    total_size = 0
    results: List[ExtractedFile] = []
    for info in infos:
        name = info.filename or ""
        safe_path = _validate_path(name)
        if safe_path is None:
            continue  # 被安全过滤跳过

        size = info.file_size
        _check_single_size(size, safe_path)
        total_size += size
        _check_total_size(total_size)

        with zf.open(info) as f:
            data = f.read()
        ext_file = _build_extracted_file(safe_path, data)
        results.append(ext_file)
    return results


def _extract_tar(raw: bytes, mode: str) -> List[ExtractedFile]:
    """解压 tar / tar.gz / tar.bz2 / tar.xz 格式

    Args:
        raw: tar 字节流
        mode: tarfile 打开模式(r:/r:gz/r:bz2/r:xz)

    Returns:
        List[ExtractedFile]: 解压后的文件列表

    Raises:
        ValidationError: tar 损坏/路径不安全/超限
    """
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode=mode)
    except tarfile.TarError as e:
        raise ValidationError(f"tar 文件已损坏: {e}", code=40001)

    members = [m for m in tf.getmembers() if m.isfile()]
    _check_file_count(len(members))

    total_size = 0
    results: List[ExtractedFile] = []
    for m in members:
        safe_path = _validate_path(m.name)
        if safe_path is None:
            continue
        size = m.size
        _check_single_size(size, safe_path)
        total_size += size
        _check_total_size(total_size)

        f = tf.extractfile(m)
        if f is None:
            continue
        data = f.read()
        ext_file = _build_extracted_file(safe_path, data)
        results.append(ext_file)
    return results


def _validate_path(name: str) -> Optional[str]:
    """校验解压路径安全性,返回标准化相对路径

    Args:
        name: 压缩包内的原始路径

    Returns:
        Optional[str]: 安全的相对路径;若被安全过滤则返回 None

    Raises:
        ValidationError: 路径包含 `..` 或绝对路径(zip slip 攻击)
    """
    if not name:
        return None
    # 统一为 POSIX 风格
    normalized = name.replace("\\", "/").lstrip("/")
    if not normalized:
        return None
    # zip slip 防护:拒绝 `..` 路径段
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise ValidationError(
            f"压缩包包含不安全路径(可能是 zip slip 攻击): {name}", code=40001
        )
    # 拒绝绝对路径(Windows 盘符)
    if re.match(r"^[a-zA-Z]:", normalized):
        raise ValidationError(
            f"压缩包包含绝对路径(可能是 zip slip 攻击): {name}", code=40001
        )
    # 过滤敏感目录
    if _ARCHIVE_METADATA_DIRS.search(normalized):
        return None
    # 过滤隐藏文件(.gitignore/.env 等敏感配置)
    if _SENSITIVE_FILE_RE.search(normalized):
        return None
    return normalized


def _build_extracted_file(safe_path: str, data: bytes) -> ExtractedFile:
    """根据解压字节构建 ExtractedFile 对象

    Args:
        safe_path: 已校验的安全相对路径
        data: 文件原始字节

    Returns:
        ExtractedFile: 填充后的文件描述对象
    """
    from app.ai.language_detector import detect_language
    from app.utils.encoding_utils import to_utf8

    name = os.path.basename(safe_path)
    language = detect_language(name)
    is_bin = _is_binary_data(data)
    if is_bin:
        # 二进制文件:content 存 base64(向后兼容),raw_bytes 存原始字节
        content = to_utf8(data)
        return ExtractedFile(
            name=name,
            path=safe_path,
            content=content,
            raw_bytes=data,
            language=language,
            size=len(data),
            is_binary=True,
        )
    # 文本文件:content 存 UTF-8 文本
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        import chardet
        guess = chardet.detect(data)
        enc = guess.get("encoding") or "utf-8"
        text = data.decode(enc, errors="replace")
    return ExtractedFile(
        name=name,
        path=safe_path,
        content=text,
        raw_bytes=None,
        language=language,
        size=len(data),
        is_binary=False,
    )


def _is_binary_data(data: bytes) -> bool:
    """判断字节流是否为二进制(含 null 字节则视为二进制)

    Args:
        data: 待判断字节

    Returns:
        bool: True 表示二进制
    """
    if not data:
        return False
    return b"\x00" in data[:8192]


def _check_file_count(count: int) -> None:
    """校验解压后文件数量是否超限

    Args:
        count: 压缩包内的文件数量

    Raises:
        ValidationError: 超过 MAX_EXTRACTED_FILES 上限
    """
    if count > MAX_EXTRACTED_FILES:
        raise ValidationError(
            f"压缩包内文件数量 {count} 超过上限 {MAX_EXTRACTED_FILES}",
            code=40001,
        )


def _check_single_size(size: int, path: str) -> None:
    """校验单个文件大小是否超限

    Args:
        size: 文件字节数
        path: 文件路径(用于错误提示)

    Raises:
        ValidationError: 超过 MAX_SINGLE_FILE_SIZE 上限
    """
    if size > MAX_SINGLE_FILE_SIZE:
        raise ValidationError(
            f"压缩包内文件 {path} 大小 {size} 字节超过单文件上限 {MAX_SINGLE_FILE_SIZE} 字节",
            code=40001,
        )


def _check_total_size(total: int) -> None:
    """校验解压累计总大小是否超限

    Args:
        total: 累计字节数

    Raises:
        ValidationError: 超过 MAX_TOTAL_SIZE 上限
    """
    if total > MAX_TOTAL_SIZE:
        raise ValidationError(
            f"压缩包解压后总大小 {total} 字节超过上限 {MAX_TOTAL_SIZE} 字节",
            code=40001,
        )
