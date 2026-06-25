"""Agent Skill 调用记录表 ORM 模型 — AgentSkill 自进化与总调度升级

记录每次 Skill 调用(手动/定时/事件/主动触发),供:
- SkillManager 页面展示调用历史
- ProactiveSkill 反思学习(scan_domain / reflect_from_logs)
- admin 审计 Skill 执行情况
"""
from sqlalchemy import BigInteger, Column, Index, Integer, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class AgentSkillRecord(Base, IdMixin, TimestampMixin):
    """Skill 调用记录表 ORM 模型

    每次通过 skill_service.invoke_skill_with_record() 调用 Skill 时写入一条记录,
    记录触发类型、输入参数、输出摘要、效果标签与耗时,供前端 SkillManager 展示
    与 ProactiveSkill 反思学习使用。

    Attributes:
        agent_name: Agent 名称(如 code_reviewer / evolution)
        skill_name: Skill 名称(如 code_reviewer.self_improve)
        trigger_type: 触发类型(manual/scheduled/event/proactive)
        trigger_source: 触发来源描述(如 scheduler_cron / event:REVIEW_ISSUE_STATUS_CHANGED)
        input_params: 输入参数 JSON
        output_summary: 输出摘要(限 500 字)
        effect: 效果标签(success/failed/no_op/proposal_created)
        duration_ms: 执行耗时(毫秒)
        created_by_user_id: 触发用户 ID(manual 模式填写)
    """

    __tablename__ = "agent_skill_record"
    __table_args__ = (
        Index("ix_agent_skill_record_agent_created", "agent_name", "create_time"),
        Index("ix_agent_skill_record_skill_effect", "skill_name", "effect"),
    )

    agent_name = Column(String(50), nullable=False, comment="Agent 名称")
    skill_name = Column(String(100), nullable=False, comment="Skill 名称")
    trigger_type = Column(
        String(20),
        nullable=False,
        comment="触发类型: manual/scheduled/event/proactive",
    )
    trigger_source = Column(String(100), nullable=False, default="", comment="触发来源描述")
    input_params = Column(Text, comment="输入参数 JSON")
    output_summary = Column(Text, comment="输出摘要(限 500 字)")
    effect = Column(String(20), nullable=False, default="success", comment="效果标签")
    duration_ms = Column(Integer, nullable=False, default=0, comment="执行耗时(毫秒)")
    created_by_user_id = Column(BigInteger, comment="触发用户 ID(manual 模式)")
