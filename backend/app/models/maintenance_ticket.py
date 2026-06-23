"""
维修工单表 ORM 模型

用户提交的平台问题工单(报障/账号异常/功能报错等),
由管理员受理并按状态流转: pending → processing → resolved → closed。
"""
from sqlalchemy import BigInteger, Column, DateTime, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class MaintenanceTicket(Base, IdMixin, TimestampMixin):
    __tablename__ = "maintenance_ticket"

    user_id = Column(BigInteger, nullable=False, comment="提交人")
    title = Column(String(150), nullable=False, comment="工单标题")
    category = Column(
        String(20), nullable=False, default="bug",
        comment="bug/account/feature/performance/other",
    )
    description = Column(Text, nullable=False, comment="问题描述")
    priority = Column(String(10), nullable=False, default="medium", comment="low/medium/high")
    status = Column(
        String(20), nullable=False, default="pending",
        comment="pending/processing/resolved/closed",
    )
    admin_reply = Column(Text, comment="管理员处理回复")
    handled_by = Column(BigInteger, comment="处理管理员ID")
    handled_at = Column(DateTime, comment="处理时间(UTC)")
