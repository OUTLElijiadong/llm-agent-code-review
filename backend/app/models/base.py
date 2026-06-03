"""
SQLAlchemy ORM公共基类和Mixin
"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, Integer


def _utcnow():
    return datetime.now(timezone.utc)


class IdMixin:
    """整数自增主键Mixin"""
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)


class TimestampMixin:
    """创建时间/更新时间自动维护Mixin"""
    create_time = Column(DateTime, default=_utcnow, nullable=False)
    update_time = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)
