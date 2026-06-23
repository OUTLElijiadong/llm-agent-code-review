"""
个人知识库 — 切片向量 ORM 模型(RAG)

存放文档切片正文与其嵌入向量(JSON 浮点数组,文本存储)。
检索时按 user_id 取出该用户的全部切片,在 Python 内做余弦相似度,
适配小体量个人知识库,避免引入重型向量库(生产服务器内存仅 ~1.9G)。
"""
from sqlalchemy import BigInteger, Column, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class KnowledgeChunk(Base, IdMixin, TimestampMixin):
    __tablename__ = "knowledge_chunk"

    doc_id = Column(BigInteger, nullable=False, comment="所属文档")
    user_id = Column(BigInteger, nullable=False, comment="所属用户(隔离键)")
    seq = Column(Integer, nullable=False, default=0, comment="切片序号")
    content = Column(Text, nullable=False, comment="切片正文")
    # 大模型嵌入向量(如 3072 维)JSON 可超过 TEXT 的 64KB,MySQL 用 LONGTEXT
    embedding = Column(
        LONGTEXT().with_variant(Text, "sqlite"),
        comment="嵌入向量 JSON 数组;为空表示尚未嵌入",
    )
    embed_model = Column(String(64), comment="向量来源标记(api:model 或 fallback)")
