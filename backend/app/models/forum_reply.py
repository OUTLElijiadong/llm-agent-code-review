"""
开发者论坛 — 回复 ORM 模型
"""
from sqlalchemy import BigInteger, Column, Index, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ForumReply(Base, IdMixin, TimestampMixin):
    __tablename__ = "forum_reply"
    __table_args__ = (
        # 取某帖的全部正常回复(对应 init.sql idx_post)
        Index("ix_forum_reply_post", "post_id", "status"),
    )

    post_id = Column(BigInteger, nullable=False, comment="所属主题帖")
    user_id = Column(BigInteger, nullable=False, comment="回复人")
    content = Column(Text, nullable=False, comment="回复内容")
    status = Column(String(20), nullable=False, default="normal", comment="normal/deleted")
