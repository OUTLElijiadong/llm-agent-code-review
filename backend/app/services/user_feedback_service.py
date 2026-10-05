"""
用户反馈服务(向管理员)

权限:
- 创建: 任意登录用户
- 查看: 本人或管理员
- 回复/改状态: 仅管理员

注意: 与 services/feedback_service.py(Agent 自进化的反馈聚合)是两回事。
"""
import json
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import Pagination
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.user_feedback import UserFeedback
from app.services.rbac_service import is_admin_user

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
    if not is_admin_user(db, user.id) or mine:
        q = q.filter(UserFeedback.user_id == user.id)
    if status:
        q = q.filter(UserFeedback.status == status)
    if feedback_type:
        q = q.filter(UserFeedback.feedback_type == feedback_type)
    total = q.count()
    pg = Pagination(page, page_size, total)
    rows = (q.order_by(UserFeedback.create_time.desc(), UserFeedback.id.desc())
            .offset(pg.offset).limit(pg.page_size).all())
    return pg.to_dict([_to_dict(f) for f in rows])


def get_feedback(db: Session, user: User, feedback_id: int) -> dict:
    fb = db.get(UserFeedback, feedback_id)
    if not fb:
        raise NotFoundError("反馈不存在", code=40400)
    if fb.user_id != user.id and not is_admin_user(db, user.id):
        raise ForbiddenError("无权查看该反馈", code=40300)
    return _to_dict(fb)


def mark_feedback_read(db: Session, admin: User, feedback_id: int) -> dict:
    """显式确认管理员已读；详情 GET 保持只读。"""
    if not is_admin_user(db, admin.id):
        raise ForbiddenError("需要管理员权限", code=40300)
    fb = db.query(UserFeedback).filter(UserFeedback.id == feedback_id).with_for_update().first()
    if not fb:
        raise NotFoundError("反馈不存在", code=40400)
    if fb.status == "new":
        previous_status = fb.status
        fb.status = "read"
        _commit_handling(db, admin, fb, "feedback.mark_read", previous_status, fb.admin_reply)
    return _to_dict(fb)


def reply_feedback(db: Session, admin: User, feedback_id: int, payload: dict) -> dict:
    if not is_admin_user(db, admin.id):
        raise ForbiddenError("需要管理员权限", code=40300)
    fb = db.query(UserFeedback).filter(UserFeedback.id == feedback_id).with_for_update().first()
    if not fb:
        raise NotFoundError("反馈不存在", code=40400)
    previous_status, previous_reply = fb.status, fb.admin_reply
    reply = previous_reply
    status = previous_status
    if "admin_reply" in payload and payload["admin_reply"] is not None:
        reply = payload["admin_reply"].strip()
        if reply:
            status = "replied"
    new_status = payload.get("status")
    if new_status and new_status in _STATUS:
        status = new_status
    if status == "replied" and not (reply or "").strip():
        raise ValidationError("标记为已回复时，请填写回复内容")
    if status == previous_status and (reply or "") == (previous_reply or ""):
        return _to_dict(fb)
    fb.status, fb.admin_reply = status, reply
    _commit_handling(db, admin, fb, "feedback.update", previous_status, previous_reply)
    return _to_dict(fb)


def stats_for_admin(db: Session, admin: User) -> dict:
    if not is_admin_user(db, admin.id):
        raise ForbiddenError("需要管理员权限", code=40300)
    return _stats(db.query(UserFeedback.status, func.count(UserFeedback.id)))


def stats_for_user(db: Session, user: User) -> dict:
    query = db.query(UserFeedback.status, func.count(UserFeedback.id))
    if not is_admin_user(db, user.id):
        query = query.filter(UserFeedback.user_id == user.id)
    return _stats(query)


def _stats(query) -> dict:
    out = {s: 0 for s in _STATUS}
    for status, count in query.group_by(UserFeedback.status).all():
        out[status] = count
    out["total"] = sum(out.values())
    return out


def _commit_handling(db: Session, admin: User, fb: UserFeedback, action: str,
                     previous_status: str, previous_reply: str | None) -> None:
    """处理结果与审计同事务；审计保留覆盖证据，不复制私人回复正文。"""
    fb.handled_by = admin.id
    fb.handled_at = datetime.now(timezone.utc)
    detail = {
        "previous_status": previous_status, "status": fb.status,
        "reply_changed": (previous_reply or "") != (fb.admin_reply or ""),
        "previous_reply_sha256": sha256((previous_reply or "").encode()).hexdigest(),
        "reply_sha256": sha256((fb.admin_reply or "").encode()).hexdigest(),
        "previous_reply_length": len(previous_reply or ""), "reply_length": len(fb.admin_reply or ""),
    }
    db.add(AuditLog(actor_id=admin.id, actor_name=admin.username, action=action,
                    target_type="user_feedback", target_id=str(fb.id),
                    detail=json.dumps(detail, ensure_ascii=False), status="success"))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(fb)


def _to_dict(f: UserFeedback) -> dict:
    return {
        "id": f.id, "user_id": f.user_id, "feedback_type": f.feedback_type,
        "content": f.content, "contact": f.contact, "status": f.status,
        "admin_reply": f.admin_reply, "handled_by": f.handled_by,
        "handled_at": f.handled_at, "create_time": f.create_time,
        "update_time": f.update_time,
    }
