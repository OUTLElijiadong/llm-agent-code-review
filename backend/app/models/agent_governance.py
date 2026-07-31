"""Agent 治理平台 ORM 模型。

该模块集中定义 Agent 身份、策略、审批、工具、记忆、知识、调度、奖惩、
告警和版本表，作为 agent-governance-platform 的治理数据底座。
"""
from sqlalchemy import BigInteger, Column, DateTime, Float, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class AgentProfile(Base, IdMixin, TimestampMixin):
    """Agent 持久化画像，记录治理层可配置的身份、状态与预算。"""

    __tablename__ = "agent_profile"
    __table_args__ = (
        Index("ix_agent_profile_code", "code", unique=True),
        Index("ix_agent_profile_category", "category"),
    )

    code = Column(String(80), nullable=False, comment="Agent 唯一编码")
    name = Column(String(120), nullable=False, comment="Agent 名称")
    description = Column(Text, comment="Agent 职责说明")
    category = Column(String(50), nullable=False, default="general", comment="Agent 分类")
    status = Column(String(30), nullable=False, default="idle", comment="idle/working/disabled/error")
    model = Column(String(128), comment="默认模型")
    icon = Column(String(50), nullable=False, default="base", comment="前端图标")
    color = Column(String(30), nullable=False, default="#5B58E8", comment="展示色")
    budget_tokens_daily = Column(Integer, nullable=False, default=0, comment="每日 token 预算，0 表示不限")
    priority = Column(Integer, nullable=False, default=50, comment="调度优先级")
    auto_approval_threshold = Column(Float, nullable=False, default=0.75, comment="自动审批置信阈值")
    is_enabled = Column(SmallInteger, nullable=False, default=1, comment="是否启用")
    config_json = Column(Text, comment="扩展配置 JSON")


class AgentSkillBinding(Base, IdMixin, TimestampMixin):
    """Agent 与 skill 的绑定关系。"""

    __tablename__ = "agent_skill_binding"
    __table_args__ = (
        Index("ix_agent_skill_agent", "agent_code"),
        Index("ix_agent_skill_code", "skill_code"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    skill_code = Column(String(120), nullable=False, comment="Skill 编码")
    skill_name = Column(String(120), nullable=False, comment="Skill 名称")
    version = Column(String(50), nullable=False, default="1.0.0", comment="Skill 版本")
    enabled = Column(SmallInteger, nullable=False, default=1, comment="是否启用")
    config_json = Column(Text, comment="Skill 配置 JSON")


class AgentToolPermission(Base, IdMixin, TimestampMixin):
    """Agent 工具权限配置。"""

    __tablename__ = "agent_tool_permission"
    __table_args__ = (
        Index("ix_agent_tool_permission_agent", "agent_code"),
        Index("ix_agent_tool_permission_tool", "tool_code"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    tool_code = Column(String(120), nullable=False, comment="工具编码")
    permission = Column(String(30), nullable=False, default="allow", comment="allow/deny/escalate")
    risk_level = Column(String(30), nullable=False, default="low", comment="low/medium/high/critical")
    enabled = Column(SmallInteger, nullable=False, default=1, comment="是否启用")
    note = Column(String(300), comment="说明")


class PolicyRule(Base, IdMixin, TimestampMixin):
    """ABAC 策略规则。"""

    __tablename__ = "policy_rule"
    __table_args__ = (
        Index("ix_policy_rule_code", "rule_code", unique=True),
        Index("ix_policy_rule_enabled", "enabled"),
    )

    rule_code = Column(String(120), nullable=False, comment="策略编码")
    name = Column(String(160), nullable=False, comment="策略名称")
    subject = Column(String(120), nullable=False, default="*", comment="主体匹配")
    action = Column(String(120), nullable=False, default="*", comment="动作匹配")
    resource = Column(String(120), nullable=False, default="*", comment="资源匹配")
    effect = Column(String(30), nullable=False, default="allow", comment="allow/deny/escalate")
    risk_level = Column(String(30), nullable=False, default="low", comment="风险等级")
    condition_json = Column(Text, comment="条件 JSON")
    priority = Column(Integer, nullable=False, default=100, comment="优先级，越小越优先")
    enabled = Column(SmallInteger, nullable=False, default=1, comment="是否启用")


class PolicyDecisionLog(Base, IdMixin, TimestampMixin):
    """策略决策日志。"""

    __tablename__ = "policy_decision_log"
    __table_args__ = (
        Index("ix_policy_decision_subject", "subject"),
        Index("ix_policy_decision_decision", "decision"),
        Index("ix_policy_decision_risk", "risk_level"),
    )

    subject = Column(String(120), nullable=False, comment="主体")
    action = Column(String(120), nullable=False, comment="动作")
    resource = Column(String(160), nullable=False, comment="资源")
    decision = Column(String(30), nullable=False, comment="allow/deny/escalate")
    risk_level = Column(String(30), nullable=False, default="low", comment="风险等级")
    risk_score = Column(Float, nullable=False, default=0.0, comment="风险分")
    reason = Column(String(500), comment="决策原因")
    matched_rule_id = Column(BigInteger, comment="命中策略 ID")
    context_json = Column(Text, comment="上下文 JSON")


class ApprovalItem(Base, IdMixin, TimestampMixin):
    """审批事项，包含自动审批与人工审批记录。"""

    __tablename__ = "approval_item"
    __table_args__ = (
        Index("ix_approval_item_status", "status"),
        Index("ix_approval_item_risk", "risk_level"),
        Index("ix_approval_item_agent", "agent_code"),
        Index("ix_approval_item_copilot_request", "copilot_request_id", unique=True),
    )

    title = Column(String(200), nullable=False, comment="审批标题")
    agent_code = Column(String(80), comment="关联 Agent")
    action = Column(String(120), nullable=False, comment="动作")
    resource = Column(String(160), nullable=False, comment="资源")
    risk_level = Column(String(30), nullable=False, default="low", comment="风险等级")
    status = Column(String(30), nullable=False, default="pending", comment="pending/approved/rejected/auto_approved")
    decision = Column(String(30), comment="allow/deny/escalate")
    decision_reason = Column(String(500), comment="决策原因")
    request_json = Column(LONGTEXT().with_variant(Text, "sqlite"), comment="请求 JSON")
    copilot_request_id = Column(String(64), comment="管理员副驾驶确认请求唯一标识")
    decided_by = Column(BigInteger, comment="审批人")
    decided_at = Column(DateTime, comment="审批时间")


class ToolCallLog(Base, IdMixin, TimestampMixin):
    """Agent 工具调用日志。"""

    __tablename__ = "tool_call_log"
    __table_args__ = (
        Index("ix_tool_call_agent", "agent_code"),
        Index("ix_tool_call_status", "status"),
        Index("ix_tool_call_risk", "risk_level"),
        Index("ix_tool_call_copilot_request", "copilot_request_id", unique=True),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    tool_code = Column(String(120), nullable=False, comment="工具编码")
    action = Column(String(120), nullable=False, comment="动作")
    resource = Column(String(160), nullable=False, default="", comment="资源")
    status = Column(String(30), nullable=False, default="pending", comment="success/failed/denied/escalated")
    risk_level = Column(String(30), nullable=False, default="low", comment="风险等级")
    decision = Column(String(30), nullable=False, default="allow", comment="策略决策")
    input_summary = Column(Text, comment="输入摘要")
    output_summary = Column(Text, comment="输出摘要")
    error = Column(Text, comment="错误信息")
    duration_ms = Column(Integer, nullable=False, default=0, comment="耗时")
    policy_decision_id = Column(BigInteger, comment="策略决策日志 ID")
    approval_id = Column(BigInteger, comment="审批事项 ID")
    copilot_request_id = Column(String(64), comment="管理员副驾驶直接执行请求唯一标识")


class AgentMemory(Base, IdMixin, TimestampMixin):
    """Agent 独立记忆。"""

    __tablename__ = "agent_memory"
    __table_args__ = (
        Index("ix_agent_memory_agent", "agent_code"),
        Index("ix_agent_memory_type", "memory_type"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    memory_type = Column(String(30), nullable=False, default="long_term", comment="short_term/long_term/reflection")
    title = Column(String(200), nullable=False, comment="记忆标题")
    content = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, comment="记忆内容")
    weight = Column(Float, nullable=False, default=1.0, comment="权重")
    status = Column(String(30), nullable=False, default="active", comment="active/archived/deleted")
    source_ref = Column(String(160), comment="来源引用")


class AgentKnowledgeSource(Base, IdMixin, TimestampMixin):
    """Agent 知识抓取来源。"""

    __tablename__ = "agent_knowledge_source"
    __table_args__ = (
        Index("ix_agent_knowledge_source_agent", "agent_code"),
        Index("ix_agent_knowledge_source_enabled", "enabled"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    source_type = Column(String(30), nullable=False, comment="project/docs/url/github/official")
    source_uri = Column(String(500), nullable=False, comment="来源 URI")
    whitelist = Column(SmallInteger, nullable=False, default=1, comment="是否白名单")
    enabled = Column(SmallInteger, nullable=False, default=1, comment="是否启用")
    config_json = Column(Text, comment="抓取配置 JSON")


class AgentKnowledgeDoc(Base, IdMixin, TimestampMixin):
    """Agent 知识库文档。"""

    __tablename__ = "agent_knowledge_doc"
    __table_args__ = (
        Index("ix_agent_knowledge_doc_agent", "agent_code"),
        Index("ix_agent_knowledge_doc_status", "status"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    source_id = Column(BigInteger, comment="知识来源 ID")
    source_type = Column(String(30), nullable=False, default="manual", comment="来源类型")
    source_ref = Column(String(160), comment="来源引用")
    title = Column(String(240), nullable=False, comment="标题")
    risk_level = Column(String(30), nullable=False, default="low", comment="风险等级")
    confidence = Column(Float, nullable=False, default=1.0, comment="置信度")
    status = Column(String(30), nullable=False, default="active", comment="active/pending_approval/deleted")
    char_count = Column(Integer, nullable=False, default=0, comment="字符数")
    chunk_count = Column(Integer, nullable=False, default=0, comment="切片数")


class AgentKnowledgeChunk(Base, IdMixin, TimestampMixin):
    """Agent 知识库切片。"""

    __tablename__ = "agent_knowledge_chunk"
    __table_args__ = (
        Index("ix_agent_knowledge_chunk_agent", "agent_code"),
        Index("ix_agent_knowledge_chunk_doc", "doc_id"),
    )

    doc_id = Column(BigInteger, nullable=False, comment="文档 ID")
    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    seq = Column(Integer, nullable=False, default=0, comment="切片序号")
    content = Column(Text, nullable=False, comment="切片内容")
    embedding = Column(LONGTEXT().with_variant(Text, "sqlite"), comment="嵌入向量 JSON")
    embed_model = Column(String(64), comment="嵌入模型")


class AgentJob(Base, IdMixin, TimestampMixin):
    """Agent 调度任务定义。"""

    __tablename__ = "agent_job"
    __table_args__ = (
        Index("ix_agent_job_code", "job_code", unique=True),
        Index("ix_agent_job_agent", "agent_code"),
    )

    job_code = Column(String(120), nullable=False, comment="任务编码")
    job_type = Column(String(50), nullable=False, comment="crawl/distill/evolution/reflection")
    agent_code = Column(String(80), comment="Agent 编码")
    schedule = Column(String(120), nullable=False, default="manual", comment="调度表达式")
    status = Column(String(30), nullable=False, default="enabled", comment="enabled/disabled")
    last_run_at = Column(DateTime, comment="最近运行时间")
    config_json = Column(Text, comment="任务配置 JSON")


class AgentJobRun(Base, IdMixin, TimestampMixin):
    """Agent 调度任务运行记录。"""

    __tablename__ = "agent_job_run"
    __table_args__ = (
        Index("ix_agent_job_run_job", "job_id"),
        Index("ix_agent_job_run_status", "status"),
    )

    job_id = Column(BigInteger, nullable=False, comment="任务 ID")
    status = Column(String(30), nullable=False, default="running", comment="running/success/failed")
    started_at = Column(DateTime, comment="开始时间")
    finished_at = Column(DateTime, comment="结束时间")
    result_json = Column(Text, comment="运行结果 JSON")
    error = Column(Text, comment="错误")


class AgentReflection(Base, IdMixin, TimestampMixin):
    """Agent 自我反思记录。"""

    __tablename__ = "agent_reflection"
    __table_args__ = (
        Index("ix_agent_reflection_agent", "agent_code"),
        Index("ix_agent_reflection_risk", "risk_score"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    task_ref = Column(String(160), comment="任务引用")
    summary = Column(Text, nullable=False, comment="反思摘要")
    lesson = Column(Text, comment="沉淀经验")
    risk_score = Column(Float, nullable=False, default=0.0, comment="风险分")
    reward_score = Column(Float, nullable=False, default=0.0, comment="奖惩分")


class AgentRewardEvent(Base, IdMixin, TimestampMixin):
    """Agent 奖励/惩罚事件。"""

    __tablename__ = "agent_reward_event"
    __table_args__ = (
        Index("ix_agent_reward_agent", "agent_code"),
        Index("ix_agent_reward_type", "event_type"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    event_type = Column(String(30), nullable=False, comment="reward/penalty")
    score = Column(Float, nullable=False, default=0.0, comment="分数")
    reason = Column(String(500), comment="原因")
    impact_json = Column(Text, comment="影响 JSON")


class AgentArtifactVersion(Base, IdMixin, TimestampMixin):
    """Agent prompt/skill/策略/知识等 artifact 版本。"""

    __tablename__ = "agent_artifact_version"
    __table_args__ = (
        Index("ix_agent_artifact_agent", "agent_code"),
        Index("ix_agent_artifact_type", "artifact_type"),
        Index("ix_agent_artifact_status", "status"),
    )

    agent_code = Column(String(80), nullable=False, comment="Agent 编码")
    artifact_type = Column(String(50), nullable=False, comment="prompt/skill/policy/knowledge/code")
    version = Column(String(50), nullable=False, comment="版本号")
    content = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False, comment="版本内容")
    snapshot = Column(LONGTEXT().with_variant(Text, "sqlite"), comment="回滚快照")
    status = Column(String(30), nullable=False, default="draft", comment="draft/gray/stable/rolled_back")


class AgentAlert(Base, IdMixin, TimestampMixin):
    """Agent 治理告警。"""

    __tablename__ = "agent_alert"
    __table_args__ = (
        Index("ix_agent_alert_status", "status"),
        Index("ix_agent_alert_severity", "severity"),
    )

    alert_type = Column(String(80), nullable=False, comment="告警类型")
    severity = Column(String(30), nullable=False, default="info", comment="info/warning/high/critical")
    status = Column(String(30), nullable=False, default="open", comment="open/resolved")
    title = Column(String(200), nullable=False, comment="标题")
    detail_json = Column(Text, comment="详情 JSON")
    resolved_by = Column(BigInteger, comment="处理人")
    resolved_at = Column(DateTime, comment="处理时间")


class AgentMetricSnapshot(Base, IdMixin, TimestampMixin):
    """Agent 治理指标快照。"""

    __tablename__ = "agent_metric_snapshot"
    __table_args__ = (
        Index("ix_agent_metric_key", "metric_key"),
        Index("ix_agent_metric_window", "window_start", "window_end"),
    )

    metric_key = Column(String(120), nullable=False, comment="指标键")
    metric_value = Column(Float, nullable=False, default=0.0, comment="指标值")
    dimension_json = Column(Text, comment="维度 JSON")
    window_start = Column(DateTime, comment="窗口开始")
    window_end = Column(DateTime, comment="窗口结束")
