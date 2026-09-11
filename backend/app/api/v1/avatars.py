"""用户头像 API(自服务)

- 内置头像: user.avatar 存 'builtin:<key>',前端本地渲染,不走网络。
- 自定义头像: user.avatar 存 'upload',二进制落 user_avatar 表(≤512KB,
  按魔数仅允许 png/jpeg/webp/gif,拒绝 svg 脚本载体),GET 带鉴权内联下发。
头像编辑不递增 token_version(不会把用户踢下线)。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import BadRequestError, NotFoundError
from app.models.user import User
from app.models.user_avatar import UserAvatar
from app.schemas.common import Resp

router = APIRouter()

MAX_AVATAR_BYTES = 512 * 1024
MAX_AVATAR_DIMENSION = 4096  # 上传侧粗检,前端展示统一缩放


class AvatarSetIn(BaseModel):
    """设置头像:内置 key(如 cat)或 'upload'"""
    avatar: str = Field(min_length=1, max_length=64)


def _sniff_image_mime(head: bytes) -> str | None:
    """按魔数识别允许的图片类型;不信任客户端声明的 Content-Type。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def set_avatar_key(db: Session, user: User, value: str) -> User:
    """写入头像标识并提交(不递增 token_version)。"""
    user.avatar = value
    db.commit()
    db.refresh(user)
    return user


@router.put("/me/avatar", response_model=Resp[dict])
def set_avatar(payload: AvatarSetIn, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """设置头像:内置('builtin:<key>' 或裸 key)或 'upload'(须已上传图片)。"""
    value = payload.avatar.strip()
    if value == "upload":
        if not db.query(UserAvatar.id).filter(UserAvatar.user_id == user.id).first():
            raise BadRequestError("请先上传头像图片", code=40001)
    elif not value.startswith("builtin:"):
        value = f"builtin:{value}"
    key = value.split(":", 1)[1] if ":" in value else value
    if len(key) > 56 or not key.replace("-", "").replace("_", "").isalnum():
        raise BadRequestError("无效的头像标识", code=40001)
    set_avatar_key(db, user, value)
    return Resp(data={"avatar": value}, message="头像已更新")


@router.post("/me/avatar/image", response_model=Resp[dict])
async def upload_avatar_image(file: UploadFile, db: Session = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """上传自定义头像图片(≤512KB,png/jpeg/webp/gif 按魔数校验)。"""
    raw = await file.read()
    if len(raw) > MAX_AVATAR_BYTES:
        raise BadRequestError("头像图片不能超过 512KB", code=40001)
    mime = _sniff_image_mime(raw[:16])
    if mime is None:
        raise BadRequestError("仅支持 PNG / JPEG / WebP / GIF 图片", code=40001)

    row = db.query(UserAvatar).filter(UserAvatar.user_id == user.id).first()
    if row:
        row.mime = mime
        row.data = raw
    else:
        row = UserAvatar(user_id=user.id, mime=mime, data=raw)
        db.add(row)
    user.avatar = "upload"
    db.commit()
    return Resp(data={"avatar": "upload", "mime": mime}, message="头像已上传")


@router.delete("/me/avatar", response_model=Resp[dict])
def clear_avatar(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """恢复默认头像(同时清掉自定义图片)。"""
    db.query(UserAvatar).filter(UserAvatar.user_id == user.id).delete()
    user.avatar = None
    db.commit()
    return Resp(data={"avatar": None}, message="已恢复默认头像")


@router.get("/users/{user_id}/avatar/image")
def get_avatar_image(user_id: int, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """下发用户自定义头像(内联,带鉴权);内置/默认头像由前端本地渲染。"""
    from fastapi.responses import Response as FastResponse

    row = db.query(UserAvatar).filter(UserAvatar.user_id == user_id).first()
    if not row:
        raise NotFoundError("该用户未上传头像", code=40400)
    return FastResponse(
        content=row.data,
        media_type=row.mime,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )
