"""
开发者论坛 — 主题帖 ORM 模型

全员(登录用户)可发帖;管理员可置顶/删除。
"""
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ForumPost(Base, IdMixin, TimestampMixin):
    __tablename__ = "forum_post"

    user_id = Column(BigInteger, nullable=False, comment="作者")
    category = Column(
        String(20), nullable=False, default="qa",
        comment="qa/tech/share/announce/other",
    )
    title = Column(String(200), nullable=False, comment="标题")
    content = Column(Text, nullable=False, comment="正文(Markdown)")
    view_count = Column(Integer, nullable=False, default=0, comment="浏览数")
    reply_count = Column(Integer, nullable=False, default=0, comment="回复数")
    is_pinned = Column(Boolean, nullable=False, default=False, comment="是否置顶(管理员)")
    status = Column(String(20), nullable=False, default="normal", comment="normal/deleted")
