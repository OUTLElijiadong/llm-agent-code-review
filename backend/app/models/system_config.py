"""
系统配置表 ORM 模型(键值)

目前用于管理员配置 RAG 嵌入(embedding)端点/模型/Key 等运行期可调参数。
未配置时由 embedding_service 回退到环境变量,再回退到本地降级方案。
"""
from sqlalchemy import Column, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class SystemConfig(Base, IdMixin, TimestampMixin):
    __tablename__ = "system_config"

    config_key = Column(String(64), nullable=False, unique=True, comment="配置键")
    config_value = Column(Text, comment="配置值(字符串/JSON)")
