"""账号隔离的圆桌会话与有序发言账本。"""

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, String

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RoundtableSession(Base):
    __tablename__ = "roundtable_session"
    __table_args__ = (
        Index("ix_roundtable_session_owner_updated", "owner_user_id", "updated_at"),
        Index("ix_roundtable_session_owner_status", "owner_user_id", "status"),
    )

    session_id = Column(String(64), primary_key=True)
    owner_user_id = Column(BigInteger, nullable=False)
    task_id = Column(BigInteger, nullable=False, default=0)
    project_id = Column(BigInteger, nullable=False, default=0)
    file_id = Column(BigInteger, nullable=False, default=0)
    file_name = Column(String(255), nullable=False)
    review_type = Column(String(50), nullable=False, default="full")
    status = Column(String(20), nullable=False, default="active")
    max_rounds = Column(Integer, nullable=False, default=2)
    report_task_id = Column(BigInteger, nullable=False, default=0)
    agents = Column(JSON, nullable=False)
    progress = Column(JSON, nullable=False)
    last_turn_seq = Column(Integer, nullable=False, default=0)
    origin_surface = Column(String(24), nullable=False, default="")
    origin_session_key = Column(String(128), nullable=False, default="")
    continued_from_session_id = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    closed_at = Column(DateTime, nullable=True)


class RoundtableTurn(Base):
    __tablename__ = "roundtable_turn"
    __table_args__ = (
        Index("ix_roundtable_turn_owner_session", "owner_user_id", "session_id"),
    )

    session_id = Column(String(64), ForeignKey("roundtable_session.session_id"), primary_key=True)
    seq = Column(Integer, primary_key=True)
    owner_user_id = Column(BigInteger, nullable=False)
    turn = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
