"""
编码探测与转换工具模块
"""
import base64

BINARY_THRESHOLD = 0.3
BASE64_PREFIX = "[BINARY:BASE64:]"


def to_utf8(raw: bytes) -> str:
    """将原始字节流转为UTF-8字符串,自动探测编码

    对文本文件自动探测编码并转UTF-8;
    对二进制文件(图片、编译产物等)使用base64编码存储。

    Args:
        raw: 原始字节数据

    Returns:
        str: UTF-8编码的字符串(文本文件) 或 base64编码字符串(二进制文件)

    Raises:
        ValidationError: 无法识别编码
    """
    if not raw:
        return ""
    if _is_binary(raw):
        return _encode_base64(raw)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        import chardet
        guess = chardet.detect(raw)
        enc = guess.get("encoding") or "gbk"
        confidence = guess.get("confidence") or 0
        if confidence < BINARY_THRESHOLD:
            return _encode_base64(raw)
        try:
            return raw.decode(enc, errors="replace")
        except Exception:
            return _encode_base64(raw)


def _is_binary(raw: bytes) -> bool:
    """通过null字节比例判断是否为二进制文件

    Args:
        raw: 原始字节数据

    Returns:
        bool: True表示二进制文件
    """
    if not raw:
        return False
    null_count = raw.count(b'\x00')
    return null_count > 0


def _encode_base64(raw: bytes) -> str:
    """将二进制内容编码为base64文本

    Args:
        raw: 原始字节数据

    Returns:
        str: base64编码字符串,带前缀标记
    """
    return BASE64_PREFIX + base64.b64encode(raw).decode("ascii")
