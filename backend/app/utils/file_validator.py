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


# ============ MIME 白名单与大小上限(T05 文件上传安全模块) ============

# MIME 白名单:允许上传的代码文件扩展名
ALLOWED_MIME_EXTENSIONS: frozenset = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cc", ".php", ".rb", ".vue",
    ".sql", ".sh", ".bash", ".zsh",
    ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".txt", ".xml", ".html", ".htm", ".css", ".scss", ".less",
    ".gitignore", ".dockerignore", ".env.example",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz",
    ".tbz2", ".txz", ".zst", ".tzst", ".lz", ".lzma", ".lzip", ".z",
    ".cpio", ".cab", ".ar", ".xar", ".lha", ".lzh", ".iso",
    # 压缩包由 archive_extractor/libarchive 统一识别和解包
    # T06: 图片资源文件(项目可能包含图片资源,需作为二进制文件入库)
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".bmp", ".webp",
})

# 拒绝的可执行文件扩展名
BLOCKED_EXECUTABLE_EXTENSIONS: frozenset = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bat", ".com", ".scr",
    ".msi", ".app", ".command", ".sh.bin", ".pif", ".jar",
})

def validate_mime(file_name: str) -> bool:
    """校验文件扩展名是否在 MIME 白名单内

    Args:
        file_name: 文件名(含扩展名)

    Returns:
        bool: True 表示允许上传, False 表示拒绝
    """
    if not file_name:
        return False
    base = os.path.basename(file_name).strip().lower()
    if not base:
        return False
    ext = os.path.splitext(base)[1]
    # 优先排除可执行文件(按扩展名与全名双重匹配)
    if ext in BLOCKED_EXECUTABLE_EXTENSIONS or base in BLOCKED_EXECUTABLE_EXTENSIONS:
        return False
    # 命中白名单:扩展名匹配 或 全名匹配(如 .gitignore/.env.example)
    if ext in ALLOWED_MIME_EXTENSIONS or base in ALLOWED_MIME_EXTENSIONS:
        return True
    return False
