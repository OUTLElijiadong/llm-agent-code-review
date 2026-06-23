"""
开发者论坛 — 回复 ORM 模型
"""
from sqlalchemy import BigInteger, Column, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ForumReply(Base, IdMixin, TimestampMixin):
    __tablename__ = "forum_reply"

    post_id = Column(BigInteger, nullable=False, comment="所属主题帖")
    user_id = Column(BigInteger, nullable=False, comment="回复人")
    content = Column(Text, nullable=False, comment="回复内容")
    status = Column(String(20), nullable=False, default="normal", comment="normal/deleted")
