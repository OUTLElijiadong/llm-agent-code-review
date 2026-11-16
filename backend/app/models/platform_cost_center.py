"""Detached platform-cost foundation; not registered with application metadata.

These tables have no production migration or call path. Do not enable them until
migration ordering, lifecycle, authorization and deployment gates are reviewed.
"""

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import declarative_base

PlatformCostBase = declarative_base()
_PK = BigInteger().with_variant(Integer, 'sqlite')
_DIGEST = LargeBinary(32).with_variant(mysql.VARBINARY(32), 'mysql')


class PlatformCostCenter(PlatformCostBase):
    __tablename__ = 'platform_cost_center'
    __table_args__ = (
        CheckConstraint('id = 1', name='ck_platform_cost_center_singleton'),
        CheckConstraint("status IN ('active', 'disabled')", name='ck_platform_cost_center_status'),
    )

    id = Column(_PK, primary_key=True, autoincrement=False)
    status = Column(String(16), nullable=False, default='disabled')


class PlatformCostBudget(PlatformCostBase):
    __tablename__ = 'platform_cost_budget'
    __table_args__ = (
        CheckConstraint('limit_cny_micros > 0', name='ck_platform_cost_limit_positive'),
        CheckConstraint('reserved_cny_micros >= 0 AND spent_cny_micros >= 0', name='ck_platform_cost_nonnegative'),
        CheckConstraint('reserved_cny_micros + spent_cny_micros <= limit_cny_micros',
                        name='ck_platform_cost_hard_limit'),
        CheckConstraint("status IN ('active', 'disabled')", name='ck_platform_cost_budget_status'),
        CheckConstraint('period_end > period_start', name='ck_platform_cost_period'),
    )

    id = Column(_PK, primary_key=True, autoincrement=True)
    center_id = Column(_PK, ForeignKey('platform_cost_center.id', ondelete='RESTRICT'), nullable=False, default=1)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    limit_cny_micros = Column(BigInteger, nullable=False)
    reserved_cny_micros = Column(BigInteger, nullable=False, default=0)
    spent_cny_micros = Column(BigInteger, nullable=False, default=0)
    status = Column(String(16), nullable=False, default='disabled')


class PlatformCostAttempt(PlatformCostBase):
    __tablename__ = 'platform_cost_attempt'
    __table_args__ = (
        CheckConstraint("status IN ('reserved', 'unknown')", name='ck_platform_cost_attempt_status'),
        CheckConstraint('reserved_cny_micros > 0', name='ck_platform_cost_attempt_reserved'),
        CheckConstraint('length(attempt_digest) = 32 AND length(payload_digest) = 32',
                        name='ck_platform_cost_attempt_digest'),
    )

    id = Column(_PK, primary_key=True, autoincrement=True)
    budget_id = Column(_PK, ForeignKey('platform_cost_budget.id', ondelete='RESTRICT'), nullable=False)
    actor_id = Column(BigInteger, nullable=False)
    attempt_digest = Column(_DIGEST, nullable=False, unique=True)
    attempt_bytes = Column(LargeBinary(640), nullable=False)
    payload_digest = Column(_DIGEST, nullable=False)
    status = Column(String(16), nullable=False)
    model_name = Column(String(80), nullable=False)
    provider_name = Column(String(32), nullable=False)
    provider_endpoint_host = Column(String(253), nullable=False)
    credential_source = Column(String(16), nullable=False)
    pricing_version = Column(String(64), nullable=False)
    input_cost_cny_micros_per_million = Column(BigInteger, nullable=False)
    output_cost_cny_micros_per_million = Column(BigInteger, nullable=False)
    input_limit = Column(BigInteger, nullable=False)
    output_limit = Column(BigInteger, nullable=False)
    reserved_cny_micros = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False)
