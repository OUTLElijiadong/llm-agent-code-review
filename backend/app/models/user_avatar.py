"""用户自定义头像二进制表 ORM(与 user 一对一)

头像标识存 user.avatar('upload' 表示用这里的数据);本表只存图 bytes 与
真实嗅探出的 MIME,上传侧限制 ≤512KB 且仅 png/jpeg/webp/gif(按魔数判断,
拒绝 svg 等脚本载体)。下发走带鉴权的内联响应。
"""
from sqlalchemy import BigInteger, Column, LargeBinary, String

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class UserAvatar(Base, IdMixin, TimestampMixin):
    __tablename__ = "user_avatar"

    user_id = Column(BigInteger, nullable=False, unique=True, comment="用户ID(一对一)")
    mime = Column(String(32), nullable=False, comment="图片 MIME(服务端嗅探)")
    data = Column(LargeBinary, nullable=False, comment="头像图片二进制(≤512KB)")
