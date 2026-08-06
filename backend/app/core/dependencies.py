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


def authenticate_access_token(token: str, db: Session) -> User:
    """校验访问令牌、账号状态和会话版本并返回用户。"""
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])
        token_version = int(payload.get("ver", 0))
    except (KeyError, ValueError, TypeError):
        raise AuthError("token非法或已过期", code=40101)
    except Exception:
        raise AuthError("token非法或已过期", code=40101)

    user = db.get(User, user_id)
    if not user or user.status != 1:
        raise ForbiddenError("账号不存在或已禁用", code=40301)
    if token_version != (user.token_version or 0):
        raise AuthError("账号已在另一台设备登录，当前设备已下线", code=40102)
    return user


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
    return authenticate_access_token(authorization[7:], db)


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


def require_super_admin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """仅允许唯一 ``admin`` 超级管理员。"""

    from app.services.rbac_service import is_super_admin_user

    if not is_super_admin_user(db, user.id):
        raise ForbiddenError("仅超级管理员 admin 可执行此操作", code=40322)
    return user


def get_optional_user(
    authorization: str = Header(None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """可选认证: 有token则解析,无token则返回None"""
    if not authorization:
        return None
    return get_current_user(authorization=authorization, db=db)
