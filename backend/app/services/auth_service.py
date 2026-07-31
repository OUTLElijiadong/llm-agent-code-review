"""
鉴权服务模块: 注册、登录、密码修改
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import AuthError, ConflictError, ForbiddenError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import RegisterIn
from app.utils.sanitize import sanitize_text


def register(db: Session, payload: RegisterIn) -> User:
    """用户注册：原子创建用户、分配默认角色并消费内测码。

    Args:
        db: 数据库会话
        payload: 注册请求体(用户名/密码/邮箱/昵称)

    Returns:
        User: 新创建的用户ORM对象

    Raises:
        ConflictError: 用户名已存在
        ValidationError: 内测码不可用
    """
    from app.core.config import settings
    from app.models.rbac import Role
    from app.services import beta_invite_service, rbac_service

    invite = None
    try:
        if settings.beta_registration_enabled:
            invite = beta_invite_service.lock_valid_code(db, payload.beta_code or "")

        exists = db.query(User.id).filter(User.username == payload.username).first()
        if exists:
            raise ConflictError("用户名已存在", code=40901)

        user = User(
            username=payload.username,
            password=hash_password(payload.password),
            email=payload.email,
            # 昵称剥纯文本,防存储型 XSS
            nickname=sanitize_text(payload.nickname) or payload.username,
            role="user",
            status=1,
        )
        db.add(user)
        db.flush()

        # 与用户创建处于同一事务，避免邀请码已消费但角色分配失败。
        default_role = db.query(Role).filter(Role.code == "user", Role.status == "active").first()
        if default_role:
            rbac_service.assign_roles_to_user(db, user.id, [default_role.id], commit=False)

        if invite is not None:
            beta_invite_service.consume_locked_code(db, invite, user_id=user.id)

        db.commit()
        db.refresh(user)
        return user
    except Exception:
        db.rollback()
        raise


def login(db: Session, username: str, password: str, ip: str = ""):
    """用户登录: 验证密码,更新最后登录时间与来源IP,签发JWT

    Args:
        db: 数据库会话
        username: 用户名
        password: 明文密码
        ip: 登录来源 IP(用于审计与用户管理展示)

    Returns:
        tuple[str, User]: (JWT令牌, 用户ORM对象)

    Raises:
        AuthError: 用户名或密码错误
        ForbiddenError: 账号已禁用或已删除
    """
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password):
        raise AuthError("用户名或密码错误", code=40001)
    if user.status == -1:
        raise ForbiddenError("账号已被删除", code=40302)
    if user.status != 1:
        raise ForbiddenError("账号已禁用", code=40301)
    user.last_login = datetime.now(timezone.utc)
    if ip:
        user.last_login_ip = ip
    db.commit()
    token = create_access_token(user.id, user.role, getattr(user, "token_version", 0) or 0)
    return token, user


def change_password(db: Session, user: User, old_password: str, new_password: str) -> None:
    """修改密码: 验证旧密码后更新

    Args:
        db: 数据库会话
        user: 当前登录用户
        old_password: 旧密码
        new_password: 新密码

    Raises:
        AuthError: 旧密码错误
    """
    if not verify_password(old_password, user.password):
        raise AuthError("旧密码错误", code=40001)
    user.password = hash_password(new_password)
    # 递增 token 版本 → 改密后此前签发的所有 JWT 立即失效
    user.token_version = (getattr(user, "token_version", 0) or 0) + 1
    db.commit()
