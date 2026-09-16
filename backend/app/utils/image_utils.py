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
    if not payload or not header.lower().endswith(";base64") or len(payload) > ((max_bytes + 2) // 3) * 4:
        return None
    try:
        import base64

        binary = base64.b64decode(payload, validate=True)
    except Exception:  # noqa: BLE001 - 非法 base64 一律拒绝
        return None
    if not binary or len(binary) > max_bytes:
        return None
    try:
        mime = validate_image_bytes(binary, max_dimension=16384, max_pixels=33_554_432)
    except ValueError:
        return None
    return mime, binary


def validate_image_bytes(raw: bytes, *, max_dimension: int = 4096, max_pixels: int = 16_777_216) -> str:
    """验证完整图片可解码且像素有界；损坏/超限抛 ValueError。"""
    import io
    import warnings

    from PIL import Image, UnidentifiedImageError

    mime = sniff_image_mime(raw[:16])
    if not mime:
        raise ValueError("仅支持 PNG / JPEG / WebP / GIF 图片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as picture:
                width, height = picture.size
                if min(width, height) < 1 or max(width, height) > max_dimension or width * height > max_pixels:
                    raise ValueError(f"图片尺寸不能超过 {max_dimension} 像素，或总像素超限")
                picture.verify()
            with Image.open(io.BytesIO(raw)) as picture:
                # 校验所有帧并限制解码总量，避免小文件压缩出无界动画。
                frames = getattr(picture, "n_frames", 1)
                if frames * width * height > max_pixels or frames > 100:
                    raise ValueError("动画图片帧数或总像素超限，请上传静态图片")
                for frame in range(frames):
                    picture.seek(frame)
                    picture.load()
    except (
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("图片损坏或无法解码，请重新选择图片") from exc
    return mime
