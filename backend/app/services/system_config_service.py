"""
系统配置服务(键值)

目前承载 RAG 嵌入(embedding)运行期配置:端点/模型/Key/是否启用。
读取优先级: system_config 表 > 环境变量(settings) > 本地降级。
Key 不直接回显给前端(仅返回是否已配置)。
"""
import json
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.system_config import SystemConfig
from app.utils.api_resolver import normalize_ai_base_url, validate_ai_base_url

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
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


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
        if settings.embedding_allow_private_endpoint and _is_private_embedding_url(base_url):
            # 本地嵌入服务(如 compose 内 TEI): 部署显式开启开关时读取不再被安全停用
            pass
        else:
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


def _is_private_embedding_url(url: str) -> bool:
    """与 embedding_service._is_private_url 同语义(避免循环导入的本地实现)。"""
    import ipaddress
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or ("." not in host and ":" not in host):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


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
        if not value:
            data["base_url"] = ""
        elif settings.embedding_allow_private_endpoint and _is_private_embedding_url(value):
            # 本地嵌入服务(如 compose 内 TEI): 开关由部署显式开启时放行私网端点
            data["base_url"] = value
        else:
            data["base_url"] = validate_ai_base_url(value, resolve_host=True, allow_private=False)
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
def _runtime_number(value, default, minimum, maximum, cast):
    """读取历史 JSON 中的运行参数，坏值回退默认而不是中断服务。"""
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = cast(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _llm_runtime_options(data: dict) -> dict:
    return {
        "timeout_seconds": _runtime_number(
            data.get("timeout_seconds"), settings.deepseek_timeout, 5, 600, int,
        ),
        "max_retries": _runtime_number(
            data.get("max_retries"), settings.deepseek_max_retries, 0, 5, int,
        ),
        "temperature": _runtime_number(
            data.get("temperature"), settings.deepseek_temperature, 0, 2, float,
        ),
    }


def _validated_runtime_option(name: str, value, minimum, maximum, cast):
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = cast(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{name} 必须是有效数字", code=40001) from exc
    if parsed < minimum or parsed > maximum:
        raise ValidationError(f"{name} 必须在 {minimum} 到 {maximum} 之间", code=40001)
    return parsed


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
        未配置或 JSON 不是对象时返回 None；缺字段由调用方判定是否生效。
    """
    raw = _get_raw(db, LLM_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
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
        **_llm_runtime_options(data),
    }


def _system_llm_config_public(fallback_reason: str) -> dict:
    """返回当前真正生效的系统默认配置，且仅回显密钥掩码。"""
    from app.utils.api_resolver import mask_api_key

    api_key = settings.deepseek_api_key.strip()
    return {
        "provider": "deepseek",
        "base_url": settings.deepseek_base_url,
        "model": settings.deepseek_model,
        "active": False,
        "api_key_masked": mask_api_key(api_key) if api_key else "",
        "is_set": bool(api_key),
        "source": "default",
        "fallback_reason": fallback_reason,
        "timeout_seconds": settings.deepseek_timeout,
        "max_retries": settings.deepseek_max_retries,
        "temperature": settings.deepseek_temperature,
    }


def get_llm_config_public(db: Session) -> dict:
    """供管理员前端展示当前生效的脱敏 LLM 配置。"""
    raw = _get_raw(db, LLM_KEY)
    cfg = get_llm_config(db)
    if not cfg:
        return _system_llm_config_public("invalid_config" if raw else "not_configured")

    from app.utils.api_resolver import mask_api_key

    has_key = bool(cfg["api_key"])
    effective = bool(cfg["active"] and has_key and cfg["base_url"] and cfg["model"])
    if not effective:
        try:
            stored = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            stored = {}
        encrypted_key_present = isinstance(stored, dict) and bool(stored.get("api_key_enc"))
        if encrypted_key_present and not has_key:
            reason = "credential_unavailable"
        elif not cfg["active"]:
            reason = "inactive"
        else:
            reason = "incomplete_config"
        return _system_llm_config_public(reason)

    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "active": cfg["active"],
        "api_key_masked": mask_api_key(cfg["api_key"]) if has_key else "",
        "is_set": has_key,
        "source": "global",
        "fallback_reason": "",
        "timeout_seconds": cfg["timeout_seconds"],
        "max_retries": cfg["max_retries"],
        "temperature": cfg["temperature"],
    }


def update_llm_config(
    db: Session,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    active: Optional[bool] = None,
    timeout_seconds: Optional[int] = None,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict:
    """更新全局 LLM 配置(管理员)

    api_key: None=保持原值;非空=加密覆盖;空串=清空(回退 DeepSeek)。
    """
    existing = _get_raw(db, LLM_KEY)
    data = {}
    if existing:
        try:
            parsed = json.loads(existing)
            data = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            data = {}

    if provider is not None:
        if provider not in {"deepseek", "openai", "custom"}:
            raise ValidationError("不支持的 LLM 提供商", code=40001)
        data["provider"] = provider
    if base_url is not None:
        value = base_url.strip()
        data["base_url"] = (
            normalize_ai_base_url(value, resolve_host=True, allow_private=False)
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
    if timeout_seconds is not None:
        data["timeout_seconds"] = _validated_runtime_option(
            "超时时间", timeout_seconds, 5, 600, int,
        )
    if max_retries is not None:
        data["max_retries"] = _validated_runtime_option(
            "最大重试次数", max_retries, 0, 5, int,
        )
    if temperature is not None:
        data["temperature"] = _validated_runtime_option(
            "温度系数", temperature, 0, 2, float,
        )

    try:
        _set_raw(db, LLM_KEY, json.dumps(data, ensure_ascii=False))
    except Exception as exc:
        logger.warning(
            f"[system_config] 全局 LLM 配置提交失败(error_type={type(exc).__name__})",
        )
        raise
    return get_llm_config_public(db)


# ── 模型注册表与按角色模型分配 ────────────────────────────────────────────────
# 管理员从 provider 拉取或手工登记可用模型(含视觉能力标记),再把语义角色
# (小菱对话/小菱视觉/总调度/子Agent)指到注册表中的具体模型;留空表示沿用
# 全局默认。注册表与分配都不含 Key,仅存模型标识与能力元数据。

MODEL_REGISTRY_KEY = "model_registry"
MODEL_ASSIGNMENTS_KEY = "model_assignments"

# 语义角色 -> 中文说明;新增角色必须同步前端 LlmConfig.vue 与运行时解析点。
MODEL_ASSIGNMENT_ROLES: dict[str, str] = {
    "chat": "小菱对话(用户端默认)",
    "chat_vision": "小菱视觉(消息含图片时临时使用)",
    "orchestrator": "总调度/管理端助手",
    "subagent": "子Agent默认",
}

_VISION_MODEL_MARKERS = ("vision", "-vl", "vl-", "omni", "gpt-4o", "gemini", "claude", "qwen-vl")


def guess_vision_capability(model_id: str) -> bool:
    """按模型名推断视觉能力;仅作为注册表初始标记,管理员可改。"""
    lowered = (model_id or "").lower()
    return any(marker in lowered for marker in _VISION_MODEL_MARKERS)


def _normalize_registry_entry(entry: dict, *, source: str) -> Optional[dict]:
    model_id = str(entry.get("id") or entry.get("model") or "").strip()
    if not model_id or len(model_id) > 128:
        return None
    return {
        "id": model_id,
        "label": str(entry.get("label") or model_id).strip()[:64] or model_id,
        "vision": bool(entry.get("vision")),
        "source": source if source in {"pulled", "manual"} else "manual",
        "added_at": str(entry.get("added_at") or ""),
    }


def get_model_registry(db: Session) -> list[dict]:
    """读取模型注册表;坏数据自动清空重建,不抛错。"""
    raw = _get_raw(db, MODEL_REGISTRY_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    seen, result = set(), []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        normalized = _normalize_registry_entry(entry, source=str(entry.get("source") or "manual"))
        if normalized and normalized["id"] not in seen:
            seen.add(normalized["id"])
            result.append(normalized)
    return result


def replace_model_registry(db: Session, models: list[dict]) -> list[dict]:
    """整表替换(管理员手工编辑保存)。"""
    normalized, seen = [], set()
    for entry in models:
        if not isinstance(entry, dict):
            continue
        item = _normalize_registry_entry(entry, source=str(entry.get("source") or "manual"))
        if item and item["id"] not in seen:
            seen.add(item["id"])
            normalized.append(item)
    _set_raw(db, MODEL_REGISTRY_KEY, json.dumps({"models": normalized}, ensure_ascii=False))
    return normalized


def merge_pulled_models(db: Session, pulled_ids: list[str]) -> tuple[list[dict], list[str]]:
    """把 provider /models 拉取结果合并进注册表;返回(新注册表, 新增ids)。

    已存在条目仅刷新能力推断,不覆盖管理员手工修改的 label/vision;
    拉取列表里消失的条目保留(可能是手工登记或上游列表不全)。
    """
    existing = {m["id"]: m for m in get_model_registry(db)}
    added: list[str] = []
    for raw_id in pulled_ids:
        model_id = str(raw_id or "").strip()
        if not model_id:
            continue
        if model_id in existing:
            continue
        entry = _normalize_registry_entry(
            {"id": model_id}, source="pulled",
        )
        if entry is None:
            continue
        entry["vision"] = guess_vision_capability(model_id)
        entry["added_at"] = datetime.now(timezone.utc).isoformat()
        existing[model_id] = entry
        added.append(model_id)
    ordered = sorted(existing.values(), key=lambda m: m["id"])
    _set_raw(db, MODEL_REGISTRY_KEY, json.dumps({"models": ordered}, ensure_ascii=False))
    return ordered, added


def get_model_assignments(db: Session) -> dict[str, str]:
    """读取角色->模型分配;只返回合法角色的非空字符串值。"""
    raw = _get_raw(db, MODEL_ASSIGNMENTS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for role, model_id in data.items():
        if role in MODEL_ASSIGNMENT_ROLES and isinstance(model_id, str) and model_id.strip():
            result[role] = model_id.strip()
    return result


def update_model_assignments(db: Session, assignments: dict[str, str]) -> dict[str, str]:
    """更新角色分配;模型必须已在注册表(或允许指向当前全局默认模型的任意 id)。

    空串/None 表示清除该角色分配,回退全局默认。
    """
    if not isinstance(assignments, dict):
        raise ValidationError("分配格式不正确", code=40001)
    registry_ids = {m["id"] for m in get_model_registry(db)}
    current = get_model_assignments(db)
    for role, model_id in assignments.items():
        if role not in MODEL_ASSIGNMENT_ROLES:
            raise ValidationError(f"未知的模型分配角色: {role}", code=40001)
        value = (model_id or "").strip()
        if value and value not in registry_ids:
            raise ValidationError(
                f"模型 {value} 未在注册表中,请先在模型注册表添加", code=40001,
            )
        if value:
            current[role] = value
        else:
            current.pop(role, None)
    _set_raw(db, MODEL_ASSIGNMENTS_KEY, json.dumps(current, ensure_ascii=False))
    return current


def resolve_model_assignment(db: Session, role: str, default: str = "") -> str:
    """运行时解析角色模型;未分配/表缺失时返回 default(全局默认)。"""
    assignments = get_model_assignments(db)
    assigned = assignments.get(role, "")
    return assigned if assigned else (default or "")
