"""
用户画像 API 路由(本人维护;显式 + 隐式学习)
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.profile import ProfileOut, ProfileUpdateIn
from app.services import profile_service

router = APIRouter()


@router.get("/profile", response_model=Resp[ProfileOut])
def get_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取本人画像(不存在则初始化空画像)"""
    profile = profile_service.get_or_create(db, user.id)
    return Resp(data=ProfileOut(**profile_service.to_dict(profile)))


@router.put("/profile", response_model=Resp[ProfileOut])
def update_profile(payload: ProfileUpdateIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """更新本人显式画像"""
    profile = profile_service.update_profile(db, user.id, payload.model_dump(exclude_unset=True))
    return Resp(data=ProfileOut(**profile_service.to_dict(profile)))


@router.post("/profile/relearn", response_model=Resp[ProfileOut])
def relearn_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """触发隐式学习:从行为数据重新推断画像"""
    profile = profile_service.refresh_implicit(db, user.id, force=True)
    return Resp(data=ProfileOut(**profile_service.to_dict(profile)))
