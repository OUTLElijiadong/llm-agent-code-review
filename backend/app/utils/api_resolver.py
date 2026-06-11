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
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings

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
                return ApiConfig(
                    api_key=key,
                    base_url=row.base_url,
                    model=row.model,
                    provider=row.provider,
                    source="user",
                )
            logger.warning(f"[api_resolver] 用户 {user_id} 的 API Key 解密失败，回退系统默认")

    # 回退系统默认
    return ApiConfig(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        provider="deepseek",
        source="system",
    )
