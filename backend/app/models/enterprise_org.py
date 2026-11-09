"""企业组织边界：平台角色不代表任何企业的成员或管理权限。"""
from sqlalchemy import JSON, BigInteger, Boolean, Column, Date, DateTime, Index, Integer, String, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class Enterprise(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise"
    __table_args__ = (UniqueConstraint("paid_order_id", name="uk_enterprise_paid_order"),)

    name = Column(String(120), nullable=False)
    owner_id = Column(BigInteger, nullable=False, index=True)
    seat_limit = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="active")
    billing_suspended = Column(Boolean, nullable=False, default=False, server_default="0")
    proactive_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    proactive_monthly_credits = Column(Integer, nullable=False, default=0, server_default="0")
    proactive_version = Column(Integer, nullable=False, default=0, server_default="0")
    proactive_day = Column(Date, nullable=True)
    proactive_daily_count = Column(Integer, nullable=False, default=0, server_default="0")
    clarification_authority_version = Column(BigInteger, nullable=False, default=0, server_default="0")
    paid_order_id = Column(String(64), nullable=False)


class EnterpriseMember(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_member"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "user_id", name="uk_enterprise_member"),
        Index("ix_enterprise_member_user_status", "user_id", "status"),
    )

    enterprise_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False)
    role = Column(String(20), nullable=False, default="member")
    status = Column(String(20), nullable=False, default="active")
    default_group_id = Column(BigInteger, nullable=True)
    billing_group_version = Column(BigInteger, nullable=False, default=0, server_default="0")
    clarification_authority_version = Column(BigInteger, nullable=False, default=0, server_default="0")


class EnterpriseGroup(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_group"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "name", name="uk_enterprise_group_name"),
        UniqueConstraint("enterprise_id", "id", name="uq_ent_group_scope"),
    )

    enterprise_id = Column(BigInteger, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    status = Column(String(20), nullable=False, default="active")


class EnterpriseGroupMember(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_group_member"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "group_id", "user_id", name="uk_enterprise_group_member"),
        Index("ix_enterprise_group_member_user", "enterprise_id", "user_id", "status"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    group_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False)
    role = Column(String(20), nullable=False, default="member")
    status = Column(String(20), nullable=False, default="active")
    clarification_authority_version = Column(BigInteger, nullable=False, default=0, server_default="0")


class EnterpriseInvitation(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_invitation"

    enterprise_id = Column(BigInteger, nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_by = Column(BigInteger, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    max_uses = Column(Integer, nullable=False, default=1)
    use_count = Column(Integer, nullable=False, default=0)
    revoked_at = Column(DateTime, nullable=True)


class EnterpriseAuditEvent(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_audit_event"
    __table_args__ = (Index("ix_enterprise_audit_created", "enterprise_id", "create_time"),)

    enterprise_id = Column(BigInteger, nullable=False)
    actor_id = Column(BigInteger, nullable=False)
    action = Column(String(50), nullable=False)
    subject_user_id = Column(BigInteger, nullable=True)
    group_id = Column(BigInteger, nullable=True)
    detail = Column(JSON, nullable=False, default=dict)
