"""小菱动态子 Agent 团队、工作图和可审计事件账本。"""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin

_JSON = LONGTEXT().with_variant(Text, "sqlite")


class AgentTeam(Base, IdMixin, TimestampMixin):
    """同一账户内一次动态子 Agent 协作运行。"""

    __tablename__ = "agent_team"
    __table_args__ = (
        Index("ix_agent_team_owner_status", "user_id", "status", "id"),
        Index("ix_agent_team_trace", "user_id", "trace_id"),
    )

    user_id = Column(BigInteger, nullable=False)
    surface = Column(String(24), nullable=False)
    session_key = Column(String(128), nullable=False)
    title = Column(String(200), nullable=False)
    objective = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="draft")
    max_active_children = Column(Integer, nullable=False, default=3)
    max_attempts = Column(Integer, nullable=False, default=3)
    priority = Column(Integer, nullable=False, default=0)
    trace_id = Column(String(80), nullable=False)
    deadline_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    summary_json = Column(_JSON, nullable=False, default="{}")
    error_json = Column(_JSON, nullable=False, default="{}")
    archived_at = Column(DateTime, nullable=True)


class AgentTeamMember(Base, IdMixin, TimestampMixin):
    """一个团队内固定能力快照的临时 Agent 成员。"""

    __tablename__ = "agent_team_member"
    __table_args__ = (
        UniqueConstraint("team_id", "member_key", name="uk_agent_team_member_key"),
        Index("ix_agent_team_member_team_status", "team_id", "status"),
    )

    team_id = Column(BigInteger, nullable=False)
    member_key = Column(String(80), nullable=False)
    display_name = Column(String(200), nullable=False)
    address = Column(String(200), nullable=False)
    kind = Column(String(24), nullable=False)
    role = Column(String(24), nullable=False, default="worker")
    template_id = Column(BigInteger, nullable=True)
    template_version_id = Column(BigInteger, nullable=True)
    capabilities_json = Column(_JSON, nullable=False, default="{}")
    status = Column(String(24), nullable=False, default="created")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class AgentTeamTask(Base, IdMixin, TimestampMixin):
    """团队工作图中的原子任务，依赖通过 task_key JSON 固定。"""

    __tablename__ = "agent_team_task"
    __table_args__ = (
        UniqueConstraint("team_id", "task_key", name="uk_agent_team_task_key"),
        Index("ix_agent_team_task_queue", "team_id", "status", "priority", "id"),
        Index("ix_agent_team_task_lease", "status", "lease_expires_at"),
    )

    team_id = Column(BigInteger, nullable=False)
    member_id = Column(BigInteger, nullable=False)
    task_key = Column(String(80), nullable=False)
    title = Column(String(240), nullable=False)
    instructions = Column(Text, nullable=False)
    dependency_keys_json = Column(_JSON, nullable=False, default="[]")
    input_json = Column(_JSON, nullable=False, default="{}")
    status = Column(String(32), nullable=False, default="waiting_dependency")
    priority = Column(Integer, nullable=False, default=0)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    next_attempt_at = Column(DateTime, nullable=True)
    lease_token = Column(String(80), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    result_json = Column(_JSON, nullable=False, default="{}")
    artifacts_json = Column(_JSON, nullable=False, default="[]")
    errors_json = Column(_JSON, nullable=False, default="[]")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class AgentTeamEvent(Base, IdMixin, TimestampMixin):
    """团队不可变状态事件，用于悬浮窗时间线和审计复核。"""

    __tablename__ = "agent_team_event"
    __table_args__ = (
        Index("ix_agent_team_event_team", "team_id", "id"),
        Index("ix_agent_team_event_trace", "user_id", "trace_id", "id"),
        Index("ix_agent_team_event_task", "task_id", "id"),
    )

    team_id = Column(BigInteger, nullable=False)
    task_id = Column(BigInteger, nullable=True)
    member_id = Column(BigInteger, nullable=True)
    user_id = Column(BigInteger, nullable=False)
    message_id = Column(String(80), nullable=True)
    correlation_id = Column(String(80), nullable=False, default="")
    event_type = Column(String(48), nullable=False)
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=True)
    actor_address = Column(String(200), nullable=False)
    trace_id = Column(String(80), nullable=False)
    detail_json = Column(_JSON, nullable=False, default="{}")
