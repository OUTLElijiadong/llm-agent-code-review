"""
报告模板表 ORM 模型

存储 Jinja2 报告模板,支持 3 套预置模板(simple/detailed/compliance)与用户自定义模板。
"""
from sqlalchemy import BigInteger, Column, Index, Integer, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ReportTemplate(Base, IdMixin, TimestampMixin):
    """报告模板 ORM 模型

    一个 ReportTemplate 对应一套 Jinja2 报告模板。
    is_builtin=1 的 3 套模板(simple/detailed/compliance)不可删除;
    用户可创建自定义模板(type=custom)。
    """

    __tablename__ = "report_template"
    __table_args__ = (
        Index("ix_report_template_type", "type"),
        Index("ix_report_template_builtin", "is_builtin"),
    )

    name = Column(String(128), nullable=False, comment="模板名称")
    type = Column(String(32), nullable=False, comment="模板类型:simple/detailed/compliance/custom")
    content = Column(Text, nullable=False, comment="Jinja2 模板字符串")
    is_builtin = Column(Integer, nullable=False, default=0, comment="是否内置模板:0否 1是(内置不可删)")
    creator_id = Column(BigInteger, nullable=True, comment="创建者用户 ID")
    description = Column(String(255), nullable=True, comment="模板描述")
