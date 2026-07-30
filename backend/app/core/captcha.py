"""轻量注册验证码模块:防批量注册刷号。

采用「数学问答 + 一次性令牌」:
- GET /auth/captcha 返回 captcha_id + 数学题(如 "3 + 5 = ?"),不返回答案;
- 注册时携带 captcha_id + captcha_answer,服务端校验一次性、带过期;
- 答错或过期即拒绝,答案用完即焚,防重放。

进程内存储,单实例部署足够(与 rate_limit 一致)。生产配 limiter 双层防护。
"""
from __future__ import annotations

import random
import time
import uuid
from threading import Lock

# captcha_id -> (answer, expire_ts)
_STORE: dict[str, tuple[str, float]] = {}
_LOCK = Lock()
_TTL_SECONDS = 300  # 验证码有效期 5 分钟
_MAX_ENTRIES = 10000


def _purge_expired(now: float) -> None:
    expired = [k for k, (_, exp) in _STORE.items() if exp < now]
    for k in expired:
        _STORE.pop(k, None)


def create_captcha() -> dict:
    """生成一道数学验证码。

    Returns:
        dict: {"captcha_id": str, "question": str}(不含答案)
    """
    now = time.time()
    with _LOCK:
        _purge_expired(now)
        # 容量兜底,防内存膨胀
        if len(_STORE) >= _MAX_ENTRIES:
            _STORE.clear()
        a, b = random.randint(1, 20), random.randint(1, 20)
        op = random.choice(["+", "-"])
        if op == "-" and a < b:
            a, b = b, a  # 保证非负答案
        answer = str(a + b if op == "+" else a - b)
        captcha_id = uuid.uuid4().hex
        _STORE[captcha_id] = (answer, now + _TTL_SECONDS)
        question = f"{a} {op} {b} = ?"
    return {"captcha_id": captcha_id, "question": question}


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
    if exp < now:
        return False
    return expected == (answer or "").strip()
