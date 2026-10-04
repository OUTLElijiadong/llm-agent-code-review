"""跨 REST、小菱工具和团队入口共享的正式审查准入额度。"""

from __future__ import annotations

import math
import time

from limits import parse
from loguru import logger

from app.core import rate_limit
from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError, TooManyRequestsError

_REVIEW_START_LIMIT = parse("5/minute")
_REVIEW_START_SCOPE = "review_start_shared"


def admit_review_start(user_id: int) -> None:
    """按稳定账号 ID 原子消耗一次审查准入名额。

    使用 SlowAPI 已配置的同一 limits storage，因此 Redis 部署下各 Web worker
    和服务入口共享计数。存储故障时关闭准入，避免退化成进程内独立额度。
    """
    try:
        actor_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ServiceUnavailableError("无法确认审查账号，暂未创建审查任务") from exc
    if actor_id <= 0:
        raise ServiceUnavailableError("无法确认审查账号，暂未创建审查任务")
    if settings.app_env.lower() not in {"dev", "test"} and not settings.redis_url.strip():
        raise ServiceUnavailableError("审查准入未配置共享限流存储，任务未创建")

    identifiers = (
        rate_limit.RATE_LIMIT_KEY_PREFIX,
        f"user:{actor_id}",
        _REVIEW_START_SCOPE,
    )
    backend = rate_limit.limiter.limiter
    try:
        allowed = backend.hit(_REVIEW_START_LIMIT, *identifiers)
    except Exception as exc:  # noqa: BLE001 - 限流存储失效必须 fail closed
        logger.exception("审查准入计数存储不可用，拒绝创建任务")
        raise ServiceUnavailableError("审查准入校验暂不可用，任务未创建，请稍后重试") from exc
    if allowed:
        return

    try:
        reset_at = float(backend.get_window_stats(_REVIEW_START_LIMIT, *identifiers).reset_time)
        retry_after = max(1, math.ceil(reset_at - time.time()))
    except Exception:  # noqa: BLE001 - 名额已被拒绝，重置时间查询失败不放行
        retry_after = 60
    raise TooManyRequestsError(
        "请求过于频繁,请稍后再试",
        detail="审查发起过于频繁，请稍后重试",
        retry_after=retry_after,
    )
