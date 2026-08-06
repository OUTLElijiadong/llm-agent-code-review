"""
代码文件表ORM模型

v2 增强:新增 is_binary/original_blob 字段,支持二进制文件(图片/可执行文件等)的原始字节存储,
避免在编辑器中以 base64 字符串形式展示。
"""
from sqlalchemy import BigInteger, Column, Index, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class CodeFile(Base, IdMixin, TimestampMixin):
    """代码文件 ORM 模型

    一个 CodeFile 对应项目内的一个代码文件或二进制文件。
    v2 增强:is_binary=1 时 content 字段仍存 base64(向后兼容),original_blob 存原始字节,
    API 返回时 content 置空,前端通过下载接口获取原文件。
    """

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
    size_bytes = Column(BigInteger, nullable=False, default=0, comment="字节数")
    line_count = Column(Integer, nullable=False, default=0, comment="行数")
    version_no = Column(Integer, nullable=False, default=1, comment="当前版本号")
    content = Column(
        LONGTEXT().with_variant(Text, "sqlite"),
        nullable=False,
        comment="代码内容UTF-8;binary 文件存 base64",
    )
    status = Column(String(20), nullable=False, default="active", comment="active/deleted")

    # === v2 二进制文件支持(2026-06-25 新增)===
    is_binary = Column(Integer, nullable=False, default=0, comment="是否二进制文件:0否 1是")
    # 026 迁移:MySQL 上扩为 LONGBLOB,容纳超过 64KB 的二进制源码成员
    original_blob = Column(
        LONGBLOB().with_variant(LargeBinary, "sqlite"),
        nullable=True,
        comment="二进制文件原始字节(仅 is_binary=1 时使用)",
    )

    # === v3 原始大小字段(2026-06-25 006 迁移新增)===
    # 026 迁移:扩为 BIGINT,与解除大小上限后的真实文件体量对齐
    raw_size = Column(BigInteger, nullable=False, default=0, comment="原始字节数(含 binary 真实大小,用于项目总大小校验)")  # noqa: E501
