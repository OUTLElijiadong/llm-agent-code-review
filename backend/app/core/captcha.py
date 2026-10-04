"""注册位图挑战与一次性令牌。

- GET /auth/captcha 返回 captcha_id + PNG，不公开可直接计算的算式或字符答案;
- 注册时携带 captcha_id + captcha_answer,服务端校验一次性、带过期;
- 答错或过期即拒绝,答案用完即焚,防重放。

位图不保证抵抗 OCR；注册仍依赖共享接口限流与可选内测码。挑战存储为单进程，
多进程部署须迁移到共享存储，不应把该挑战描述为强人机验证。
"""
from __future__ import annotations

import base64
import io
import secrets
import time
import uuid
from threading import Lock

from PIL import Image, ImageDraw, ImageFont

from app.core.exceptions import TooManyRequestsError

# captcha_id -> (answer, expire_ts)
_STORE: dict[str, tuple[str, float]] = {}
_LOCK = Lock()
_TTL_SECONDS = 300  # 验证码有效期 5 分钟
_MAX_ENTRIES = 10000
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _render_image(answer: str) -> str:
    """使用已有 Pillow 位图绘制，无文字元数据、SVG 或外部字体依赖。"""
    image = Image.new("RGB", (168, 56), "#f5f5fb")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    for index, character in enumerate(answer):
        draw.text((8 + index * 25, 8 + secrets.randbelow(8)), character, fill="#36316f", font=font)
    for _ in range(3):
        draw.line((secrets.randbelow(168), secrets.randbelow(56),
                   secrets.randbelow(168), secrets.randbelow(56)), fill="#b7b4d4", width=1)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _purge_expired(now: float) -> None:
    expired = [k for k, (_, exp) in _STORE.items() if exp <= now]
    for k in expired:
        _STORE.pop(k, None)


def create_captcha() -> dict:
    """生成 6 位字符挑战，先完成渲染再占用存储。

    Returns:
        dict: 标识、固定提示与 PNG 位图，不含算式或明文答案
    """
    now = time.time()
    answer = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    image_data = _render_image(answer)
    with _LOCK:
        _purge_expired(now)
        # 容量兜底,防内存膨胀
        if len(_STORE) >= _MAX_ENTRIES:
            raise TooManyRequestsError("验证码请求过多，请稍后刷新", retry_after=_TTL_SECONDS)
        captcha_id = uuid.uuid4().hex
        _STORE[captcha_id] = (answer, now + _TTL_SECONDS)
    return {"captcha_id": captcha_id, "question": "请输入图片中的 6 位字符（不区分大小写）", "image_data": image_data}


def verify_captcha(captcha_id: str, answer: str) -> bool:
    """校验验证码,一次性有效(无论对错都消费掉,防暴力枚举)。

    Args:
        captcha_id: create_captcha 返回的标识
        answer: 用户填写的答案

    Returns:
        bool: 校验通过返回 True
    """
    now = time.time()
    with _LOCK:
        entry = _STORE.pop(captcha_id, None)  # 一次性:取出即删
    if not entry:
        return False
    expected, exp = entry
    if exp <= now:
        return False
    normalized = (answer or "").strip()
    return normalized.isascii() and secrets.compare_digest(expected, normalized.upper())
