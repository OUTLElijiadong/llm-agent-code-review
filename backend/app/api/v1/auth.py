"""
鉴权API路由: 注册、登录、获取当前用户、修改密码、退出登录
"""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.captcha import create_captcha, verify_captcha
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AuthError, ForbiddenError, TooManyRequestsError, ValidationError
from app.core.rate_limit import client_ip, limiter, login_failure_limiter
from app.models.user import User
from app.schemas.auth import ChangePasswordIn, LoginIn, LoginOut, RegisterIn, UserOut
from app.schemas.common import Resp
from app.services import audit_service, auth_service

router = APIRouter()


def _client_ip(request: Request) -> str:
    """只信任配置网段内反向代理覆盖写入的真实来源。"""
    return client_ip(request)


@router.get("/captcha", response_model=Resp[dict])
@limiter.limit("30/minute")
def get_captcha(request: Request, response: Response):
    """获取注册验证码(数学题)。返回 captcha_id 与题目,不返回答案。"""
    data = create_captcha()
    data["beta_registration_enabled"] = settings.beta_registration_enabled
    return Resp(data=data)


@router.post("/register", response_model=Resp[dict])
@limiter.limit("10/minute")
def register(payload: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """用户注册(生产环境需先通过一次性验证码)"""
    if settings.register_captcha_enabled:
        if not payload.captcha_id or not verify_captcha(payload.captcha_id, payload.captcha_answer or ""):
            raise ValidationError("验证码错误或已过期,请刷新后重试", code=42210)
    user = auth_service.register(db, payload)
    audit_service.log(
        db,
        user,
        "user",
        target_type="user",
        target_id=user.id,
        detail=f"新用户注册: {user.username}",
        ip=_client_ip(request),
    )
    return Resp(data={"user_id": user.id, "username": user.username})


@router.post("/login", response_model=Resp[LoginOut])
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    """用户登录"""
    ip = _client_ip(request)
    attempt = login_failure_limiter.begin_attempt(ip)
    if not attempt.allowed:
        raise TooManyRequestsError(retry_after=attempt.retry_after)
    try:
        token, user = auth_service.login(db, payload.username, payload.password, ip=ip)
    except (AuthError, ForbiddenError) as exc:
        login_failure_limiter.finish_attempt(ip, attempt.reservation_id, success=False)
        audit_service.log(
            db,
            None,
            "login",
            target_type="user",
            target_id=payload.username,
            detail=f"登录失败: {exc}",
            status="failed",
            ip=ip,
        )
        raise
    except Exception as exc:
        login_failure_limiter.release_attempt(ip, attempt.reservation_id)
        audit_service.log(
            db,
            None,
            "login",
            target_type="user",
            target_id=payload.username,
            detail=f"登录失败: {exc}",
            status="failed",
            ip=ip,
        )
        raise
    login_failure_limiter.finish_attempt(ip, attempt.reservation_id, success=True)
    audit_service.log(
        db,
        user,
        "login",
        target_type="user",
        target_id=user.id,
        detail=f"用户 {user.username} 登录成功",
        ip=ip,
    )
    return Resp(
        data=LoginOut(
            access_token=token,
            expires_in=settings.jwt_expire_seconds,
            user=UserOut.model_validate(user),
        )
    )


@router.get("/me", response_model=Resp[UserOut])
def me(user: User = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return Resp(data=UserOut.model_validate(user))


@router.post("/change-password", response_model=Resp[None])
def change_password(
    payload: ChangePasswordIn, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """修改密码"""
    auth_service.change_password(db, user, payload.old_password, payload.new_password)
    audit_service.log(
        db,
        user,
        "user",
        target_type="user",
        target_id=user.id,
        detail="修改密码",
        ip=_client_ip(request),
    )
    return Resp(data=None)


@router.post("/logout", response_model=Resp[None])
def logout(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """退出登录并使当前账号已签发的会话令牌失效。"""
    auth_service.logout(db, user)
    audit_service.log(
        db,
        user,
        "logout",
        target_type="user",
        target_id=user.id,
        detail=f"用户 {user.username} 退出登录",
        ip=_client_ip(request),
    )
    return Resp(data=None)
