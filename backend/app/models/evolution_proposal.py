"""进化提案表 ORM 模型 — Agent 自进化 L2/L3

EvolutionAgent 从反馈聚合产出的「候选进化动作」,默认不生效,
经评估闸门(eval_case)+ admin 人工审批才 promote 写入 review_rule;
applied_snapshot 保存改动前状态以支持一键回滚。
"""
from sqlalchemy import BigInteger, Column, DateTime, Index, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EvolutionProposal(Base, IdMixin, TimestampMixin):
    __tablename__ = "evolution_proposal"
    __table_args__ = (
        Index("ix_evolution_proposal_status", "status"),
        Index("ix_evolution_proposal_type", "proposal_type"),
    )

    proposal_type = Column(String(30), nullable=False,
                           comment="new_rule/disable_rule/adjust_severity/narrow_language/new_fewshot")
    target_rule_id = Column(BigInteger, comment="针对已有规则的提案指向")
    title = Column(String(200), nullable=False, comment="提案摘要")
    payload = Column(Text, nullable=False, comment="提案内容 JSON")
    evidence = Column(Text, comment="支撑证据 JSON(样本量/采纳率/假阳性率/代表案例)")
    status = Column(String(20), nullable=False, default="pending",
                    comment="pending/eval_passed/eval_failed/approved/rejected/promoted/rolled_back")
    eval_score = Column(Text, comment="评估闸门跑分 JSON(before/after precision/recall/noise)")
    applied_rule_id = Column(BigInteger, comment="promote 后实际写入/改动的规则 id")
    applied_snapshot = Column(Text, comment="改动前状态 JSON,供回滚还原")
    created_by = Column(String(50), nullable=False, default="evolution_agent")
    reviewed_by = Column(BigInteger, comment="审批人(admin)")
    reviewed_at = Column(DateTime, comment="审批时间(UTC)")
    note = Column(String(500), comment="驳回原因/回滚说明")
    agent_name = Column(
        String(50),
        nullable=False,
        default="evolution",
        comment="提案产出 Agent 名称,默认 evolution 兼容旧数据",
    )
