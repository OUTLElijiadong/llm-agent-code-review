"""当前账号会话内的单次受控页面动作与客户端观察账本。"""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class AgentBrowserAction(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_browser_action"
    __table_args__ = (
        Index("uq_agent_browser_request", "request_id", unique=True),
        Index("ix_agent_browser_owner_session", "user_id", "surface", "session_key", "run_id"),
    )

    request_id = Column(String(64), nullable=False)
    input_sha256 = Column(String(64), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    token_version = Column(Integer, nullable=False)
    run_id = Column(String(80), nullable=False)
    session_key = Column(String(128), nullable=False)
    surface = Column(String(24), nullable=False)
    call_id = Column(String(160), nullable=False)
    action = Column(String(24), nullable=False)
    status = Column(String(24), nullable=False, default="queued")
    arguments_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    observation_snapshot_id = Column(String(32), nullable=False)
    nonce_sha256 = Column(String(64), nullable=False)
    claim_sha256 = Column(String(64), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    observation_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
    error = Column(String(500), nullable=True)
