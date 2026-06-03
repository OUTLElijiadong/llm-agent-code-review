"""
AI调用日志表ORM模型
"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin


def _utcnow():
    return datetime.now(timezone.utc)


class AiCallLog(Base, IdMixin):
    __tablename__ = "ai_call_log"

    task_id = Column(BigInteger)
    user_id = Column(BigInteger)
    file_id = Column(BigInteger)
    chunk_index = Column(Integer, comment="分片序号,从0开始")
    model_name = Column(String(50), nullable=False)
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    total_tokens = Column(Integer)
    duration_ms = Column(Integer)
    prompt = Column(LONGTEXT().with_variant(Text, "sqlite"), comment="送入LLM的user prompt")
    response = Column(LONGTEXT().with_variant(Text, "sqlite"), comment="原始返回")
    status = Column(String(20), nullable=False, default="success", comment="success/failed/retry")
    error_message = Column(String(500))
    create_time = Column(DateTime, nullable=False, default=_utcnow, comment="创建时间(UTC)")
