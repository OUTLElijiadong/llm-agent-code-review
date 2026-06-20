"""
安全模块: bcrypt密码哈希与JWT令牌管理
"""
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """对明文密码进行bcrypt哈希加密

    Args:
        plain: 明文密码

    Returns:
        str: bcrypt哈希后的密码字符串
    """
    return pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码与哈希密码是否匹配

    Args:
        plain: 明文密码
        hashed: bcrypt哈希密码

    Returns:
        bool: 匹配则True,否则False
    """
    return pwd_ctx.verify(plain, hashed)


def create_access_token(user_id: int, role: str) -> str:
    """生成JWT访问令牌

    Args:
        user_id: 用户ID
        role: 用户角色

    Returns:
        str: JWT令牌字符串
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_expire_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """解码并验证JWT令牌

    Args:
        token: JWT令牌字符串

    Returns:
        dict: 解码后的载荷字典(含sub, role, iat, exp)

    Raises:
        jwt.ExpiredSignatureError: 令牌已过期
        jwt.InvalidTokenError: 令牌非法(含缺少 exp/sub 必填声明)
    """
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub"]},
    )
