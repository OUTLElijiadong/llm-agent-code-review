"""
API 配置解析器 —— 统一 API 配置入口

职责:
1. Fernet 加密/解密 API Key（密钥从 JWT_SECRET 派生）
2. 按优先级解析有效 API 配置: 用户自定义 > 系统默认
3. 提供统一的数据类 ApiConfig 供 Agent 层消费

安全:
- API Key 使用 Fernet (AES-128-CBC + HMAC) 加密存储
- 加密密钥 = SHA256(JWT_SECRET) 的前 32 字节
- 返回给前端的 key 始终脱敏: sk-a1****z0
"""
import base64
import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError

# ── 密钥派生 ────────────────────────────────────────────

def _derive_fernet_key() -> bytes:
    """从 JWT_SECRET 派生 Fernet 密钥

    Fernet 要求 32 字节 URL-safe base64 编码的密钥。
    将 JWT_SECRET SHA256 哈希后取 32 字节，再用 base64 编码。
    """
    secret = settings.jwt_secret.encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    return base64.urlsafe_b64encode(digest[:32])


_fernet = Fernet(_derive_fernet_key())


# ── 加密 / 解密 ─────────────────────────────────────────

def encrypt_api_key(plain: str) -> str:
    """加密 API Key"""
    return _fernet.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_api_key(encrypted: str) -> str:
    """解密 API Key"""
    try:
        return _fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.warning("[api_resolver] 解密 API Key 失败，可能密钥已变更")
        return ""


def mask_api_key(key: str) -> str:
    """脱敏 API Key: sk-a1b2c3...x9z0 → sk-a1****z0"""
    if not key or len(key) < 12:
        return "****"
    return key[:5] + "****" + key[-4:]


# ── API Base URL 安全校验 ───────────────────────────────

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".corp")


def _is_blocked_ip(ip_text: str) -> bool:
    """判断地址是否属于不应被普通用户配置访问的非公网范围。

    Args:
        ip_text: IPv4 或 IPv6 字符串。

    Returns:
        bool: True 表示默认应阻止访问。
    """
    ip = ipaddress.ip_address(ip_text)
    return not ip.is_global


def _resolve_host(host: str) -> set[str]:
    """解析主机名为 IP 集合。

    Args:
        host: URL 中的 hostname。

    Returns:
        set[str]: 解析到的 IP 地址集合。
    """
    rows = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return {row[4][0] for row in rows}


def validate_ai_base_url(
    base_url: str,
    *,
    resolve_host: bool = False,
    allow_private: Optional[bool] = None,
) -> str:
    """校验 OpenAI-compatible API base_url,阻止 SSRF 高风险地址。

    Args:
        base_url: 用户或系统配置的 API 端点。
        resolve_host: 是否解析 DNS 并校验解析结果。
        allow_private: 是否允许私有/本机地址;None 时读取配置。

    Returns:
        str: 去除尾部斜杠后的规范化 URL。

    Raises:
        ValidationError: URL 非法或指向非公网地址。
    """
    allow_private = settings.allow_private_ai_base_url if allow_private is None else allow_private
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ValidationError("API 端点不能为空", code=40001)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("API 端点必须是 http(s) URL", code=40001)
    if parsed.username or parsed.password:
        raise ValidationError("API 端点不能包含用户名或密码", code=40001)
    if parsed.query or parsed.fragment:
        raise ValidationError("API 端点不能包含 query 或 fragment", code=40001)

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValidationError("API 端点缺少主机名", code=40001)

    if allow_private:
        return url

    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES) or "." not in host:
        raise ValidationError("API 端点不能指向本机或内网主机名", code=40001)

    try:
        if _is_blocked_ip(host):
            raise ValidationError("API 端点不能指向内网或保留地址", code=40001)
    except ValueError:
        pass

    if resolve_host:
        try:
            addresses = _resolve_host(host)
        except socket.gaierror as exc:
            raise ValidationError("API 端点域名无法解析", code=40001) from exc
        for addr in addresses:
            try:
                if _is_blocked_ip(addr):
                    raise ValidationError("API 端点解析到内网或保留地址", code=40001)
            except ValueError:
                continue

    return url


# ── 解析结果 ────────────────────────────────────────────

@dataclass
class ApiConfig:
    """解析后的 API 配置"""
    api_key: str
    base_url: str
    model: str
    provider: str = "deepseek"
    source: str = "system"  # "system" | "user"


# ── 核心解析 ────────────────────────────────────────────

def resolve_api_config(
    db: Session,
    user_id: Optional[int] = None,
) -> ApiConfig:
    """解析有效的 API 配置

    优先级: 用户自定义 (active) > 系统默认

    Args:
        db: 数据库会话
        user_id: 可选的用户ID，为 None 时直接返回系统默认

    Returns:
        ApiConfig: 最终使用的 API 配置
    """
    # 尝试读取用户配置
    if user_id is not None:
        from app.models.api_config import UserApiConfig

        row = (
            db.query(UserApiConfig)
            .filter(UserApiConfig.user_id == user_id, UserApiConfig.is_active.is_(True))
            .first()
        )
        if row:
            key = decrypt_api_key(row.api_key_enc)
            if key:
                try:
                    base_url = validate_ai_base_url(
                        row.base_url,
                        resolve_host=settings.enforce_ai_base_url_dns_check,
                    )
                except ValidationError as exc:
                    logger.warning(f"[api_resolver] 用户 {user_id} 的 API 端点不安全,回退系统默认: {exc.message}")
                    base_url = ""
                if not base_url:
                    return ApiConfig(
                        api_key=settings.deepseek_api_key,
                        base_url=validate_ai_base_url(settings.deepseek_base_url),
                        model=settings.deepseek_model,
                        provider="deepseek",
                        source="system",
                    )
                return ApiConfig(
                    api_key=key,
                    base_url=base_url,
                    model=row.model,
                    provider=row.provider,
                    source="user",
                )
            logger.warning(f"[api_resolver] 用户 {user_id} 的 API Key 解密失败，回退系统默认")

    # 管理员全局覆盖: 平台级在 DeepSeek 与自定义 OpenAI 兼容端点(如 gpt-5.5)间切换。
    # 优先级低于用户自定义, 高于系统内置默认; 任何异常都降级为内置默认。
    try:
        from app.services import system_config_service

        gcfg = system_config_service.get_llm_config(db)
        if (
            gcfg
            and gcfg.get("active")
            and gcfg.get("api_key")
            and gcfg.get("base_url")
            and gcfg.get("model")
        ):
            return ApiConfig(
                api_key=gcfg["api_key"],
                base_url=gcfg["base_url"],
                model=gcfg["model"],
                provider=gcfg.get("provider", "custom"),
                source="global",
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[api_resolver] 全局 LLM 配置解析失败，回退系统默认: {e}")

    # 回退系统默认
    return ApiConfig(
        api_key=settings.deepseek_api_key,
        base_url=validate_ai_base_url(settings.deepseek_base_url),
        model=settings.deepseek_model,
        provider="deepseek",
        source="system",
    )
