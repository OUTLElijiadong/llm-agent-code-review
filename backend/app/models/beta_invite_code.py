"""一次性内测邀请码 ORM 模型。"""

from sqlalchemy import BigInteger, Column, DateTime, Index, String

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class BetaInviteCode(Base, IdMixin, TimestampMixin):
    """仅持久化邀请码摘要，明文只在生成响应中出现一次。"""

    __tablename__ = "beta_invite_code"
    __table_args__ = (
        Index("ux_beta_invite_code_hash", "code_hash", unique=True),
        Index("ix_beta_invite_status_expires", "status", "expires_at"),
        Index("ix_beta_invite_created_by", "created_by"),
    )

    code_hash = Column(String(64), nullable=False, comment="HMAC-SHA256 摘要")
    display_prefix = Column(String(32), nullable=False, comment="用于管理列表的脱敏前缀")
    label = Column(String(100), nullable=True, comment="管理员备注")
    status = Column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
        comment="active/used/revoked",
    )
    expires_at = Column(DateTime, nullable=False)
    created_by = Column(BigInteger, nullable=False, comment="创建管理员用户 ID")
    used_by = Column(BigInteger, nullable=True, comment="成功注册用户 ID")
    used_at = Column(DateTime, nullable=True)
