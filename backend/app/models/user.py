"""
用户表ORM模型
"""
from sqlalchemy import Column, DateTime, Index, Integer, SmallInteger, String

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "user"
    __table_args__ = (
        Index("ix_user_status", "status"),
    )

    username = Column(String(50), unique=True, nullable=False)
    password = Column(String(255), nullable=False)
    email = Column(String(100))
    nickname = Column(String(50))
    role = Column(String(20), nullable=False, default="user")
    status = Column(SmallInteger, nullable=False, default=1, comment="1=启用,0=禁用")
    last_login = Column(DateTime)
    token_version = Column(
        Integer, nullable=False, default=0, server_default="0",
        comment="令牌版本: 改密/禁用/重置时递增,使此前签发的JWT失效",
    )
