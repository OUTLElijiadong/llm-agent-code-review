"""
审查问题表ORM模型
"""
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ReviewIssue(Base, IdMixin, TimestampMixin):
    __tablename__ = "review_issue"

    task_id = Column(BigInteger, nullable=False)
    file_id = Column(BigInteger)
    file_name = Column(String(255), comment="冗余字段,便于报告导出")
    line_number = Column(Integer, comment="问题所在行,0表示文件级")
    end_line = Column(Integer, comment="可选区间结束行")
    issue_type = Column(String(50), nullable=False, comment="问题类型枚举")
    severity = Column(String(20), nullable=False, comment="严重/高/中/低")
    title = Column(String(200))
    description = Column(Text, nullable=False)
    suggestion = Column(Text)
    fixed_code = Column(LONGTEXT().with_variant(Text, "sqlite"))
    status = Column(String(20), nullable=False, default="unfixed", comment="unfixed/fixed/ignored/pending_review")
    handled_by = Column(BigInteger, comment="状态变更人")
    handled_at = Column(DateTime)
