"""管理员副驾驶持久化会话、消息与运维执行记录。"""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class AdminChatSession(Base, IdMixin, TimestampMixin):
    """按管理员隔离的持久化副驾驶会话。"""

    __tablename__ = "admin_chat_session"
    __table_args__ = (
        Index("uq_admin_chat_session_owner_key", "user_id", "session_key", unique=True),
        Index("ix_admin_chat_session_last_message", "last_message_at"),
    )

    user_id = Column(BigInteger, nullable=False)
    session_key = Column(String(128), nullable=False)
    title = Column(String(200), nullable=False, default="管理副驾驶")
    status = Column(String(30), nullable=False, default="active")
    last_message_at = Column(DateTime, nullable=True)


class AdminChatMessage(Base, IdMixin, TimestampMixin):
    """副驾驶消息，payload 保留组件协议以便刷新后恢复卡片。"""

    __tablename__ = "admin_chat_message"
    __table_args__ = (
        Index("ix_admin_chat_message_session_id", "session_id", "id"),
        Index("ix_admin_chat_message_action_hash", "action_token_hash"),
        Index("ix_admin_chat_message_trace", "trace_id"),
    )

    session_id = Column(BigInteger, nullable=False)
    role = Column(String(20), nullable=False)
    message_type = Column(String(30), nullable=False, default="text")
    content = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
    payload_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    action_token_hash = Column(String(64), nullable=True)
    action_status = Column(String(30), nullable=True)
    agent_code = Column(String(80), nullable=True)
    trace_id = Column(String(80), nullable=True)


class OpsExecution(Base, IdMixin, TimestampMixin):
    """运维 Agent 每次执行的不可变审计摘要。"""

    __tablename__ = "ops_execution"
    __table_args__ = (
        Index("uq_ops_execution_request_id", "request_id", unique=True),
        Index("ix_ops_execution_status", "status"),
        Index("ix_ops_execution_action", "action"),
    )

    request_id = Column(String(64), nullable=False)
    session_id = Column(BigInteger, nullable=True)
    actor_id = Column(BigInteger, nullable=True)
    action = Column(String(80), nullable=False)
    risk_level = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False, default="running")
    params_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, default="{}")
    result_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
