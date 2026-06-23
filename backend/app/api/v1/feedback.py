"""
用户反馈 API 路由(向管理员)
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_admin
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.feedback import FeedbackIn, FeedbackOut, FeedbackReplyIn
from app.services import user_feedback_service

router = APIRouter()


@router.post("", response_model=Resp[dict])
def create_feedback(payload: FeedbackIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """提交反馈"""
    fb = user_feedback_service.create_feedback(db, user, payload.model_dump())
    return Resp(data={"id": fb.id})


@router.get("", response_model=Resp[PageOut[FeedbackOut]])
def list_feedback(
    status: str = Query(""),
    feedback_type: str = Query(""),
    scope: str = Query("mine", pattern="^(mine|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """反馈列表;scope=all 仅管理员看全部"""
    mine = scope == "mine"
    result = user_feedback_service.list_feedback(db, user, status, feedback_type, mine, page, page_size)
    return Resp(data=PageOut(**result))


@router.get("/stats", response_model=Resp[dict])
def feedback_stats(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """反馈状态统计(管理员)"""
    return Resp(data=user_feedback_service.stats_for_admin(db, admin))


@router.get("/{feedback_id}", response_model=Resp[FeedbackOut])
def get_feedback(feedback_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """反馈详情(本人或管理员)"""
    return Resp(data=FeedbackOut(**user_feedback_service.get_feedback(db, user, feedback_id)))


@router.put("/{feedback_id}/reply", response_model=Resp[FeedbackOut])
def reply_feedback(feedback_id: int, payload: FeedbackReplyIn,
                   db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """回复反馈(管理员)"""
    data = user_feedback_service.reply_feedback(db, admin, feedback_id, payload.model_dump(exclude_none=True))
    return Resp(data=FeedbackOut(**data))
