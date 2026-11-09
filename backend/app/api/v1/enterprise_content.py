"""企业论坛与公告 API。"""
from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import enterprise_content_service as service

router = APIRouter(prefix="/enterprises/{enterprise_id}", tags=["enterprise-content"])


class ContentIn(StrictInputModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    request_key: str | None = Field(default=None, min_length=1, max_length=80)


class ReplyIn(StrictInputModel):
    content: str = Field(min_length=1, max_length=20000)
    request_key: str | None = Field(default=None, min_length=1, max_length=80)


def _content(row):
    result = {"id": row.id, "enterprise_id": row.enterprise_id, "author_id": row.author_id,
              "content": row.content, "create_time": row.create_time, "update_time": row.update_time}
    if hasattr(row, "title"):
        result["title"] = row.title
    if hasattr(row, "post_id"):
        result["post_id"] = row.post_id
    return result


@router.get("/forum/posts", response_model=Resp[dict])
def list_posts(enterprise_id: int, limit: int = Query(50, ge=1, le=100), before_id: int | None = Query(None, ge=1),
               db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data={"items": [_content(row) for row in service.posts(
        db, enterprise_id, user.id, limit=limit, before_id=before_id)]})


@router.post("/forum/posts", response_model=Resp[dict])
def create_post(enterprise_id: int, payload: ContentIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    return Resp(data=_content(service.create_post(
        db, enterprise_id, user.id, payload.title, payload.content, request_key=payload.request_key)))


@router.get("/forum/posts/{post_id}", response_model=Resp[dict])
def post_detail(enterprise_id: int, post_id: int, after_reply_id: int = Query(0, ge=0),
                db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    row = service.post_detail(db, enterprise_id, user.id, post_id)
    return Resp(data={"post": _content(row), "replies": [
        _content(reply) for reply in service.replies(db, enterprise_id, user.id, post_id, after_id=after_reply_id)
    ]})


@router.delete("/forum/posts/{post_id}", response_model=Resp[None])
def delete_post(enterprise_id: int, post_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    service.remove_post(db, enterprise_id, user.id, post_id)
    return Resp(data=None)


@router.put("/forum/posts/{post_id}", response_model=Resp[dict])
def update_post(enterprise_id: int, post_id: int, payload: ContentIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    return Resp(data=_content(service.update_post(db, enterprise_id, user.id, post_id, payload.title, payload.content)))


@router.post("/forum/posts/{post_id}/replies", response_model=Resp[dict])
def create_reply(enterprise_id: int, post_id: int, payload: ReplyIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    return Resp(data=_content(service.create_reply(
        db, enterprise_id, user.id, post_id, payload.content, request_key=payload.request_key)))


@router.delete("/forum/posts/{post_id}/replies/{reply_id}", response_model=Resp[None])
def delete_reply(enterprise_id: int, post_id: int, reply_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    service.remove_reply(db, enterprise_id, user.id, post_id, reply_id)
    return Resp(data=None)


@router.get("/announcements", response_model=Resp[dict])
def list_announcements(enterprise_id: int, limit: int = Query(50, ge=1, le=100),
                       before_id: int | None = Query(None, ge=1),
                       db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = service.announcements(db, enterprise_id, user.id, limit=limit, before_id=before_id)
    return Resp(data={"items": [_content(row) for row in rows]})


@router.post("/announcements", response_model=Resp[dict])
def create_announcement(enterprise_id: int, payload: ContentIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    return Resp(data=_content(service.save_announcement(
        db, enterprise_id, user.id, payload.title, payload.content, request_key=payload.request_key)))


@router.put("/announcements/{announcement_id}", response_model=Resp[dict])
def update_announcement(enterprise_id: int, announcement_id: int, payload: ContentIn,
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_content(service.save_announcement(
        db, enterprise_id, user.id, payload.title, payload.content, announcement_id=announcement_id)))


@router.delete("/announcements/{announcement_id}", response_model=Resp[None])
def delete_announcement(enterprise_id: int, announcement_id: int, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    service.remove_announcement(db, enterprise_id, user.id, announcement_id)
    return Resp(data=None)
