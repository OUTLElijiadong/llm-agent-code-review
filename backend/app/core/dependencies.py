"""
FastAPI依赖注入模块: 用户认证与权限校验
"""

from typing import Optional

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import AuthError, ForbiddenError
from app.core.security import decode_token
from app.models.user import User


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> User:
    """从请求头解析JWT令牌并返回当前登录用户

    Args:
        authorization: Authorization请求头,格式为'Bearer <token>'
        db: 数据库会话

    Returns:
        User: 当前登录用户ORM对象

    Raises:
        AuthError: token缺失/非法/过期
        ForbiddenError: 用户不存在或已禁用
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("缺少token", code=40100)
    token = authorization[7:]
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        # 签名有效但缺少/非法 sub 声明
        raise AuthError("token非法或已过期", code=40101)
    except Exception:
        raise AuthError("token非法或已过期", code=40101)

    user = db.get(User, user_id)
    if not user or user.status != 1:
        raise ForbiddenError("账号不存在或已禁用", code=40301)
    # 令牌版本校验: 改密/禁用/重置后旧令牌的 ver 与数据库不一致 → 失效
    if int(payload.get("ver", 0)) != (user.token_version or 0):
        raise AuthError("登录态已失效,请重新登录", code=40102)
    return user


def require_admin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """校验当前用户是否为管理员角色

    Args:
        user: 当前登录用户

    Returns:
        User: 管理员用户对象

    Raises:
        ForbiddenError: 非管理员用户
    """
    from app.services.rbac_service import is_admin_user

    if user.role not in {"admin", "super_admin"} and not is_admin_user(db, user.id):
        raise ForbiddenError("需要管理员权限", code=40300)
    return user


def get_optional_user(
    authorization: str = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """可选认证: 有token则解析,无token则返回None"""
    if not authorization:
        return None
    return get_current_user(authorization=authorization, db=db)
