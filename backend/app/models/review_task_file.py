"""
审查任务与代码文件关联表 ORM 模型
"""
from sqlalchemy import BigInteger, Column, DateTime, Index, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, _utcnow


class ReviewTaskFile(Base, IdMixin):
    """持久化一次审查实际包含的代码文件。"""

    __tablename__ = "review_task_file"
    __table_args__ = (
        UniqueConstraint("task_id", "file_id", name="uk_task_file"),
        Index("idx_file", "file_id"),
    )

    task_id = Column(BigInteger, nullable=False, index=True)
    file_id = Column(BigInteger, nullable=False)
    create_time = Column(DateTime, default=_utcnow, nullable=False)
