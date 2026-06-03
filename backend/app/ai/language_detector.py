"""
语言检测模块: 根据文件扩展名推断编程语言
"""
import os

LANG_MAP = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".vue": "vue", ".svelte": "svelte",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".css": "css", ".scss": "css", ".less": "css", ".sass": "css",
    ".php": "php",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".c": "c", ".h": "c",
    ".sql": "sql",
    ".go": "go",
    ".rs": "rust", ".rb": "ruby",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".dart": "dart", ".scala": "scala", ".groovy": "groovy",
    ".lua": "lua", ".r": "r", ".pl": "perl",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".xml": "xml",
    ".csv": "csv",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".md": "markdown", ".markdown": "markdown", ".rst": "markdown",
    ".txt": "plaintext", ".cfg": "plaintext", ".ini": "plaintext",
    ".conf": "plaintext", ".env": "plaintext", ".properties": "plaintext",
    ".gitignore": "plaintext", ".editorconfig": "plaintext",
    ".dockerfile": "dockerfile", ".cmake": "cmake", ".makefile": "makefile",
    ".gradle": "gradle", ".proto": "protobuf",
    ".tf": "terraform", ".sol": "solidity", ".graphql": "graphql",
    ".png": "binary", ".jpg": "binary", ".jpeg": "binary", ".gif": "binary",
    ".svg": "binary", ".ico": "binary", ".webp": "binary", ".bmp": "binary",
    ".pdf": "binary", ".doc": "binary", ".docx": "binary", ".xls": "binary",
    ".xlsx": "binary", ".ppt": "binary", ".pptx": "binary",
    ".zip": "binary", ".tar": "binary", ".gz": "binary", ".rar": "binary",
    ".7z": "binary", ".bz2": "binary",
    ".mp3": "binary", ".mp4": "binary", ".avi": "binary", ".mov": "binary",
    ".wav": "binary", ".flac": "binary",
    ".ttf": "binary", ".otf": "binary", ".woff": "binary", ".woff2": "binary",
    ".eot": "binary",
    ".so": "binary", ".dll": "binary", ".exe": "binary", ".bin": "binary",
    ".o": "binary", ".a": "binary", ".class": "binary", ".jar": "binary",
    ".war": "binary", ".pyc": "binary",
}


def detect_language(filename: str) -> str:
    """根据文件名扩展名检测编程语言

    Args:
        filename: 文件名(含扩展名)

    Returns:
        str: 语言标识字符串,未匹配则返回"plaintext"
    """
    ext = os.path.splitext(filename.lower())[1]
    return LANG_MAP.get(ext, "plaintext")
