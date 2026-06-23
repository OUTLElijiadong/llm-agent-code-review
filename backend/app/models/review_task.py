"""
审查任务表ORM模型
"""
from sqlalchemy import JSON, BigInteger, Column, DateTime, Index, Integer, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ReviewTask(Base, IdMixin, TimestampMixin):
    __tablename__ = "review_task"
    __table_args__ = (
        Index("ix_review_task_user_create", "user_id", "create_time"),
        Index("ix_review_task_project_status", "project_id", "status"),
        Index("ix_review_task_status", "status"),
    )

    user_id = Column(BigInteger, nullable=False)
    project_id = Column(BigInteger, nullable=False)
    task_name = Column(String(100))
    review_type = Column(String(50), nullable=False, default="standard", comment="quick/standard/full")
    status = Column(String(20), nullable=False, default="pending", comment="pending/running/success/failed/cancelled")
    total_files = Column(Integer, nullable=False, default=0)
    processed_files = Column(Integer, nullable=False, default=0)
    total_issues = Column(Integer, nullable=False, default=0)
    severe_issues = Column(Integer, nullable=False, default=0)
    high_issues = Column(Integer, nullable=False, default=0)
    medium_issues = Column(Integer, nullable=False, default=0)
    low_issues = Column(Integer, nullable=False, default=0)
    score = Column(Integer, nullable=False, default=0, comment="综合评分0-100")
    summary = Column(Text, comment="AI总体评价")
    rules_snapshot = Column(JSON, comment="本次启用的规则快照")
    model_name = Column(String(50))
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    duration_ms = Column(Integer, nullable=False, default=0, comment="耗时毫秒")
    error_message = Column(String(500))
