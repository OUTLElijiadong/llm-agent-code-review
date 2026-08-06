"""
API 配置解析器 —— 统一 API 配置入口。

职责:
1. 使用独立密钥环加密 API Key，并兼容历史 JWT_SECRET 派生密文。
2. 按优先级解析有效 API 配置: 用户自定义 > 系统全局 > 系统默认。
3. 对旧密钥密文执行机会式重加密，对不可恢复记录执行安全失效。

安全:
- API Key 使用 Fernet 加密存储。
- 新写入只使用 API_KEY_ENCRYPTION_KEYS 第一项。
- 旧独立密钥与历史 JWT 派生密钥只用于兼容解密。
- 返回给前端的 key 始终脱敏: sk-a1****z0。
"""
import base64
import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError


@dataclass(frozen=True)
class ApiKeyDecryption:
    """描述一次 API Key 解密的来源与是否需要轮换。

    Attributes:
        plaintext: 解密得到的明文，只允许后端内部短暂使用。
        source: ``configured`` 或 ``legacy_jwt``。
        key_index: 配置密钥环下标；历史 JWT 回退为 -1。
        needs_rotation: 是否应立即用当前主密钥重加密。
    """

    plaintext: str
    source: str
    key_index: int
    needs_rotation: bool


@dataclass(frozen=True)
class _FernetKeyEntry:
    """内部 Fernet 密钥环条目。"""

    source: str
    key_index: int
    fernet: Fernet


# ── 密钥派生与密钥环 ─────────────────────────────────────

def _derive_fernet_key(secret: str) -> bytes:
    """从任意独立 secret 派生 Fernet 密钥。

    Args:
        secret: 用于派生的非空秘密字符串。

    Returns:
        bytes: 32 字节摘要的 URL-safe Base64 编码。
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _configured_encryption_secrets() -> list[str]:
    """读取、去空白并按首次出现顺序去重独立密钥。

    Returns:
        list[str]: 当前动态配置的独立密钥列表。
    """
    secrets: list[str] = []
    seen: set[str] = set()
    for raw in settings.api_key_encryption_keys:
        secret = raw.strip()
        if secret and secret not in seen:
            secrets.append(secret)
            seen.add(secret)
    return secrets


def _build_fernet_keyring() -> list[_FernetKeyEntry]:
    """动态构建解密密钥环并追加历史 JWT 兼容项。

    Returns:
        list[_FernetKeyEntry]: 配置密钥在前、历史 JWT 回退在后的密钥环。
    """
    entries: list[_FernetKeyEntry] = []
    seen_keys: set[bytes] = set()
    for index, secret in enumerate(_configured_encryption_secrets()):
        derived = _derive_fernet_key(secret)
        if derived in seen_keys:
            continue
        entries.append(_FernetKeyEntry("configured", index, Fernet(derived)))
        seen_keys.add(derived)

    legacy_key = _derive_fernet_key(settings.jwt_secret)
    if legacy_key not in seen_keys:
        entries.append(_FernetKeyEntry("legacy_jwt", -1, Fernet(legacy_key)))
    return entries


def _primary_fernet() -> Fernet:
    """获取只用于新写入的当前主 Fernet 实例。

    Returns:
        Fernet: 独立密钥环第一项；开发未配置时回退历史 JWT 派生项。

    Raises:
        RuntimeError: 未能构建任何可用密钥时抛出。
    """
    entries = _build_fernet_keyring()
    if not entries:
        raise RuntimeError("API Key 加密密钥环为空")
    return entries[0].fernet


# ── 加密 / 解密 ─────────────────────────────────────────

def encrypt_api_key(plain: str) -> str:
    """使用当前主密钥加密 API Key。

    Args:
        plain: API Key 明文。

    Returns:
        str: Fernet 密文。
    """
    return _primary_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_api_key_with_metadata(encrypted: str) -> Optional[ApiKeyDecryption]:
    """依次尝试密钥环并返回不含日志副作用的解密元数据。

    Args:
        encrypted: Fernet 密文。

    Returns:
        Optional[ApiKeyDecryption]: 解密成功信息；全部失败时返回 None。
    """
    configured_count = len(_configured_encryption_secrets())
    try:
        token = encrypted.encode("utf-8")
    except (AttributeError, TypeError):
        return None

    for entry in _build_fernet_keyring():
        try:
            plaintext = entry.fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError, TypeError):
            continue
        needs_rotation = (
            (entry.source == "configured" and entry.key_index != 0)
            or (entry.source == "legacy_jwt" and configured_count > 0)
        )
        return ApiKeyDecryption(
            plaintext=plaintext,
            source=entry.source,
            key_index=entry.key_index,
            needs_rotation=needs_rotation,
        )
    return None


def decrypt_api_key(encrypted: str) -> str:
    """兼容旧调用方地解密 API Key，失败时返回空字符串。

    Args:
        encrypted: Fernet 密文。

    Returns:
        str: API Key 明文；失败时为空字符串。
    """
    result = decrypt_api_key_with_metadata(encrypted)
    if result is None:
        logger.warning("[api_resolver] 解密 API Key 失败；记录需要失效或人工恢复")
        return ""
    return result.plaintext


def mask_api_key(key: str) -> str:
    """生成不泄露完整凭据的 API Key 脱敏文本。

    Args:
        key: API Key 明文。

    Returns:
        str: 首尾局部可见的脱敏文本。
    """
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


def _commit_api_key_security_change(db: Session, action: str) -> bool:
    """提交 API Key 轮换或失效变更，失败时回滚且不泄露敏感值。

    Args:
        db: SQLAlchemy 数据库会话。
        action: 仅包含操作类型和记录标识的安全日志描述。

    Returns:
        bool: 提交成功为 True；回滚后为 False。
    """
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            f"[api_resolver] API Key 安全变更提交失败(action={action}, "
            f"error_type={type(exc).__name__})",
        )
        return False
    return True


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
            decryption = decrypt_api_key_with_metadata(row.api_key_enc)
            if decryption is None:
                row.is_active = False
                _commit_api_key_security_change(db, f"deactivate_user:{user_id}")
                logger.warning(
                    f"[api_resolver] 用户 {user_id} 的 API Key 无法解密，配置已失效并回退",
                )
            else:
                key = decryption.plaintext
                if decryption.needs_rotation:
                    row.api_key_enc = encrypt_api_key(key)
                    if not _commit_api_key_security_change(db, f"rotate_user:{user_id}"):
                        logger.warning(
                            f"[api_resolver] 用户 {user_id} 的 API Key 轮换未持久化，安全回退",
                        )
                        key = ""
                if key:
                    try:
                        base_url = validate_ai_base_url(
                            row.base_url,
                            resolve_host=settings.enforce_ai_base_url_dns_check,
                        )
                    except ValidationError as exc:
                        logger.warning(
                            f"[api_resolver] 用户 {user_id} 的 API 端点不安全,"
                            f"回退系统默认: {exc.message}",
                        )
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

    # 管理员全局覆盖: 平台级在 DeepSeek 与自定义 OpenAI 兼容端点间切换。
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
