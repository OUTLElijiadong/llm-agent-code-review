"""
用户管理API路由(管理员)
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.user import PasswordResetOut, RoleIn, StatusIn, UserListItem
from app.services import audit_service, user_service

router = APIRouter()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("", response_model=Resp[PageOut[UserListItem]])
def list_users(
    keyword: str = Query(""),
    role: str = Query(""),
    status: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """用户列表(管理员)"""
    result = user_service.list_users(db, keyword, role, status, page, page_size)
    return Resp(data=PageOut(**result))


@router.post("/{user_id}/reset-password", response_model=Resp[PasswordResetOut])
def reset_password(user_id: int, request: Request, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    """重置密码(管理员)"""
    data = user_service.reset_password(db, user_id, admin)
    audit_service.log(
        db, admin, "user",
        target_type="user", target_id=user_id,
        detail=f"管理员重置用户 {user_id} 密码",
        ip=_client_ip(request),
    )
    return Resp(data=data)


@router.post("/{user_id}/toggle-status", response_model=Resp[None])
def toggle_status(user_id: int, payload: StatusIn, request: Request,
                  db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    """启用/禁用用户(管理员)"""
    user_service.toggle_status(db, user_id, payload.status, admin)
    audit_service.log(
        db, admin, "user",
        target_type="user", target_id=user_id,
        detail=f"修改用户 {user_id} 状态为 {payload.status}",
        ip=_client_ip(request),
    )
    return Resp(data=None)


@router.post("/{user_id}/role", response_model=Resp[None])
def set_role(user_id: int, payload: RoleIn, request: Request,
             db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    """设置用户角色(管理员)"""
    user_service.set_role(db, user_id, payload.role, admin.id)
    audit_service.log(
        db, admin, "user",
        target_type="user", target_id=user_id,
        detail=f"修改用户 {user_id} 角色为 {payload.role}",
        ip=_client_ip(request),
    )
    return Resp(data=None)


@router.delete("/{user_id}", response_model=Resp[None])
def delete_user(user_id: int, request: Request,
                db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    """软删除用户(管理员)。

    软删保留项目与历史数据,仅禁止登录;不能删除自己或最后一个可用管理员。
    """
    user_service.delete_user(db, user_id, admin.id)
    audit_service.log(
        db, admin, "user",
        target_type="user", target_id=user_id,
        detail=f"管理员软删除用户 {user_id}",
        ip=_client_ip(request),
    )
    return Resp(data=None)
