"""
个人知识库 — 文档/来源 ORM 模型(RAG)

每条记录代表一个被纳入用户专属知识库的来源:手动上传、项目代码、
已处理的审查问题/报告、论坛/反馈/工单历史。严格按 user_id 隔离。
"""
from sqlalchemy import BigInteger, Column, Index, Integer, String

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class KnowledgeDoc(Base, IdMixin, TimestampMixin):
    __tablename__ = "knowledge_doc"
    __table_args__ = (
        # 用户文档列表(按状态) + 来源去重查找(对应 init.sql idx_user_status / idx_user_source)
        Index("ix_knowledge_doc_user_status", "user_id", "status"),
        Index("ix_knowledge_doc_user_source", "user_id", "source_type", "source_ref"),
    )

    user_id = Column(BigInteger, nullable=False, comment="所属用户(隔离键)")
    source_type = Column(
        String(20), nullable=False, default="upload",
        comment="upload/code/issue/forum/feedback/ticket",
    )
    source_ref = Column(String(64), comment="来源引用,如 file:123 / issue:45,用于去重")
    title = Column(String(200), nullable=False, comment="文档标题")
    char_count = Column(Integer, nullable=False, default=0, comment="原文字符数")
    chunk_count = Column(Integer, nullable=False, default=0, comment="切片数")
    status = Column(String(20), nullable=False, default="active", comment="active/deleted")
