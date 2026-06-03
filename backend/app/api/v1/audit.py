"""
操作审计 API 路由(管理员)
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.models.user import User
from app.schemas.audit import AuditLogOut
from app.schemas.common import PageOut, Resp
from app.services import audit_service

router = APIRouter()


@router.get("", response_model=Resp[PageOut[AuditLogOut]])
def list_audit_logs(
    action: str = Query(""),
    keyword: str = Query(""),
    actor_id: Optional[int] = Query(None),
    start: str = Query(""),
    end: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """审计日志列表(仅管理员)"""
    result = audit_service.list_logs(db, action, keyword, actor_id, start, end, page, page_size)
    return Resp(data=PageOut(**result))
