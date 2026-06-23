"""
项目表ORM模型
"""
from sqlalchemy import BigInteger, Column, Index, String, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class Project(Base, IdMixin, TimestampMixin):
    __tablename__ = "project"
    __table_args__ = (
        UniqueConstraint("user_id", "project_name", name="uk_user_proj_name"),
        Index("ix_project_user_status", "user_id", "status"),
        Index("ix_project_create_time", "create_time"),
    )

    user_id = Column(BigInteger, nullable=False, comment="所属用户")
    project_name = Column(String(100), nullable=False, comment="项目名")
    description = Column(String(500))
    language = Column(String(50), comment="主语言")
    status = Column(String(20), nullable=False, default="active", comment="active/archived/deleted")
