"""企业论坛和公告独立于个人空间的公共论坛。"""
from sqlalchemy import BigInteger, Column, Index, String, Text, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EnterpriseForumPost(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_forum_post"
    __table_args__ = (Index("ix_enterprise_forum_post_org", "enterprise_id", "status", "id"),
                      UniqueConstraint("enterprise_id", "author_id", "request_key", name="uq_forum_post_request"))

    enterprise_id = Column(BigInteger, nullable=False)
    author_id = Column(BigInteger, nullable=False)
    request_key = Column(String(80), nullable=True)
    request_digest = Column(String(64), nullable=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="active")


class EnterpriseForumReply(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_forum_reply"
    __table_args__ = (Index("ix_enterprise_forum_reply_post", "enterprise_id", "post_id", "status", "id"),
                      UniqueConstraint("enterprise_id", "author_id", "request_key", name="uq_forum_reply_request"))

    enterprise_id = Column(BigInteger, nullable=False)
    post_id = Column(BigInteger, nullable=False)
    author_id = Column(BigInteger, nullable=False)
    request_key = Column(String(80), nullable=True)
    request_digest = Column(String(64), nullable=True)
    content = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="active")


class EnterpriseAnnouncement(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_announcement"
    __table_args__ = (Index("ix_enterprise_announcement_org", "enterprise_id", "status", "id"),
                      UniqueConstraint("enterprise_id", "author_id", "request_key", name="uq_announcement_request"))

    enterprise_id = Column(BigInteger, nullable=False)
    author_id = Column(BigInteger, nullable=False)
    request_key = Column(String(80), nullable=True)
    request_digest = Column(String(64), nullable=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="active")
