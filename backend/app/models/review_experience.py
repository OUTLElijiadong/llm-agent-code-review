"""经验记忆表 ORM 模型 — Agent 自进化 L1 在线经验

沉淀「被开发者确认(fixed)的真实问题」与「被忽略(ignored)的噪声」,
审查时按语言检索高权重经验注入 Prompt,带时间衰减自然淘汰过期经验。
"""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ReviewExperience(Base, IdMixin, TimestampMixin):
    __tablename__ = "review_experience"

    fingerprint = Column(String(64), nullable=False, index=True,
                         comment="问题指纹:issue_type+归一化模式哈希")
    language = Column(String(30), default="*", comment="适用语言,*=通用")
    issue_type = Column(String(50), nullable=False, comment="问题类型(中文枚举)")
    title = Column(String(200), comment="代表性问题标题")
    code_pattern = Column(LONGTEXT().with_variant(Text, "sqlite"),
                          comment="脱敏后的代表性代码片段")
    canonical_suggestion = Column(Text, comment="该类问题的优质修复建议(取自被采纳案例)")
    accepted_count = Column(Integer, nullable=False, default=0, comment="被采纳(fixed)次数")
    rejected_count = Column(Integer, nullable=False, default=0, comment="被忽略(ignored)次数")
    weight = Column(Float, nullable=False, default=0.0, comment="时间衰减后权重,低于阈值不再注入")
    last_seen = Column(DateTime, comment="最近一次出现时间(UTC),用于衰减")
    project_id = Column(BigInteger, comment="作用域项目,NULL=全局")
    user_id = Column(BigInteger, comment="作用域用户,NULL=全局")
