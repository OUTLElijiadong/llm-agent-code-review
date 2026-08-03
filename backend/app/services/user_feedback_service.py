"""
用户反馈服务(向管理员)

权限:
- 创建: 任意登录用户
- 查看: 本人或管理员
- 回复/改状态: 仅管理员

注意: 与 services/feedback_service.py(Agent 自进化的反馈聚合)是两回事。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import Pagination
from app.models.user import User
from app.models.user_feedback import UserFeedback

_TYPES = ("suggestion", "complaint", "praise", "bug", "other")
_STATUS = ("new", "read", "replied", "closed")


def create_feedback(db: Session, user: User, payload: dict) -> UserFeedback:
    content = (payload.get("content") or "").strip()
    if not content:
        raise ValidationError("反馈内容不能为空", code=42201)
    ftype = payload.get("feedback_type") if payload.get("feedback_type") in _TYPES else "suggestion"
    fb = UserFeedback(
        user_id=user.id, feedback_type=ftype, content=content,
        contact=(payload.get("contact") or "").strip()[:100] or None,
        status="new",
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return fb


def list_feedback(db: Session, user: User, status: str = "", feedback_type: str = "",
                  mine: bool = True, page: int = 1, page_size: int = 20) -> dict:
    q = db.query(UserFeedback)
    if user.role not in {"admin", "super_admin"} or mine:
        q = q.filter(UserFeedback.user_id == user.id)
    if status:
        q = q.filter(UserFeedback.status == status)
    if feedback_type:
        q = q.filter(UserFeedback.feedback_type == feedback_type)
    total = q.count()
    pg = Pagination(page, page_size, total)
    rows = (q.order_by(UserFeedback.create_time.desc())
            .offset(pg.offset).limit(pg.page_size).all())
    return pg.to_dict([_to_dict(f) for f in rows])


def get_feedback(db: Session, user: User, feedback_id: int) -> dict:
    fb = db.get(UserFeedback, feedback_id)
    if not fb:
        raise NotFoundError("反馈不存在", code=40400)
    if fb.user_id != user.id and user.role not in {"admin", "super_admin"}:
        raise ForbiddenError("无权查看该反馈", code=40300)
    # 管理员打开即标记已读
    if user.role in {"admin", "super_admin"} and fb.status == "new":
        fb.status = "read"
        db.commit()
        db.refresh(fb)
    return _to_dict(fb)


def reply_feedback(db: Session, admin: User, feedback_id: int, payload: dict) -> dict:
    if admin.role not in {"admin", "super_admin"}:
        raise ForbiddenError("需要管理员权限", code=40300)
    fb = db.get(UserFeedback, feedback_id)
    if not fb:
        raise NotFoundError("反馈不存在", code=40400)
    if "admin_reply" in payload and payload["admin_reply"] is not None:
        fb.admin_reply = payload["admin_reply"]
        fb.status = "replied"
    new_status = payload.get("status")
    if new_status and new_status in _STATUS:
        fb.status = new_status
    fb.handled_by = admin.id
    fb.handled_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(fb)
    return _to_dict(fb)


def stats_for_admin(db: Session, admin: User) -> dict:
    if admin.role not in {"admin", "super_admin"}:
        raise ForbiddenError("需要管理员权限", code=40300)
    out = {s: 0 for s in _STATUS}
    for status, in db.query(UserFeedback.status).all():
        out[status] = out.get(status, 0) + 1
    out["total"] = sum(out.values())
    return out


def _to_dict(f: UserFeedback) -> dict:
    return {
        "id": f.id, "user_id": f.user_id, "feedback_type": f.feedback_type,
        "content": f.content, "contact": f.contact, "status": f.status,
        "admin_reply": f.admin_reply, "handled_by": f.handled_by,
        "handled_at": f.handled_at, "create_time": f.create_time,
        "update_time": f.update_time,
    }
