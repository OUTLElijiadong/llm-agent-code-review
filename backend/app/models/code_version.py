"""
代码版本历史表ORM模型
"""
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin


class CodeVersion(Base, IdMixin):
    __tablename__ = "code_version"

    file_id = Column(BigInteger, nullable=False)
    version_no = Column(Integer, nullable=False)
    content = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    change_desc = Column(String(255), comment="修改说明")
    operator_id = Column(BigInteger, comment="操作人")
    create_time = Column(DateTime, nullable=False, comment="创建时间")
