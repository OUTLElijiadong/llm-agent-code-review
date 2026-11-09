"""企业项目的归属与显式授权，禁止将平台角色作为租户权限。"""
from sqlalchemy import BigInteger, Column, Index, String, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EnterpriseProject(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_project"
    __table_args__ = (
        UniqueConstraint("project_id", name="uk_enterprise_project_project"),
        UniqueConstraint("enterprise_id", "project_id", name="uq_enterprise_project_tenant_scope"),
        Index("ix_enterprise_project_group", "enterprise_id", "group_id"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    project_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=False)
    status = Column(String(20), nullable=False, default="active")


class EnterpriseProjectGrant(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_project_grant"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "project_id", "user_id", name="uk_enterprise_project_grant"),
        Index("ix_enterprise_project_grant_user", "enterprise_id", "user_id", "status"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    project_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    role = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="active")
