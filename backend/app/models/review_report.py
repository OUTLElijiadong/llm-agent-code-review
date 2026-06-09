"""
审查报告表ORM模型(可选,用于保存历史快照)
"""
from sqlalchemy import JSON, BigInteger, Column, DateTime, Index, Integer, Text

from app.core.database import Base
from app.models.base import IdMixin


class ReviewReport(Base, IdMixin):
    __tablename__ = "review_report"
    __table_args__ = (
        Index("ix_review_report_user_create", "user_id", "create_time"),
    )

    task_id = Column(BigInteger, nullable=False, unique=True)
    user_id = Column(BigInteger, nullable=False)
    content_json = Column(JSON, nullable=False, comment="报告结构化数据")
    summary = Column(Text)
    score = Column(Integer, nullable=False, default=0)
    create_time = Column(DateTime, nullable=False)
