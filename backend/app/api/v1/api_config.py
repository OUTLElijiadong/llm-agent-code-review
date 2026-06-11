"""
用户 API 配置路由
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.api_config import (
    ApiConfigOut,
    ApiConfigSaveIn,
    ApiConfigTestIn,
    ApiConfigTestOut,
)
from app.schemas.common import Resp
from app.services import api_config_service

router = APIRouter(prefix="/api-config", tags=["API配置"])


@router.get("", response_model=Resp[ApiConfigOut])
def get_config(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取当前用户的 API 配置

    返回用户自定义配置或系统默认配置。
    API Key 始终脱敏返回。
    """
    cfg = api_config_service.get_config(db, user.id)
    return Resp(data=cfg)


@router.put("", response_model=Resp[ApiConfigOut])
def save_config(
    payload: ApiConfigSaveIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """保存或更新用户的 API 配置

    配置后即刻生效：审查、聊天、安全扫描均使用用户自定义 API。
    """
    cfg = api_config_service.save_config(db, user.id, payload)
    return Resp(data=cfg)


@router.delete("", response_model=Resp[str])
def delete_config(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除用户的 API 配置

    恢复使用系统默认 DeepSeek API。
    """
    api_config_service.delete_config(db, user.id)
    return Resp(data="已恢复系统默认 API 配置")


@router.post("/test", response_model=Resp[ApiConfigTestOut])
def test_connection(
    payload: ApiConfigTestIn,
    user: User = Depends(get_current_user),
):
    """测试 API 连接

    发送最小化请求验证连通性和认证，不存储任何数据。
    """
    result = api_config_service.test_connection(payload)
    return Resp(data=result, code=0 if result.success else -1, message=result.message)
