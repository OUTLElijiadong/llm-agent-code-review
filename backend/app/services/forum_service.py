"""
开发者论坛服务

权限:
- 浏览/发帖/回复: 任意登录用户
- 编辑/删除帖子或回复: 作者本人或管理员
- 置顶: 仅管理员
"""
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import Pagination
from app.models.forum_post import ForumPost
from app.models.forum_reply import ForumReply
from app.models.user import User

_CATEGORIES = ("qa", "tech", "share", "announce", "other")


def _author_names(db: Session, user_ids) -> dict:
    if not user_ids:
        return {}
    rows = db.query(User.id, User.nickname, User.username).filter(User.id.in_(set(user_ids))).all()
    return {uid: (nick or uname or f"用户{uid}") for uid, nick, uname in rows}


def list_posts(db: Session, keyword: str = "", category: str = "",
               page: int = 1, page_size: int = 20) -> dict:
    q = db.query(ForumPost).filter(ForumPost.status == "normal")
    if category:
        q = q.filter(ForumPost.category == category)
    if keyword:
        q = q.filter(ForumPost.title.contains(keyword))
    total = q.count()
    pg = Pagination(page, page_size, total)
    rows = (q.order_by(ForumPost.is_pinned.desc(), ForumPost.create_time.desc())
            .offset(pg.offset).limit(pg.page_size).all())
    names = _author_names(db, [p.user_id for p in rows])
    items = [_post_dict(p, names.get(p.user_id, "")) for p in rows]
    return pg.to_dict(items)


def get_post(db: Session, post_id: int, *, with_replies: bool = True) -> dict:
    post = db.get(ForumPost, post_id)
    if not post or post.status != "normal":
        raise NotFoundError("帖子不存在或已删除", code=40400)
    post.view_count = (post.view_count or 0) + 1
    db.commit()
    db.refresh(post)

    author_ids = [post.user_id]
    replies = []
    if with_replies:
        reply_rows = (db.query(ForumReply)
                      .filter(ForumReply.post_id == post_id, ForumReply.status == "normal")
                      .order_by(ForumReply.create_time.asc()).all())
        author_ids.extend(r.user_id for r in reply_rows)
        names = _author_names(db, author_ids)
        replies = [_reply_dict(r, names.get(r.user_id, "")) for r in reply_rows]
    else:
        names = _author_names(db, author_ids)

    data = _post_dict(post, names.get(post.user_id, ""))
    data["content"] = post.content
    data["replies"] = replies
    return data


def create_post(db: Session, user: User, payload: dict) -> ForumPost:
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    if not title or not content:
        raise ValidationError("标题和内容不能为空", code=42201)
    category = payload.get("category") if payload.get("category") in _CATEGORIES else "qa"
    post = ForumPost(user_id=user.id, title=title[:200], content=content, category=category)
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def update_post(db: Session, user: User, post_id: int, payload: dict) -> dict:
    post = db.get(ForumPost, post_id)
    if not post or post.status != "normal":
        raise NotFoundError("帖子不存在", code=40400)
    if post.user_id != user.id and user.role != "admin":
        raise ForbiddenError("只能编辑自己的帖子", code=40300)
    if payload.get("title"):
        post.title = payload["title"].strip()[:200]
    if payload.get("content"):
        post.content = payload["content"].strip()
    if payload.get("category") in _CATEGORIES:
        post.category = payload["category"]
    db.commit()
    db.refresh(post)
    return get_post(db, post_id)


def delete_post(db: Session, user: User, post_id: int) -> None:
    post = db.get(ForumPost, post_id)
    if not post or post.status != "normal":
        raise NotFoundError("帖子不存在", code=40400)
    if post.user_id != user.id and user.role != "admin":
        raise ForbiddenError("只能删除自己的帖子", code=40300)
    post.status = "deleted"
    db.commit()


def pin_post(db: Session, admin: User, post_id: int, pinned: bool) -> dict:
    if admin.role != "admin":
        raise ForbiddenError("需要管理员权限", code=40300)
    post = db.get(ForumPost, post_id)
    if not post or post.status != "normal":
        raise NotFoundError("帖子不存在", code=40400)
    post.is_pinned = bool(pinned)
    db.commit()
    return get_post(db, post_id, with_replies=False)


def create_reply(db: Session, user: User, post_id: int, content: str) -> ForumReply:
    content = (content or "").strip()
    if not content:
        raise ValidationError("回复内容不能为空", code=42201)
    post = db.get(ForumPost, post_id)
    if not post or post.status != "normal":
        raise NotFoundError("帖子不存在", code=40400)
    reply = ForumReply(post_id=post_id, user_id=user.id, content=content)
    db.add(reply)
    post.reply_count = (post.reply_count or 0) + 1
    db.commit()
    db.refresh(reply)
    return reply


def delete_reply(db: Session, user: User, reply_id: int) -> None:
    reply = db.get(ForumReply, reply_id)
    if not reply or reply.status != "normal":
        raise NotFoundError("回复不存在", code=40400)
    if reply.user_id != user.id and user.role != "admin":
        raise ForbiddenError("只能删除自己的回复", code=40300)
    reply.status = "deleted"
    post = db.get(ForumPost, reply.post_id)
    if post and (post.reply_count or 0) > 0:
        post.reply_count -= 1
    db.commit()


def _post_dict(p: ForumPost, author_name: str) -> dict:
    return {
        "id": p.id, "user_id": p.user_id, "author_name": author_name,
        "category": p.category, "title": p.title,
        "view_count": p.view_count, "reply_count": p.reply_count,
        "is_pinned": bool(p.is_pinned), "create_time": p.create_time,
        "update_time": p.update_time,
    }


def _reply_dict(r: ForumReply, author_name: str) -> dict:
    return {
        "id": r.id, "post_id": r.post_id, "user_id": r.user_id,
        "author_name": author_name, "content": r.content,
        "create_time": r.create_time,
    }
