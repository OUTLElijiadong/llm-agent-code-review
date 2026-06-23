"""
维修工单服务(平台问题)

权限:
- 创建: 任意登录用户
- 查看/撤销: 本人或管理员
- 受理/回复/改状态: 仅管理员
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import Pagination
from app.models.maintenance_ticket import MaintenanceTicket
from app.models.user import User

_CATEGORIES = ("bug", "account", "feature", "performance", "other")
_PRIORITIES = ("low", "medium", "high")
_USER_STATUS = ("pending", "processing", "resolved", "closed")


def create_ticket(db: Session, user: User, payload: dict) -> MaintenanceTicket:
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    if not title or not description:
        raise ValidationError("标题和描述不能为空", code=42201)
    category = payload.get("category") if payload.get("category") in _CATEGORIES else "bug"
    priority = payload.get("priority") if payload.get("priority") in _PRIORITIES else "medium"
    ticket = MaintenanceTicket(
        user_id=user.id, title=title[:150], description=description,
        category=category, priority=priority, status="pending",
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def list_tickets(db: Session, user: User, status: str = "", mine: bool = True,
                 page: int = 1, page_size: int = 20) -> dict:
    """列表。管理员 mine=False 时看全部;否则只看本人。"""
    q = db.query(MaintenanceTicket)
    if user.role != "admin" or mine:
        q = q.filter(MaintenanceTicket.user_id == user.id)
    if status:
        q = q.filter(MaintenanceTicket.status == status)
    total = q.count()
    pg = Pagination(page, page_size, total)
    rows = (q.order_by(MaintenanceTicket.create_time.desc())
            .offset(pg.offset).limit(pg.page_size).all())
    return pg.to_dict([_to_dict(t) for t in rows])


def get_ticket(db: Session, user: User, ticket_id: int) -> dict:
    ticket = db.get(MaintenanceTicket, ticket_id)
    if not ticket:
        raise NotFoundError("工单不存在", code=40400)
    if ticket.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无权查看该工单", code=40300)
    return _to_dict(ticket)


def handle_ticket(db: Session, admin: User, ticket_id: int, payload: dict) -> dict:
    """管理员受理/回复/改状态"""
    if admin.role != "admin":
        raise ForbiddenError("需要管理员权限", code=40300)
    ticket = db.get(MaintenanceTicket, ticket_id)
    if not ticket:
        raise NotFoundError("工单不存在", code=40400)
    new_status = payload.get("status")
    if new_status:
        if new_status not in _USER_STATUS:
            raise ValidationError("非法状态", code=42201)
        ticket.status = new_status
    if "admin_reply" in payload and payload["admin_reply"] is not None:
        ticket.admin_reply = payload["admin_reply"]
    if payload.get("priority") in _PRIORITIES:
        ticket.priority = payload["priority"]
    ticket.handled_by = admin.id
    ticket.handled_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ticket)
    return _to_dict(ticket)


def close_own_ticket(db: Session, user: User, ticket_id: int) -> dict:
    """用户撤销/关闭自己的工单"""
    ticket = db.get(MaintenanceTicket, ticket_id)
    if not ticket:
        raise NotFoundError("工单不存在", code=40400)
    if ticket.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无权操作该工单", code=40300)
    ticket.status = "closed"
    db.commit()
    db.refresh(ticket)
    return _to_dict(ticket)


def stats_for_admin(db: Session, admin: User) -> dict:
    if admin.role != "admin":
        raise ForbiddenError("需要管理员权限", code=40300)
    out = {s: 0 for s in _USER_STATUS}
    for status, in db.query(MaintenanceTicket.status).all():
        out[status] = out.get(status, 0) + 1
    out["total"] = sum(out.values())
    return out


def _to_dict(t: MaintenanceTicket) -> dict:
    return {
        "id": t.id, "user_id": t.user_id, "title": t.title,
        "category": t.category, "description": t.description,
        "priority": t.priority, "status": t.status,
        "admin_reply": t.admin_reply, "handled_by": t.handled_by,
        "handled_at": t.handled_at, "create_time": t.create_time,
        "update_time": t.update_time,
    }
