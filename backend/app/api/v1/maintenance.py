"""
维修工单 API 路由(平台问题)
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.maintenance import TicketHandleIn, TicketIn, TicketOut
from app.services import maintenance_service

router = APIRouter()


@router.post("", response_model=Resp[dict])
def create_ticket(payload: TicketIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """提交维修工单"""
    t = maintenance_service.create_ticket(db, user, payload.model_dump())
    return Resp(data={"id": t.id})


@router.get("", response_model=Resp[PageOut[TicketOut]])
def list_tickets(
    status: str = Query(""),
    scope: str = Query("mine", pattern="^(mine|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """工单列表;scope=all 仅管理员可看全部"""
    mine = scope == "mine"
    result = maintenance_service.list_tickets(db, user, status, mine, page, page_size)
    return Resp(data=PageOut(**result))


@router.get("/stats", response_model=Resp[dict])
def ticket_stats(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """工单状态统计(管理员)"""
    return Resp(data=maintenance_service.stats_for_admin(db, admin))


@router.get("/{ticket_id}", response_model=Resp[TicketOut])
def get_ticket(ticket_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """工单详情(本人或管理员)"""
    return Resp(data=TicketOut(**maintenance_service.get_ticket(db, user, ticket_id)))


@router.put("/{ticket_id}/handle", response_model=Resp[TicketOut])
def handle_ticket(ticket_id: int, payload: TicketHandleIn,
                  db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """受理/回复/改状态(管理员)"""
    data = maintenance_service.handle_ticket(db, admin, ticket_id, payload.model_dump(exclude_none=True))
    return Resp(data=TicketOut(**data))


@router.post("/{ticket_id}/close", response_model=Resp[TicketOut])
def close_ticket(ticket_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """撤销/关闭自己的工单"""
    return Resp(data=TicketOut(**maintenance_service.close_own_ticket(db, user, ticket_id)))
