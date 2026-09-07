"""所有调用来源均通过可空外键持久化；未知历史来源不猜测。"""

from sqlalchemy import BigInteger, Column, ForeignKey


class UsageAttributionMixin:
    root_agent_run_id = Column(
        BigInteger, ForeignKey("agent_response_run.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_run_id = Column(
        BigInteger, ForeignKey("agent_response_run.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_execution_id = Column(
        BigInteger, ForeignKey("agent_tool_execution.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_team_id = Column(BigInteger, ForeignKey("agent_team.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_team_task_id = Column(
        BigInteger, ForeignKey("agent_team_task.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_execution_event_id = Column(
        BigInteger, ForeignKey("agent_team_event.id", ondelete="SET NULL"), nullable=True, index=True
    )
