"""图片魔数嗅探与校验(头像上传与小菱多模态共用)。

不信任客户端声明的 MIME/扩展名,一律按文件头魔数判断;svg 等脚本载体
一律拒绝。
"""
from __future__ import annotations


def sniff_image_mime(head: bytes) -> str | None:
    """按魔数识别 png/jpeg/gif/webp;未知返回 None。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_data_image_url(url: str, *, max_bytes: int) -> tuple[str, bytes] | None:
    """解析 data:image/...;base64,... 形式的外链图片。

    Returns:
        (mime, binary) 校验通过时返回;格式/魔数/大小不符返回 None。
    """
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    header, _, payload = url.partition(",")
    if not payload or "base64" not in header.lower():
        return None
    try:
        import base64

        binary = base64.b64decode(payload, validate=False)
    except Exception:  # noqa: BLE001 - 非法 base64 一律拒绝
        return None
    if not binary or len(binary) > max_bytes:
        return None
    mime = sniff_image_mime(binary[:16])
    if mime is None:
        return None
    return mime, binary
