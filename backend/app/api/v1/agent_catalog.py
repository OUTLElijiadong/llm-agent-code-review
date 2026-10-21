"""Authenticated catalog API for globally published custom agents."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.agent_studio import CatalogAgentOut
from app.schemas.common import Resp
from app.services import agent_studio_service

router = APIRouter()


@router.get("", response_model=Resp[list[CatalogAgentOut]])
def list_catalog(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission(PermissionCode.CUSTOM_AGENT_INVOKE)),
):
    return Resp(data=[CatalogAgentOut(**row) for row in agent_studio_service.list_catalog(db)])


# 已发布 Agent 只能由小菱的 invoke_published_agent 工具调用（作为小菱的子 Agent），
# 目录接口只保留只读列表。
