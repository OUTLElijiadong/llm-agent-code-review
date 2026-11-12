"""企业范围文字知识与附件；附件向量仅保存在所属企业的本地检索表。"""

from sqlalchemy import BigInteger, Column, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import MEDIUMBLOB, MEDIUMTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EnterpriseKnowledgeEntry(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_knowledge_entry"
    __table_args__ = (
        Index("ix_enterprise_knowledge_scope", "enterprise_id", "status", "id"),
        UniqueConstraint("enterprise_id", "author_id", "client_request_id", name="uq_enterprise_knowledge_create"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    author_id = Column(BigInteger, nullable=False)
    updated_by = Column(BigInteger, nullable=False)
    client_request_id = Column(String(80), nullable=False)
    create_hash = Column(String(64), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(MEDIUMTEXT().with_variant(Text, "sqlite"), nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    status = Column(String(20), nullable=False, default="active")


class EnterpriseKnowledgeAttachment(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_knowledge_attachment"
    __table_args__ = (
        Index("ix_enterprise_knowledge_attachment_scope", "enterprise_id", "status", "id"),
        UniqueConstraint(
            "enterprise_id", "author_id", "client_request_id",
            name="uq_enterprise_knowledge_attachment_create",
        ),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    author_id = Column(BigInteger, nullable=False)
    updated_by = Column(BigInteger, nullable=False)
    client_request_id = Column(String(80), nullable=False)
    create_hash = Column(String(64), nullable=False)
    file_name = Column(String(200), nullable=False)
    file_ext = Column(String(10), nullable=False)
    mime = Column(String(100), nullable=False)
    file_size = Column(Integer, nullable=False)
    chunk_count = Column(Integer, nullable=False, default=0, server_default="0")
    source_sha256 = Column(String(64), nullable=False)
    original_data = Column(MEDIUMBLOB().with_variant(LargeBinary(), "sqlite"), nullable=True)
    extracted_text = Column(MEDIUMTEXT().with_variant(Text, "sqlite"), nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    status = Column(String(20), nullable=False, default="active")


class EnterpriseKnowledgeAttachmentChunk(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_knowledge_attachment_chunk"
    __table_args__ = (
        Index("ix_enterprise_knowledge_attachment_chunk_scope", "enterprise_id", "attachment_id", "id"),
        UniqueConstraint("attachment_id", "seq", name="uq_enterprise_knowledge_attachment_chunk_seq"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    attachment_id = Column(BigInteger, nullable=False)
    seq = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Text, nullable=False)
    embed_model = Column(String(64), nullable=False)
