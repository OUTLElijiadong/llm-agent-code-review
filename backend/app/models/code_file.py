"""
代码文件表ORM模型
"""
from sqlalchemy import BigInteger, Column, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class CodeFile(Base, IdMixin, TimestampMixin):
    __tablename__ = "code_file"
    __table_args__ = (
        Index("ix_code_file_project_status", "project_id", "status"),
        Index("ix_code_file_project_lang", "project_id", "language"),
        Index("ix_code_file_create_time", "create_time"),
    )

    project_id = Column(BigInteger, nullable=False)
    file_name = Column(String(255), nullable=False, comment="文件名(含扩展名)")
    file_path = Column(String(500), comment="逻辑路径")
    language = Column(String(50), nullable=False, comment="语言标识")
    size_bytes = Column(Integer, nullable=False, default=0, comment="字节数")
    line_count = Column(Integer, nullable=False, default=0, comment="行数")
    version_no = Column(Integer, nullable=False, default=1, comment="当前版本号")
    content = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, comment="代码内容UTF-8")
    status = Column(String(20), nullable=False, default="active", comment="active/deleted")
