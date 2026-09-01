"""
用户 API 配置服务

提供 API 配置的 CRUD、测试连接等功能。
"""
import re
import time
from dataclasses import dataclass
from typing import Optional

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
from app.schemas.llm_config import LlmModelsIn, LlmModelsOut
from app.utils.api_resolver import (
    decrypt_api_key_with_metadata,
    encrypt_api_key,
    mask_api_key,
    normalize_ai_base_url,
)
from app.utils.public_http import pin_public_http_url

_TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_UNSUPPORTED_MODELS_STATUS_CODES = {404, 405, 501}


@dataclass(frozen=True)
class _RequestOutcome:
    response: Optional[httpx.Response]
    error: Optional[Exception]
    duration_ms: int
    attempts: int


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
    safe_base_url = normalize_ai_base_url(
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

    try:
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        raise

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
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        logger.info(f"[api_config] 用户 {user_id} 已删除自定义 API 配置, 恢复系统默认")


def _request_with_retries(
    method: str,
    url: str,
    *,
    api_key: str,
    timeout_seconds: int,
    max_retries: int,
    json_body: Optional[dict] = None,
) -> _RequestOutcome:
    """向固定公网目标发请求，仅对瞬时故障执行有限重试。"""
    target = pin_public_http_url(url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Host": target.host_header,
    }
    started = time.monotonic()
    attempts = 0
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        remaining_seconds = timeout_seconds - (time.monotonic() - started)
        if remaining_seconds <= 0:
            return _RequestOutcome(
                response=None,
                error=httpx.TimeoutException("交互请求超过总超时时间"),
                duration_ms=int((time.monotonic() - started) * 1000),
                attempts=max(0, attempts - 1),
            )
        try:
            with httpx.Client(timeout=max(0.1, remaining_seconds), trust_env=False) as client:
                request = client.get if method == "GET" else client.post
                kwargs = {
                    "headers": headers,
                    "extensions": target.request_extensions,
                }
                if json_body is not None:
                    kwargs["json"] = json_body
                response = request(target.request_url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= max_retries:
                return _RequestOutcome(
                    response=None,
                    error=exc,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts,
                )
        else:
            if response.status_code not in _TRANSIENT_STATUS_CODES or attempt >= max_retries:
                return _RequestOutcome(
                    response=response,
                    error=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts,
                )
        remaining_seconds = timeout_seconds - (time.monotonic() - started)
        if remaining_seconds > 0:
            time.sleep(min(4, 2 ** attempt, remaining_seconds))
    raise RuntimeError("LLM 请求重试循环未返回结果")


def _transport_failure(outcome: _RequestOutcome) -> tuple[str, bool, str]:
    if isinstance(outcome.error, httpx.TimeoutException):
        return (
            f"连接超时，已尝试 {outcome.attempts} 次",
            True,
            "可稍后重试，或适当提高超时时间并检查上游状态",
        )
    return (
        f"无法连接上游，已尝试 {outcome.attempts} 次",
        True,
        "请检查 Base URL、网络和上游服务状态后重试",
    )


def _http_failure(status_code: int, attempts: int) -> tuple[str, bool, str]:
    if status_code == 401:
        return "认证失败：API Key 无效或已过期", False, "请更新 API Key 后重试"
    if status_code == 403:
        return "上游拒绝访问：当前 Key 无权使用该接口或模型", False, "请检查 Key 权限、配额和模型授权"
    if status_code == 404:
        return "接口或模型不存在（404）", False, "请检查 Base URL 和模型名称"
    if status_code in {400, 422}:
        return f"请求参数不兼容（{status_code}）", False, "请检查模型名称、接口协议和参数支持情况"
    if status_code == 429:
        return f"上游限流，已尝试 {attempts} 次", True, "请稍后重试或降低并发"
    if status_code >= 500:
        return f"上游服务暂时异常（{status_code}），已尝试 {attempts} 次", True, "请稍后重试"
    return f"上游请求失败（{status_code}）", False, "请检查提供商配置后重试"


def _extract_models(body) -> list[str]:
    items = body.get("data") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return []
    models: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
        if not isinstance(value, str):
            continue
        model = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:128]
        if model and model not in seen:
            seen.add(model)
            models.append(model)
        if len(models) >= 500:
            break
    return models


def fetch_models(payload: LlmModelsIn) -> LlmModelsOut:
    """拉取 OpenAI-compatible 模型列表，不支持列表时保留手工模型。"""
    api_key = (payload.api_key or "").strip()
    manual_model = (payload.model or "").strip()
    if not api_key:
        return LlmModelsOut(
            success=False,
            message="请先填写 API Key，或保存后使用已配置的 Key",
            models=[manual_model] if manual_model else [],
            selected_model=manual_model,
            next_action="填写 API Key 后重试；也可以继续手工输入模型名称",
        )
    try:
        base_url = normalize_ai_base_url(
            payload.base_url or "",
            resolve_host=True,
            allow_private=False,
        )
    except ValidationError as exc:
        return LlmModelsOut(
            success=False,
            message=exc.message,
            models=[manual_model] if manual_model else [],
            selected_model=manual_model,
            next_action="请修正 Base URL 后重试",
        )
    try:
        outcome = _request_with_retries(
            "GET",
            f"{base_url}/models",
            api_key=api_key,
            timeout_seconds=payload.timeout_seconds or settings.deepseek_timeout,
            max_retries=payload.max_retries if payload.max_retries is not None else settings.deepseek_max_retries,
        )
    except ValidationError as exc:
        return LlmModelsOut(
            success=False,
            message=exc.message,
            models=[manual_model] if manual_model else [],
            selected_model=manual_model,
            next_action="请修正 Base URL 后重试",
        )
    if outcome.error is not None:
        message, retryable, next_action = _transport_failure(outcome)
        return LlmModelsOut(
            success=False,
            message=message,
            models=[manual_model] if manual_model else [],
            selected_model=manual_model,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            fallback=bool(manual_model),
            retryable=retryable,
            next_action=next_action,
        )

    response = outcome.response
    if response.status_code in _UNSUPPORTED_MODELS_STATUS_CODES and manual_model:
        return LlmModelsOut(
            success=True,
            message="上游不支持模型列表，已保留手工模型，可继续测试和保存",
            models=[manual_model],
            selected_model=manual_model,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            fallback=True,
            next_action="确认手工模型名称后继续测试连接",
        )
    if response.status_code != 200:
        message, retryable, next_action = _http_failure(response.status_code, outcome.attempts)
        return LlmModelsOut(
            success=False,
            message=message,
            models=[manual_model] if manual_model else [],
            selected_model=manual_model,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            fallback=bool(manual_model),
            retryable=retryable,
            next_action=next_action,
        )
    try:
        models = _extract_models(response.json())
    except (TypeError, ValueError):
        models = []
    if not models:
        return LlmModelsOut(
            success=bool(manual_model),
            message=(
                "模型列表为空，已保留手工模型，可继续测试和保存"
                if manual_model
                else "上游返回了空模型列表，请手工输入模型名称"
            ),
            models=[manual_model] if manual_model else [],
            selected_model=manual_model,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            fallback=bool(manual_model),
            next_action="手工填写并测试一个有权限的模型名称",
        )
    return LlmModelsOut(
        success=True,
        message=f"已拉取 {len(models)} 个模型",
        models=models,
        selected_model=manual_model or models[0],
        duration_ms=outcome.duration_ms,
        attempts=outcome.attempts,
    )


def test_connection(payload: ApiConfigTestIn) -> ApiConfigTestOut:
    """发送最小 Chat Completions 请求并返回可恢复的结构化结果。"""
    try:
        base_url = normalize_ai_base_url(
            payload.base_url,
            resolve_host=True,
            allow_private=False,
        )
    except ValidationError as exc:
        return ApiConfigTestOut(
            success=False,
            message=exc.message,
            next_action="请修正 Base URL 后重试",
        )
    try:
        outcome = _request_with_retries(
            "POST",
            f"{base_url}/chat/completions",
            api_key=payload.api_key,
            timeout_seconds=payload.timeout_seconds,
            max_retries=payload.max_retries,
            json_body={
                "model": payload.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
                "temperature": payload.temperature,
            },
        )
    except ValidationError as exc:
        return ApiConfigTestOut(
            success=False,
            message=exc.message,
            next_action="请修正 Base URL 后重试",
        )
    if outcome.error is not None:
        message, retryable, next_action = _transport_failure(outcome)
        return ApiConfigTestOut(
            success=False,
            message=message,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            retryable=retryable,
            next_action=next_action,
        )

    response = outcome.response
    if response.status_code != 200:
        message, retryable, next_action = _http_failure(response.status_code, outcome.attempts)
        return ApiConfigTestOut(
            success=False,
            message=message,
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            retryable=retryable,
            next_action=next_action,
        )
    try:
        body = response.json()
    except (TypeError, ValueError):
        return ApiConfigTestOut(
            success=False,
            message="上游返回 200，但响应不是有效 JSON",
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            next_action="请确认该地址是 OpenAI-compatible Chat Completions 接口",
        )
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
        return ApiConfigTestOut(
            success=False,
            message="上游返回 200，但响应结构不兼容",
            duration_ms=outcome.duration_ms,
            attempts=outcome.attempts,
            next_action="请确认该地址是 OpenAI-compatible Chat Completions 接口",
        )
    actual_model = body.get("model") if isinstance(body.get("model"), str) else payload.model
    return ApiConfigTestOut(
        success=True,
        message=f"连接成功（模型：{actual_model}）",
        model=actual_model,
        duration_ms=outcome.duration_ms,
        attempts=outcome.attempts,
        next_action="可以保存并应用此配置",
    )
