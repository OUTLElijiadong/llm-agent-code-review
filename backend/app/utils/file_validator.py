"""
文件名和大小校验工具模块
"""
import os
import re

from app.core.exceptions import ValidationError

BLOCK_DIRS = re.compile(
    r"(^|/)(\.git|\.svn|\.hg|node_modules|__pycache__|\.next|dist|build|target|\.idea|\.vscode)(/|$)"
)


def validate_filename(name: str, allowed_extensions: list[str]) -> str:
    """校验文件名合法性

    Args:
        name: 文件名(可能是相对路径)
        allowed_extensions: 允许的扩展名列表,含"*"表示不限制

    Returns:
        str: 清除路径后的安全文件名

    Raises:
        ValidationError: 文件名非法或扩展名不支持
    """
    base = os.path.basename(name).strip()
    if not base or len(base) > 255:
        raise ValidationError("文件名不合法", code=40001)
    if '\x00' in base:
        raise ValidationError("文件名包含非法字符(null)", code=40001)
    if _is_system_path(name):
        raise ValidationError(f"不允许上传系统目录中的文件: {name}", code=40001)
    ext = os.path.splitext(base)[1].lower()
    if not ext:
        return base
    if ext not in allowed_extensions and "*" not in allowed_extensions:
        raise ValidationError(f"不支持的文件类型 {ext}（{name}）", code=41500)
    return base


def _is_system_path(path: str) -> bool:
    """检查路径是否位于系统/构建目录中

    Args:
        path: 相对文件路径

    Returns:
        bool: True表示应阻止上传
    """
    normalized = path.replace("\\", "/")
    return bool(BLOCK_DIRS.search(normalized))


def validate_size(size: int, limit: int) -> None:
    """校验文件大小是否超出限制

    Args:
        size: 文件字节数
        limit: 最大字节限制

    Raises:
        ValidationError: 超出大小限制
    """
    if size > limit:
        kb = limit / 1024
        raise ValidationError(f"文件超过 {kb:.0f}KB 大小限制", code=41301)
