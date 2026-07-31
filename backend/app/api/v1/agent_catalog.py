"""Authenticated catalog API for globally published custom agents."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.agent_studio import CatalogAgentOut, CatalogInvokeIn
from app.schemas.common import Resp
from app.services import agent_studio_service, published_agent_tools

router = APIRouter()


@router.get("", response_model=Resp[list[CatalogAgentOut]])
def list_catalog(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission(PermissionCode.CUSTOM_AGENT_INVOKE)),
):
    return Resp(data=[CatalogAgentOut(**row) for row in agent_studio_service.list_catalog(db)])


@router.post("/{agent_code}/invoke", response_model=Resp[dict])
def invoke_agent(
    agent_code: str,
    payload: CatalogInvokeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(PermissionCode.CUSTOM_AGENT_INVOKE)),
):
    return Resp(data=published_agent_tools.invoke_published_agent(
        db,
        user,
        agent_code=agent_code,
        code=payload.code,
        language=payload.language,
        file_name=payload.file_name,
        rules=payload.rules,
        line_offset=payload.line_offset,
        experience=payload.experience,
    ))
