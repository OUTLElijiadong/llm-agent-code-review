"""
AI调用日志API路由(管理员)
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.models.user import User
from app.schemas.ai_log import AiLogDetailOut, AiLogOut
from app.schemas.common import PageOut, Resp
from app.services import ai_log_service

router = APIRouter()


@router.get("", response_model=Resp[PageOut[AiLogOut]])
def list_logs(
    task_id: int = Query(None),
    user_id: int = Query(None),
    status: str = Query(""),
    start: str = Query(""),
    end: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """AI调用日志列表(管理员)"""
    result = ai_log_service.list_logs(db, task_id, user_id, status, start, end, page, page_size)
    return Resp(data=PageOut(**result))


@router.get("/{log_id}", response_model=Resp[AiLogDetailOut])
def get_log(log_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """AI调用日志详情(管理员)"""
    log = ai_log_service.get_log_detail(db, log_id)
    return Resp(data=AiLogDetailOut.model_validate(log))
