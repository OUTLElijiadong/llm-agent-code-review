"""接口限流模块: 基于 slowapi 的 IP 维度速率限制

集中暴露一个全局 Limiter 实例,供 main.py 注册到应用、各路由按需 @limiter.limit 装饰。
默认使用进程内存储,单实例部署足够;多 worker 时为每进程独立计数。

主要用于保护登录/注册等可被暴力破解的端点。
"""
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _client_key(request: Request) -> str:
    """限流键: 优先取 X-Forwarded-For 首段(经 nginx 反代后的真实客户端),降级到直连 IP。"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_key)
