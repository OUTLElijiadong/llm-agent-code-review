"""
系统配置服务(键值)

目前承载 RAG 嵌入(embedding)运行期配置:端点/模型/Key/是否启用。
读取优先级: system_config 表 > 环境变量(settings) > 本地降级。
Key 不直接回显给前端(仅返回是否已配置)。
"""
import json
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.system_config import SystemConfig
from app.utils.api_resolver import validate_ai_base_url

# 配置键常量
EMBEDDING_KEY = "embedding"
LLM_KEY = "llm_provider"


def _get_raw(db: Session, key: str) -> Optional[str]:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    return row.config_value if row else None


def _set_raw(db: Session, key: str, value: str) -> None:
    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if row:
        row.config_value = value
    else:
        db.add(SystemConfig(config_key=key, config_value=value))
    db.commit()


def get_embedding_config(db: Session) -> dict:
    """获取生效的 embedding 配置(表 > 环境变量)

    Returns:
        dict: { base_url, api_key, model, enabled }
        其中 api_key 为真实值(仅供后端服务内部使用,不要直接回传前端)。
    """
    base_url = settings.embedding_base_url or ""
    api_key = settings.embedding_api_key or ""
    model = settings.embedding_model or ""
    enabled = bool(base_url and api_key and model)

    raw = _get_raw(db, EMBEDDING_KEY)
    if raw:
        try:
            data = json.loads(raw)
            base_url = (data.get("base_url") or base_url).strip()
            # 空字符串表示沿用环境变量,不覆盖
            if data.get("api_key"):
                api_key = data["api_key"]
            model = (data.get("model") or model).strip()
            if "enabled" in data:
                enabled = bool(data["enabled"]) and bool(base_url and api_key and model)
            else:
                enabled = bool(base_url and api_key and model)
        except (json.JSONDecodeError, AttributeError):
            pass

    if enabled:
        try:
            base_url = validate_ai_base_url(base_url, resolve_host=True, allow_private=False)
        except ValidationError as exc:
            logger.warning(f"[system_config] embedding 端点已安全停用: {exc.message}")
            enabled = False

    return {"base_url": base_url, "api_key": api_key, "model": model, "enabled": enabled}


def get_embedding_config_public(db: Session) -> dict:
    """供管理员前端展示的脱敏配置(不含明文 Key)"""
    cfg = get_embedding_config(db)
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "enabled": cfg["enabled"],
        "api_key_set": bool(cfg["api_key"]),
    }


def update_embedding_config(
    db: Session,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """更新 embedding 配置(管理员)

    api_key 传 None 表示保持原值;传空字符串表示清空(回退环境变量/降级)。
    """
    existing_raw = _get_raw(db, EMBEDDING_KEY)
    data = {}
    if existing_raw:
        try:
            data = json.loads(existing_raw)
        except json.JSONDecodeError:
            data = {}

    if base_url is not None:
        value = base_url.strip()
        data["base_url"] = (
            validate_ai_base_url(value, resolve_host=True, allow_private=False)
            if value
            else ""
        )
    if model is not None:
        data["model"] = model.strip()
    if api_key is not None:
        data["api_key"] = api_key.strip()
    if enabled is not None:
        data["enabled"] = bool(enabled)

    _set_raw(db, EMBEDDING_KEY, json.dumps(data, ensure_ascii=False))
    return get_embedding_config_public(db)


# ──────────────────────────────────────────────────────────
# 全局大模型(LLM)提供商配置 — 管理员可在 DeepSeek 与自定义 OpenAI 兼容端点间切换
# ──────────────────────────────────────────────────────────
def _persist_llm_security_change(db: Session, data: dict, action: str) -> bool:
    """持久化全局 LLM Key 的轮换或失效结果。

    Args:
        db: SQLAlchemy 数据库会话。
        data: 不会写入日志的完整全局 LLM 配置。
        action: 不含敏感值的操作标识。

    Returns:
        bool: 提交成功为 True；回滚后为 False。
    """
    try:
        _set_raw(db, LLM_KEY, json.dumps(data, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            f"[system_config] 全局 LLM API Key 安全变更提交失败("
            f"action={action}, error_type={type(exc).__name__})",
        )
        return False
    return True


def get_llm_config(db: Session) -> Optional[dict]:
    """获取生效的全局 LLM 覆盖配置(含解密后的真实 Key,仅供后端内部使用)

    Returns:
        dict | None: { provider, base_url, model, api_key, active };
        未配置或缺字段时返回 None,调用方应回退系统默认 DeepSeek。
    """
    raw = _get_raw(db, LLM_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    api_key = ""
    active = bool(data.get("active"))
    enc = data.get("api_key_enc")
    if enc:
        from app.utils.api_resolver import (
            decrypt_api_key_with_metadata,
            encrypt_api_key,
        )

        decryption = decrypt_api_key_with_metadata(enc)
        if decryption is None:
            if active:
                data["active"] = False
                _persist_llm_security_change(db, data, "deactivate_global")
                logger.warning("[system_config] 全局 LLM API Key 无法解密，配置已停用")
            active = False
        else:
            api_key = decryption.plaintext
            if decryption.needs_rotation:
                data["api_key_enc"] = encrypt_api_key(api_key)
                if not _persist_llm_security_change(db, data, "rotate_global"):
                    api_key = ""
                    active = False

    return {
        "provider": data.get("provider") or "custom",
        "base_url": (data.get("base_url") or "").strip(),
        "model": (data.get("model") or "").strip(),
        "api_key": api_key,
        "active": active,
    }


def get_llm_config_public(db: Session) -> dict:
    """供管理员前端展示的脱敏 LLM 配置"""
    cfg = get_llm_config(db)
    if not cfg:
        return {
            "provider": "deepseek", "base_url": "", "model": "",
            "active": False, "api_key_masked": "", "is_set": False,
            "source": "default",
        }
    from app.utils.api_resolver import mask_api_key
    has_key = bool(cfg["api_key"])
    effective = bool(cfg["active"] and has_key and cfg["base_url"] and cfg["model"])
    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "active": cfg["active"],
        "api_key_masked": mask_api_key(cfg["api_key"]) if has_key else "",
        "is_set": has_key,
        "source": "global" if effective else "default",
    }


def update_llm_config(
    db: Session,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    active: Optional[bool] = None,
) -> dict:
    """更新全局 LLM 配置(管理员)

    api_key: None=保持原值;非空=加密覆盖;空串=清空(回退 DeepSeek)。
    """
    existing = _get_raw(db, LLM_KEY)
    data = {}
    if existing:
        try:
            data = json.loads(existing)
        except json.JSONDecodeError:
            data = {}

    if provider is not None:
        data["provider"] = provider
    if base_url is not None:
        value = base_url.strip()
        data["base_url"] = (
            validate_ai_base_url(value, resolve_host=True, allow_private=False)
            if value
            else ""
        )
    if model is not None:
        data["model"] = model.strip()
    if api_key is not None:
        if api_key.strip():
            from app.utils.api_resolver import encrypt_api_key
            data["api_key_enc"] = encrypt_api_key(api_key.strip())
        else:
            data["api_key_enc"] = ""
    if active is not None:
        data["active"] = bool(active)

    _set_raw(db, LLM_KEY, json.dumps(data, ensure_ascii=False))
    return get_llm_config_public(db)
