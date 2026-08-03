"""
鉴权服务模块: 注册、登录、密码修改
"""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.exceptions import AuthError, ConflictError, ForbiddenError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import RegisterIn
from app.utils.sanitize import sanitize_text

_LOGIN_CAS_MAX_ATTEMPTS = 8


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
    try:
        for _attempt in range(_LOGIN_CAS_MAX_ATTEMPTS):
            user = db.query(User).populate_existing().filter(User.username == username).first()
            if not user or not verify_password(password, user.password):
                raise AuthError("用户名或密码错误", code=40001)
            if user.status == -1:
                raise ForbiddenError("账号已被删除", code=40302)
            if user.status != 1:
                raise ForbiddenError("账号已禁用", code=40301)

            user_id = int(user.id)
            expected_version = int(user.token_version or 0)
            stored_password = str(user.password)
            role = str(user.role)
            login_at = datetime.now(timezone.utc)

            # SQLite 的 FOR UPDATE 不生效，而且先 SELECT 后升级写锁会引入
            # SQLITE_BUSY 死锁窗口。先结束只读事务，再由单条 UPDATE 做 CAS。
            db.rollback()
            values = {
                "token_version": expected_version + 1,
                "last_login": login_at,
            }
            if ip:
                values["last_login_ip"] = ip
            result = db.execute(
                update(User)
                .where(
                    User.id == user_id,
                    User.token_version == expected_version,
                    User.password == stored_password,
                    User.status == 1,
                    User.role == role,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.rollback()
                continue

            next_version = expected_version + 1
            token = create_access_token(user_id, role, next_version)
            updated_user = db.query(User).populate_existing().filter(User.id == user_id).one()
            db.commit()
            return token, updated_user
        raise ConflictError("并发登录请求过多，请重试", code=40902)
    except Exception:
        db.rollback()
        raise


def logout(db: Session, user: User) -> None:
    """递增会话版本，使当前访问令牌及同版本令牌立即失效。"""
    expected_version = getattr(user, "token_version", 0) or 0
    try:
        locked_user = db.query(User).populate_existing().filter(User.id == user.id).with_for_update().one()
        if (locked_user.token_version or 0) != expected_version:
            raise AuthError("登录状态已失效，请重新登录", code=40102)
        locked_user.token_version = (getattr(locked_user, "token_version", 0) or 0) + 1
        db.commit()
    except Exception:
        db.rollback()
        raise


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
    expected_version = getattr(user, "token_version", 0) or 0
    try:
        locked_user = db.query(User).populate_existing().filter(User.id == user.id).with_for_update().one()
        if (locked_user.token_version or 0) != expected_version:
            raise AuthError("登录状态已失效，请重新登录", code=40102)
        # 必须在行锁内重新校验，避免并发改密或重置后仍使用旧哈希。
        if not verify_password(old_password, locked_user.password):
            raise AuthError("旧密码错误", code=40001)
        locked_user.password = hash_password(new_password)
        locked_user.token_version = (locked_user.token_version or 0) + 1
        db.commit()
    except Exception:
        db.rollback()
        raise
