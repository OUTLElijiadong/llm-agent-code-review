"""
全局大模型(LLM)提供商配置 API 路由(管理员)

管理员可在「系统默认 DeepSeek」与「自定义 OpenAI 兼容端点」间切换,
切换后对全平台生效(用户自定义 API 配置仍优先于此全局设置)。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import require_super_admin
from app.models.user import User
from app.schemas.api_config import ApiConfigTestIn, ApiConfigTestOut
from app.schemas.common import Resp
from app.schemas.llm_config import (
    LlmConfigIn,
    LlmConfigOut,
    LlmModelsIn,
    LlmModelsOut,
    LlmTestIn,
)
from app.services import api_config_service, audit_service, system_config_service

router = APIRouter()


def _endpoint_identity(value: str) -> str:
    """比较 Key 是否仍绑定同一上游；异常输入交给正式请求校验。"""
    from app.utils.api_resolver import normalize_ai_base_url

    try:
        return normalize_ai_base_url(value, resolve_host=False, allow_private=False)
    except Exception:  # noqa: BLE001
        return (value or "").strip().rstrip("/")


def _resolve_draft(payload: LlmTestIn, db: Session) -> dict:
    """合并未保存表单与持久配置，禁止把旧 Key 发送到新端点。"""
    stored = system_config_service.get_llm_config(db)
    system = {
        "provider": "deepseek",
        "base_url": settings.deepseek_base_url,
        "model": settings.deepseek_model,
        "api_key": settings.deepseek_api_key,
        "timeout_seconds": settings.deepseek_timeout,
        "max_retries": settings.deepseek_max_retries,
        "temperature": settings.deepseek_temperature,
    }
    stored_effective = bool(
        stored
        and stored.get("active")
        and stored.get("api_key")
        and stored.get("base_url")
        and stored.get("model")
    )
    source = stored if stored_effective else system
    base_url = payload.base_url if payload.base_url is not None else source.get("base_url", "")
    if payload.api_key is not None:
        api_key = payload.api_key.strip()
    elif (
        stored
        and stored_effective
        and stored.get("api_key")
        and _endpoint_identity(base_url) == _endpoint_identity(stored.get("base_url", ""))
    ):
        api_key = stored["api_key"]
    elif _endpoint_identity(base_url) == _endpoint_identity(system["base_url"]):
        api_key = system["api_key"]
    else:
        api_key = ""
    return {
        "provider": payload.provider or source.get("provider") or "custom",
        "base_url": (base_url or "").strip(),
        "model": (payload.model if payload.model is not None else source.get("model", "")).strip(),
        "api_key": api_key,
        "timeout_seconds": (
            payload.timeout_seconds
            if payload.timeout_seconds is not None
            else source.get("timeout_seconds", settings.deepseek_timeout)
        ),
        "max_retries": (
            payload.max_retries
            if payload.max_retries is not None
            else source.get("max_retries", settings.deepseek_max_retries)
        ),
        "temperature": (
            payload.temperature
            if payload.temperature is not None
            else source.get("temperature", settings.deepseek_temperature)
        ),
    }


@router.get("/config", response_model=Resp[LlmConfigOut])
def get_config(db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    """由唯一超级管理员查看当前全局 LLM 配置(Key 脱敏)。"""
    return Resp(data=LlmConfigOut(**system_config_service.get_llm_config_public(db)))


@router.put("/config", response_model=Resp[LlmConfigOut])
def update_config(payload: LlmConfigIn, db: Session = Depends(get_db),
                  admin: User = Depends(require_super_admin)):
    """由唯一超级管理员更新全局 LLM 配置。"""
    data = system_config_service.update_llm_config(
        db, provider=payload.provider, base_url=payload.base_url,
        model=payload.model, api_key=payload.api_key, active=payload.active,
        timeout_seconds=payload.timeout_seconds, max_retries=payload.max_retries,
        temperature=payload.temperature)
    audit_service.log(
        db, admin, "llm_config_update",
        target_type="system_config", target_id="llm",
        detail=f"全局LLM配置更新 provider={payload.provider} model={payload.model}"
               f" key={'已变更' if payload.api_key is not None else '未变'} active={payload.active}",
    )
    return Resp(data=LlmConfigOut(**data))


@router.post("/test", response_model=Resp[ApiConfigTestOut])
def test_config(payload: LlmTestIn, db: Session = Depends(get_db),
                admin: User = Depends(require_super_admin)):
    """由唯一超级管理员测试连通性;留空字段则用已保存配置。"""
    draft = _resolve_draft(payload, db)
    if not (draft["base_url"] and draft["model"] and draft["api_key"]):
        return Resp(
            data=ApiConfigTestOut(success=False, message="请先填写端点、模型与 API Key"),
            message="配置不完整",
        )
    result = api_config_service.test_connection(ApiConfigTestIn(**draft))
    return Resp(data=result, message=result.message)


@router.post("/models", response_model=Resp[LlmModelsOut])
def list_models(payload: LlmModelsIn, db: Session = Depends(get_db),
                admin: User = Depends(require_super_admin)):
    """从未保存草稿或当前配置拉取模型；失败时保留手工模型。"""
    draft = _resolve_draft(payload, db)
    result = api_config_service.fetch_models(LlmModelsIn(**draft))
    return Resp(data=result, message=result.message)
