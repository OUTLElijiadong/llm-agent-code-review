"""
项目成员表ORM模型

建立项目与用户的成员关系,支持按项目成员关系做数据隔离。
- role_in_project='owner': 项目拥有者(创建者),拥有读写权限
- role_in_project='reviewer': 审查员,拥有项目相关数据的读权限 + 发起审查权限
"""
from sqlalchemy import BigInteger, Column, Index, String, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ProjectMember(Base, IdMixin, TimestampMixin):
    """项目成员关系表

    Attributes:
        project_id: 项目ID,关联project.id
        user_id: 用户ID,关联user.id
        role_in_project: 项目内角色(owner/reviewer)
    """

    __tablename__ = "project_member"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uk_project_user"),
        Index("ix_pm_user", "user_id"),
        Index("ix_pm_project", "project_id"),
    )

    project_id = Column(BigInteger, nullable=False, comment="项目ID")
    user_id = Column(BigInteger, nullable=False, comment="用户ID")
    role_in_project = Column(
        String(20),
        nullable=False,
        default="reviewer",
        comment="项目内角色: owner/reviewer",
    )
