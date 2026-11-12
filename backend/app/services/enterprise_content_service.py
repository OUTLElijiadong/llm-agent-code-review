"""仅本企业成员可访问的企业论坛与公告。"""
import hashlib
import json

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_content import EnterpriseAnnouncement, EnterpriseForumPost, EnterpriseForumReply
from app.models.enterprise_org import EnterpriseAuditEvent
from app.services.enterprise_chat_service import _lock_enterprise, _require_locked_member
from app.services.enterprise_org_service import require_enterprise_member


def _lock(db: Session, enterprise_id: int, user_id: int):
    _lock_enterprise(db, enterprise_id)
    return _require_locked_member(db, enterprise_id, user_id)


def _validate(title: str | None, content: str) -> None:
    if not content.strip() or len(content) > 20000 or (title is not None and (not title.strip() or len(title) > 200)):
        raise ValidationError("标题或内容无效")


def _audit(db: Session, enterprise_id: int, user_id: int, action: str, object_id: int) -> None:
    db.add(EnterpriseAuditEvent(enterprise_id=enterprise_id, actor_id=user_id, action=action,
                                detail={"object_id": object_id}))


def _request_digest(*parts: str | int) -> str:
    body = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _replayed(db: Session, model, enterprise_id: int, user_id: int, request_key: str | None, digest: str):
    if request_key is None:
        return None
    if not request_key.strip() or len(request_key) > 80:
        raise ValidationError("客户端请求标识无效")
    # 企业行已锁定，锁定读同时避开 MySQL REPEATABLE READ 的旧快照。
    row = db.query(model).filter_by(enterprise_id=enterprise_id, author_id=user_id, request_key=request_key).\
        populate_existing().with_for_update().first()
    if row and (row.request_digest != digest or row.status != "active"):
        raise ConflictError("请求标识已用于其他内容或原内容已删除")
    return row


def posts(db: Session, enterprise_id: int, user_id: int, *, limit: int = 50, before_id: int | None = None):
    require_enterprise_member(db, enterprise_id, user_id)
    query = db.query(EnterpriseForumPost).filter_by(enterprise_id=enterprise_id, status="active")
    if before_id is not None:
        query = query.filter(EnterpriseForumPost.id < before_id)
    return query.order_by(EnterpriseForumPost.id.desc()).limit(limit).all()


def post_detail(db: Session, enterprise_id: int, user_id: int, post_id: int):
    require_enterprise_member(db, enterprise_id, user_id)
    row = db.query(EnterpriseForumPost).filter_by(id=post_id, enterprise_id=enterprise_id, status="active").first()
    if not row:
        raise NotFoundError("企业帖子不存在")
    return row


def create_post(db: Session, enterprise_id: int, user_id: int, title: str, content: str, *,
                request_key: str | None = None):
    _lock(db, enterprise_id, user_id)
    require_enterprise_member(db, enterprise_id, user_id)
    _validate(title, content)
    digest = _request_digest(title, content)
    existing = _replayed(db, EnterpriseForumPost, enterprise_id, user_id, request_key, digest)
    if existing:
        return existing
    row = EnterpriseForumPost(enterprise_id=enterprise_id, author_id=user_id, title=title, content=content,
                              request_key=request_key, request_digest=digest if request_key else None)
    db.add(row)
    db.flush()
    _audit(db, enterprise_id, user_id, "forum_post_created", row.id)
    db.commit()
    return row


def remove_post(db: Session, enterprise_id: int, user_id: int, post_id: int):
    membership = _lock(db, enterprise_id, user_id)
    post = post_detail(db, enterprise_id, user_id, post_id)
    if post.author_id != user_id and membership.role not in {"owner", "admin"}:
        raise ForbiddenError("仅作者或企业管理员可以删除")
    post.status = "deleted"
    _audit(db, enterprise_id, user_id, "forum_post_deleted", post.id)
    db.commit()


def update_post(db: Session, enterprise_id: int, user_id: int, post_id: int, title: str, content: str):
    membership = _lock(db, enterprise_id, user_id)
    post = post_detail(db, enterprise_id, user_id, post_id)
    if post.author_id != user_id and membership.role not in {"owner", "admin"}:
        raise ForbiddenError("仅作者或企业管理员可以编辑")
    _validate(title, content)
    post.title, post.content = title, content
    _audit(db, enterprise_id, user_id, "forum_post_updated", post.id)
    db.commit()
    return post


def replies(db: Session, enterprise_id: int, user_id: int, post_id: int, *, limit: int = 100, after_id: int = 0):
    post_detail(db, enterprise_id, user_id, post_id)
    return db.query(EnterpriseForumReply).filter_by(
        enterprise_id=enterprise_id, post_id=post_id, status="active"
    ).filter(EnterpriseForumReply.id > after_id).order_by(EnterpriseForumReply.id.asc()).limit(limit).all()


def create_reply(db: Session, enterprise_id: int, user_id: int, post_id: int, content: str, *,
                 request_key: str | None = None):
    _lock(db, enterprise_id, user_id)
    post_detail(db, enterprise_id, user_id, post_id)
    _validate(None, content)
    digest = _request_digest(post_id, content)
    existing = _replayed(db, EnterpriseForumReply, enterprise_id, user_id, request_key, digest)
    if existing:
        return existing
    row = EnterpriseForumReply(enterprise_id=enterprise_id, post_id=post_id, author_id=user_id, content=content,
                               request_key=request_key, request_digest=digest if request_key else None)
    db.add(row)
    db.flush()
    _audit(db, enterprise_id, user_id, "forum_reply_created", row.id)
    db.commit()
    return row


def remove_reply(db: Session, enterprise_id: int, user_id: int, post_id: int, reply_id: int):
    membership = _lock(db, enterprise_id, user_id)
    post_detail(db, enterprise_id, user_id, post_id)
    row = db.query(EnterpriseForumReply).filter_by(
        id=reply_id, enterprise_id=enterprise_id, post_id=post_id, status="active"
    ).first()
    if not row:
        raise NotFoundError("企业帖子回复不存在")
    if row.author_id != user_id and membership.role not in {"owner", "admin"}:
        raise ForbiddenError("仅回复作者或企业管理员可以删除")
    row.status = "deleted"
    _audit(db, enterprise_id, user_id, "forum_reply_deleted", row.id)
    db.commit()


def announcements(db: Session, enterprise_id: int, user_id: int, *, limit: int = 50, before_id: int | None = None):
    require_enterprise_member(db, enterprise_id, user_id)
    query = db.query(EnterpriseAnnouncement).filter_by(enterprise_id=enterprise_id, status="active")
    if before_id is not None:
        query = query.filter(EnterpriseAnnouncement.id < before_id)
    return query.order_by(EnterpriseAnnouncement.id.desc()).limit(limit).all()


def save_announcement(db: Session, enterprise_id: int, user_id: int, title: str, content: str, *,
                      announcement_id: int | None = None, request_key: str | None = None):
    membership = _lock(db, enterprise_id, user_id)
    if membership.role not in {"owner", "admin"}:
        raise ForbiddenError("需要企业管理权限")
    _validate(title, content)
    if announcement_id is None:
        digest = _request_digest(title, content)
        existing = _replayed(db, EnterpriseAnnouncement, enterprise_id, user_id, request_key, digest)
        if existing:
            return existing
        row = EnterpriseAnnouncement(enterprise_id=enterprise_id, author_id=user_id, title=title, content=content,
                                     request_key=request_key, request_digest=digest if request_key else None)
        db.add(row)
    else:
        row = db.query(EnterpriseAnnouncement).filter_by(
            id=announcement_id, enterprise_id=enterprise_id, status="active"
        ).first()
        if not row:
            raise NotFoundError("企业公告不存在")
        row.title, row.content = title, content
    db.flush()
    _audit(db, enterprise_id, user_id, "announcement_created" if announcement_id is None else "announcement_updated",
           row.id)
    db.commit()
    return row


def remove_announcement(db: Session, enterprise_id: int, user_id: int, announcement_id: int):
    membership = _lock(db, enterprise_id, user_id)
    if membership.role not in {"owner", "admin"}:
        raise ForbiddenError("需要企业管理权限")
    row = db.query(EnterpriseAnnouncement).filter_by(
        id=announcement_id, enterprise_id=enterprise_id, status="active"
    ).first()
    if not row:
        raise NotFoundError("企业公告不存在")
    row.status = "deleted"
    _audit(db, enterprise_id, user_id, "announcement_deleted", row.id)
    db.commit()
