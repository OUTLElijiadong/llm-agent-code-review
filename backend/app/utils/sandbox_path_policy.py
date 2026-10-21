"""沙箱导入路径的服务器端白名单（默认拒绝）。

沙箱执行器会再次校验，但后端不能把未经校验的路径交给 worker：
源码 ZIP 成员、Agent 生成的测试文件都必须先通过这里。
只放行规范化后的相对路径；穿越、编码穿越、绝对路径、盘符、URI 包装器、
空字节/控制字符、符号链接与特殊文件一律拒绝。
"""

from __future__ import annotations

import io
import re
import stat
import unicodedata
import urllib.parse
import zipfile

from app.core.exceptions import ValidationError

# 允许的路径段字符：Unicode 字母数字（含中文）与常见文件名符号；其余默认拒绝。
_SEGMENT_RE = re.compile(r"^[\w.\-+@ ()\[\]{}~,=#$!'&;^%]+$", re.UNICODE)
_URI_WRAPPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_MAX_PATH_BYTES = 2_048
_MAX_SEGMENT_BYTES = 255
_MAX_DECODE_ROUNDS = 3


def _reject(name: str, reason: str) -> None:
    raise ValidationError(f"沙箱导入路径不在白名单内（{reason}）：{name[:120]!r}", code=40001)


def validate_sandbox_relative_path(name: str, *, allow_directory: bool = False) -> str:
    """返回规范化的相对路径；不满足白名单时抛 ValidationError。"""
    if not isinstance(name, str) or not name:
        _reject(str(name), "空路径")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        _reject(name, "包含空字节或控制字符")
    if "\\" in name:
        _reject(name, "包含反斜杠")
    # 逐轮解码，任何一轮出现分隔符、点段或控制字符都视为编码穿越。
    decoded = name
    for _ in range(_MAX_DECODE_ROUNDS):
        if "%" not in decoded:
            break
        nxt = urllib.parse.unquote(decoded)
        if nxt == decoded:
            break
        if any(ord(char) < 32 for char in nxt) or "\\" in nxt or ".." in nxt or nxt.count("/") != name.count("/"):
            _reject(name, "包含编码后的路径穿越或控制字符")
        decoded = nxt
    normalized = unicodedata.normalize("NFC", name)
    if allow_directory and normalized.endswith("/"):
        normalized = normalized[:-1]
    if normalized.startswith("/"):
        _reject(name, "绝对路径")
    if _URI_WRAPPER_RE.match(normalized):
        _reject(name, "盘符或 URI 包装器")
    if len(normalized.encode("utf-8")) > _MAX_PATH_BYTES:
        _reject(name, "路径过长")
    parts = normalized.split("/")
    for part in parts:
        if part in {"", ".", ".."}:
            _reject(name, "空段或点段")
        if len(part.encode("utf-8")) > _MAX_SEGMENT_BYTES:
            _reject(name, "路径段过长")
        if ":" in part:
            _reject(name, "设备路径或 URI 包装器")
        if not _SEGMENT_RE.match(part):
            _reject(name, "包含不允许的字符")
    return normalized


def validate_sandbox_zip(archive: bytes) -> None:
    """逐个成员校验源码 ZIP；拒绝符号链接、特殊文件与加密成员。"""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                validate_sandbox_relative_path(info.filename, allow_directory=info.is_dir())
                if info.flag_bits & 0x1:
                    _reject(info.filename, "加密成员")
                # 只带权限位、无文件类型位的成员按普通文件处理；显式的链接/设备/管道拒绝。
                file_type = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
                if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
                    _reject(info.filename, "符号链接或特殊文件")
    except zipfile.BadZipFile as exc:
        raise ValidationError("沙箱源码归档不是合法 ZIP", code=40001) from exc
