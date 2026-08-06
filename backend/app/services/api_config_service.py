"""
用户 API 配置服务

提供 API 配置的 CRUD、测试连接等功能。
"""
import time

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.api_config import UserApiConfig
from app.schemas.api_config import (
    ApiConfigOut,
    ApiConfigSaveIn,
    ApiConfigTestIn,
    ApiConfigTestOut,
)
from app.utils.api_resolver import (
    decrypt_api_key_with_metadata,
    encrypt_api_key,
    mask_api_key,
    validate_ai_base_url,
)


def _commit_config_security_change(db: Session, action: str) -> bool:
    """提交用户 API Key 轮换/失效，失败时回滚并记录安全摘要。

    Args:
        db: SQLAlchemy 数据库会话。
        action: 不含密钥内容的操作标识。

    Returns:
        bool: 提交成功为 True；回滚后为 False。
    """
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            f"[api_config] API Key 安全变更提交失败(action={action}, "
            f"error_type={type(exc).__name__})",
        )
        return False
    return True


def get_config(db: Session, user_id: int) -> ApiConfigOut:
    """获取当前用户的 API 配置

    返回前端的配置信息, API Key 已脱敏。
    """
    row = db.query(UserApiConfig).filter(UserApiConfig.user_id == user_id).first()

    if row:
        decryption = decrypt_api_key_with_metadata(row.api_key_enc)
        decrypted = ""
        effective_active = bool(row.is_active)
        if decryption is None:
            row.is_active = False
            _commit_config_security_change(db, f"deactivate_user:{user_id}")
            effective_active = False
            logger.warning(f"[api_config] 用户 {user_id} 的 API Key 无法解密，配置已停用")
        else:
            decrypted = decryption.plaintext
            if decryption.needs_rotation:
                row.api_key_enc = encrypt_api_key(decrypted)
                if not _commit_config_security_change(db, f"rotate_user:{user_id}"):
                    decrypted = ""
                    effective_active = False

        return ApiConfigOut(
            provider=row.provider,
            api_key_masked=mask_api_key(decrypted) if decrypted else "****",
            base_url=row.base_url,
            model=row.model,
            is_active=effective_active,
            is_custom=True,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # 用户未自定义, 返回系统默认信息
    return ApiConfigOut(
        provider="deepseek",
        api_key_masked=mask_api_key(settings.deepseek_api_key),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        is_active=True,
        is_custom=False,
    )


def save_config(db: Session, user_id: int, payload: ApiConfigSaveIn) -> ApiConfigOut:
    """保存或更新用户的 API 配置"""
    safe_base_url = validate_ai_base_url(
        payload.base_url,
        resolve_host=True,
        allow_private=False,
    )
    encrypted = encrypt_api_key(payload.api_key)

    row = db.query(UserApiConfig).filter(UserApiConfig.user_id == user_id).first()

    if row:
        row.provider = payload.provider
        row.api_key_enc = encrypted
        row.base_url = safe_base_url
        row.model = payload.model
        row.is_active = True
    else:
        row = UserApiConfig(
            user_id=user_id,
            provider=payload.provider,
            api_key_enc=encrypted,
            base_url=safe_base_url,
            model=payload.model,
            is_active=True,
        )
        db.add(row)

    db.commit()
    db.refresh(row)

    return ApiConfigOut(
        provider=row.provider,
        api_key_masked=mask_api_key(payload.api_key),
        base_url=row.base_url,
        model=row.model,
        is_active=row.is_active,
        is_custom=True,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def delete_config(db: Session, user_id: int) -> None:
    """删除用户的 API 配置，恢复使用系统默认"""
    row = db.query(UserApiConfig).filter(UserApiConfig.user_id == user_id).first()
    if row:
        db.delete(row)
        db.commit()
        logger.info(f"[api_config] 用户 {user_id} 已删除自定义 API 配置, 恢复系统默认")


def test_connection(payload: ApiConfigTestIn) -> ApiConfigTestOut:
    """测试 API 连接是否可用

    发送一个最小化的请求验证连通性和认证。
    不存储任何数据。
    """
    try:
        base_url = validate_ai_base_url(
            payload.base_url,
            resolve_host=True,
            allow_private=False,
        )
    except ValidationError as exc:
        return ApiConfigTestOut(
            success=False,
            message=exc.message,
            duration_ms=0,
        )
    test_messages = [
        {"role": "user", "content": "ping"}
    ]

    t0 = time.time()
    try:
        from app.utils.public_http import pin_public_http_url

        target = pin_public_http_url(f"{base_url}/chat/completions")
        with httpx.Client(timeout=15, trust_env=False) as client:
            resp = client.post(
                target.request_url,
                headers={
                    "Authorization": f"Bearer {payload.api_key}",
                    "Content-Type": "application/json",
                    "Host": target.host_header,
                },
                json={
                    "model": payload.model,
                    "messages": test_messages,
                    "max_tokens": 5,
                    "temperature": 0,
                },
                extensions=target.request_extensions,
            )
        duration_ms = int((time.time() - t0) * 1000)

        if resp.status_code == 200:
            body = resp.json()
            actual_model = body.get("model", payload.model)
            return ApiConfigTestOut(
                success=True,
                message=f"连接成功 (模型: {actual_model})",
                model=actual_model,
                duration_ms=duration_ms,
            )
        elif resp.status_code == 401:
            return ApiConfigTestOut(
                success=False,
                message="认证失败: API Key 无效或已过期",
                duration_ms=duration_ms,
            )
        elif resp.status_code == 404:
            return ApiConfigTestOut(
                success=False,
                message=f"端点不存在: {base_url}/chat/completions 返回 404",
                duration_ms=duration_ms,
            )
        else:
            return ApiConfigTestOut(
                success=False,
                message=f"请求失败 ({resp.status_code}): {resp.text[:200]}",
                duration_ms=duration_ms,
            )
    except httpx.ConnectError:
        duration_ms = int((time.time() - t0) * 1000)
        return ApiConfigTestOut(
            success=False,
            message=f"无法连接到 {base_url}，请检查地址是否正确",
            duration_ms=duration_ms,
        )
    except httpx.TimeoutException:
        duration_ms = int((time.time() - t0) * 1000)
        return ApiConfigTestOut(
            success=False,
            message="连接超时，请检查网络或 API 端点可用性",
            duration_ms=duration_ms,
        )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        return ApiConfigTestOut(
            success=False,
            message=f"连接异常: {str(e)[:200]}",
            duration_ms=duration_ms,
        )
