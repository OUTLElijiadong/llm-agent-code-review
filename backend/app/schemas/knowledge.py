"""
个人知识库(RAG) Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DocAddIn(BaseModel):
    """手动添加一篇知识文档"""
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class DocOut(BaseModel):
    """知识文档列表项响应

    R6/R7 修复(2026-06-25):补齐 status/update_time,对齐 KnowledgeDoc ORM。
    """
    id: int
    title: str
    source_type: str
    source_ref: Optional[str] = None
    char_count: int = 0
    chunk_count: int = 0
    create_time: datetime
    # R6/R7 修复:补齐 status/update_time,对齐 KnowledgeDoc ORM
    status: str = "active"
    update_time: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SearchIn(BaseModel):
    """知识库检索(联调/演示用)"""
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class SearchHitOut(BaseModel):
    content: str
    score: float
    doc_id: int
    title: str
    source_type: str


class KbStatsOut(BaseModel):
    doc_total: int = 0
    chunk_total: int = 0
    by_source: dict = {}
    remote_embedding: bool = False


class SyncResultOut(BaseModel):
    code: int = 0
    issue: int = 0
    forum: int = 0
    feedback: int = 0
    ticket: int = 0
    total: int = 0


class EmbeddingConfigIn(BaseModel):
    """管理员配置 embedding(api_key 留空表示不修改)"""
    base_url: Optional[str] = Field(default=None, max_length=512)
    model: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=512)
    enabled: Optional[bool] = None


class EmbeddingConfigOut(BaseModel):
    base_url: str = ""
    model: str = ""
    enabled: bool = False
    api_key_set: bool = False
