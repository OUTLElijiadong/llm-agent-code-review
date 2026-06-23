"""
开发者论坛 API 路由
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.forum import (
    AssistIn,
    PinIn,
    PostDetailOut,
    PostIn,
    PostListItemOut,
    PostUpdateIn,
    ReplyIn,
)
from app.services import forum_service, personalization_service

router = APIRouter()


@router.get("/posts", response_model=Resp[PageOut[PostListItemOut]])
def list_posts(
    keyword: str = Query(""),
    category: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """帖子列表(全员可浏览)"""
    result = forum_service.list_posts(db, keyword, category, page, page_size)
    return Resp(data=PageOut(**result))


@router.post("/posts", response_model=Resp[dict])
def create_post(payload: PostIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """发帖(任意登录用户)"""
    post = forum_service.create_post(db, user, payload.model_dump())
    return Resp(data={"id": post.id})


@router.get("/posts/{post_id}", response_model=Resp[PostDetailOut])
def get_post(post_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """帖子详情 + 回复"""
    return Resp(data=PostDetailOut(**forum_service.get_post(db, post_id)))


@router.put("/posts/{post_id}", response_model=Resp[PostDetailOut])
def update_post(post_id: int, payload: PostUpdateIn, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """编辑帖子(作者或管理员)"""
    data = forum_service.update_post(db, user, post_id, payload.model_dump(exclude_none=True))
    return Resp(data=PostDetailOut(**data))


@router.delete("/posts/{post_id}", response_model=Resp[None])
def delete_post(post_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """删除帖子(作者或管理员)"""
    forum_service.delete_post(db, user, post_id)
    return Resp(data=None)


@router.put("/posts/{post_id}/pin", response_model=Resp[PostListItemOut])
def pin_post(post_id: int, payload: PinIn, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """置顶/取消置顶(管理员)"""
    data = forum_service.pin_post(db, user, post_id, payload.pinned)
    return Resp(data=PostListItemOut(**data))


@router.post("/posts/{post_id}/replies", response_model=Resp[dict])
def create_reply(post_id: int, payload: ReplyIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """回复帖子"""
    reply = forum_service.create_reply(db, user, post_id, payload.content)
    return Resp(data={"id": reply.id})


@router.delete("/replies/{reply_id}", response_model=Resp[None])
def delete_reply(reply_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """删除回复(作者或管理员)"""
    forum_service.delete_reply(db, user, reply_id)
    return Resp(data=None)


@router.post("/assist", response_model=Resp[dict])
def assist_draft(payload: AssistIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """发帖助手:基于个人知识库 + 画像给个性化建议(RAG)"""
    data = personalization_service.assist_forum_draft(db, user.id, payload.title, payload.draft)
    return Resp(data=data)
