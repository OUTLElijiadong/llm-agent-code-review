"""
全局大模型(LLM)提供商配置 API 路由(管理员)

管理员可在「系统默认 DeepSeek」与「自定义 OpenAI 兼容端点」间切换,
切换后对全平台生效(用户自定义 API 配置仍优先于此全局设置)。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_super_admin
from app.models.user import User
from app.schemas.api_config import ApiConfigTestIn, ApiConfigTestOut
from app.schemas.common import Resp
from app.schemas.llm_config import LlmConfigIn, LlmConfigOut, LlmTestIn
from app.services import api_config_service, system_config_service

router = APIRouter()


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
        model=payload.model, api_key=payload.api_key, active=payload.active)
    return Resp(data=LlmConfigOut(**data))


@router.post("/test", response_model=Resp[ApiConfigTestOut])
def test_config(payload: LlmTestIn, db: Session = Depends(get_db),
                admin: User = Depends(require_super_admin)):
    """由唯一超级管理员测试连通性;留空字段则用已保存配置。"""
    stored = system_config_service.get_llm_config(db) or {}
    base_url = (payload.base_url or stored.get("base_url") or "").strip()
    model = (payload.model or stored.get("model") or "").strip()
    api_key = payload.api_key or stored.get("api_key") or ""
    if not (base_url and model and api_key):
        return Resp(
            data=ApiConfigTestOut(success=False, message="请先填写端点、模型与 API Key"),
            code=-1, message="配置不完整",
        )
    result = api_config_service.test_connection(ApiConfigTestIn(
        provider="custom", api_key=api_key, base_url=base_url, model=model))
    return Resp(data=result, code=0 if result.success else -1, message=result.message)
