"""
用户自定义 API 配置表 ORM 模型

支持用户配置个人 API Key / 端点 / 模型，
未配置时自动回退到系统默认 DeepSeek 配置。
"""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, func

from app.core.database import Base


class UserApiConfig(Base):
    __tablename__ = "user_api_config"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        comment="用户ID（一对一关系）",
    )
    provider = Column(
        String(32),
        nullable=False,
        default="deepseek",
        comment="提供商: deepseek | openai | custom",
    )
    api_key_enc = Column(
        String(512),
        nullable=False,
        comment="Fernet 加密存储的 API Key",
    )
    base_url = Column(
        String(512),
        nullable=False,
        comment="API 端点地址",
    )
    model = Column(
        String(128),
        nullable=False,
        comment="模型名称",
    )
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="是否启用该配置",
    )
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
