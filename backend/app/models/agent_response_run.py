"""Responses Agent 的可恢复运行检查点。"""

from sqlalchemy import BigInteger, Column, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class AgentResponseRun(Base, IdMixin, TimestampMixin):
    """按用户和会话隔离的完整 Responses 工具循环状态。"""

    __tablename__ = "agent_response_run"
    __table_args__ = (
        Index("uq_agent_response_run_id", "run_id", unique=True),
        Index("ix_agent_response_run_owner_session", "user_id", "surface", "session_key"),
        Index("ix_agent_response_run_status", "status"),
    )

    run_id = Column(String(80), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    surface = Column(String(24), nullable=False)
    session_key = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False, default="running")
    checkpoint_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    version = Column(Integer, nullable=False, default=1)


class AgentToolExecution(Base, IdMixin, TimestampMixin):
    """按 run_id + call_id 去重的工具执行结果账本。"""

    __tablename__ = "agent_tool_execution"
    __table_args__ = (
        Index("uq_agent_tool_execution_request", "request_id", unique=True),
        Index("ix_agent_tool_execution_run", "run_id"),
        Index("ix_agent_tool_execution_status", "status"),
    )

    request_id = Column(String(64), nullable=False)
    run_id = Column(String(80), nullable=False)
    call_id = Column(String(160), nullable=False)
    user_id = Column(BigInteger, nullable=False)
    tool_name = Column(String(120), nullable=False)
    status = Column(String(30), nullable=False, default="executing")
    arguments_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    result_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
    error = Column(Text, nullable=True)
