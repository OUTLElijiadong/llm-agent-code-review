"""黄金回归集表 ORM 模型 — Agent 自进化 评估闸门基准

进化提案 promote 前必须在这些人工锚点用例上复跑,
precision/recall 不退化才放行。seed 用例永不被进化覆盖,防回声室。
"""
from sqlalchemy import Column, Index, SmallInteger, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EvalCase(Base, IdMixin, TimestampMixin):
    __tablename__ = "eval_case"
    __table_args__ = (
        Index("ix_eval_case_enabled", "enabled"),
    )

    name = Column(String(100), nullable=False, comment="用例名")
    language = Column(String(30), default="*", comment="语言")
    code = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, comment="代码片段")
    expected_issues = Column(Text, nullable=False,
                             comment="期望命中 JSON: [{issue_type, keyword?}]")
    tags = Column(String(200), comment="分类标签 security/performance/...")
    enabled = Column(SmallInteger, nullable=False, default=1, comment="1=纳入闸门")
    source = Column(String(30), nullable=False, default="seed",
                    comment="seed=内置锚点 / from_feedback=真实案例固化")
