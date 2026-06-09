"""
审查规则表ORM模型
"""
from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ReviewRule(Base, IdMixin, TimestampMixin):
    __tablename__ = "review_rule"
    __table_args__ = (
        UniqueConstraint("user_id", "rule_code", name="uk_user_rule_code"),
        Index("ix_review_rule_enabled", "enabled"),
    )

    user_id = Column(BigInteger, comment="NULL表示系统内置规则")
    rule_code = Column(String(50), nullable=False, comment="机器标识")
    rule_name = Column(String(100), nullable=False, comment="展示名称")
    rule_type = Column(String(50), nullable=False, comment="类型分组")
    rule_content = Column(Text, nullable=False, comment="Prompt片段")
    language = Column(String(30), default="*", comment="适用编程语言,*=通用")
    severity = Column(String(10), default="中", comment="严重|高|中|低")
    enabled = Column(SmallInteger, nullable=False, default=1, comment="1=启用,0=禁用")
    is_builtin = Column(SmallInteger, nullable=False, default=0, comment="1=内置不可删")
    sort_order = Column(Integer, nullable=False, default=0)
