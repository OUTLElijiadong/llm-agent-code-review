"""add agent governance platform tables

Revision ID: 002
Revises: 001
Create Date: 2026-06-25
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    """创建兼容 MySQL/SQLite 的自增主键列。

    Returns:
        sa.Column: id 主键列。
    """
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """创建通用创建/更新时间列。

    Returns:
        tuple[sa.Column, sa.Column]: create_time 与 update_time 两列。
    """
    return (
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    """创建 Agent 治理平台相关表。"""
    op.create_table(
        "agent_profile",
        _id_column(),
        sa.Column("code", sa.String(80), nullable=False, comment="Agent 唯一编码"),
        sa.Column("name", sa.String(120), nullable=False, comment="Agent 名称"),
        sa.Column("description", sa.Text(), comment="Agent 职责说明"),
        sa.Column("category", sa.String(50), nullable=False, server_default="general", comment="Agent 分类"),
        sa.Column("status", sa.String(30), nullable=False, server_default="idle", comment="运行状态"),
        sa.Column("model", sa.String(128), comment="默认模型"),
        sa.Column("icon", sa.String(50), nullable=False, server_default="base", comment="前端图标"),
        sa.Column("color", sa.String(30), nullable=False, server_default="#5B58E8", comment="展示色"),
        sa.Column("budget_tokens_daily", sa.Integer(), nullable=False, server_default="0", comment="每日 token 预算"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50", comment="调度优先级"),
        sa.Column("auto_approval_threshold", sa.Float(), nullable=False, server_default="0.75", comment="自动审批阈值"),
        sa.Column("is_enabled", sa.SmallInteger(), nullable=False, server_default="1", comment="是否启用"),
        sa.Column("config_json", sa.Text(), comment="扩展配置 JSON"),
        *_timestamps(),
    )
    op.create_index("ix_agent_profile_code", "agent_profile", ["code"], unique=True)
    op.create_index("ix_agent_profile_category", "agent_profile", ["category"])

    op.create_table(
        "agent_skill_binding",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False, comment="Agent 编码"),
        sa.Column("skill_code", sa.String(120), nullable=False, comment="Skill 编码"),
        sa.Column("skill_name", sa.String(120), nullable=False, comment="Skill 名称"),
        sa.Column("version", sa.String(50), nullable=False, server_default="1.0.0", comment="Skill 版本"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1", comment="是否启用"),
        sa.Column("config_json", sa.Text(), comment="Skill 配置 JSON"),
        *_timestamps(),
    )
    op.create_index("ix_agent_skill_agent", "agent_skill_binding", ["agent_code"])
    op.create_index("ix_agent_skill_code", "agent_skill_binding", ["skill_code"])

    op.create_table(
        "agent_tool_permission",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("tool_code", sa.String(120), nullable=False),
        sa.Column("permission", sa.String(30), nullable=False, server_default="allow"),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="low"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("note", sa.String(300)),
        *_timestamps(),
    )
    op.create_index("ix_agent_tool_permission_agent", "agent_tool_permission", ["agent_code"])
    op.create_index("ix_agent_tool_permission_tool", "agent_tool_permission", ["tool_code"])

    op.create_table(
        "policy_rule",
        _id_column(),
        sa.Column("rule_code", sa.String(120), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("subject", sa.String(120), nullable=False, server_default="*"),
        sa.Column("action", sa.String(120), nullable=False, server_default="*"),
        sa.Column("resource", sa.String(120), nullable=False, server_default="*"),
        sa.Column("effect", sa.String(30), nullable=False, server_default="allow"),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="low"),
        sa.Column("condition_json", sa.Text()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        *_timestamps(),
    )
    op.create_index("ix_policy_rule_code", "policy_rule", ["rule_code"], unique=True)
    op.create_index("ix_policy_rule_enabled", "policy_rule", ["enabled"])

    op.create_table(
        "policy_decision_log",
        _id_column(),
        sa.Column("subject", sa.String(120), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("resource", sa.String(160), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="low"),
        sa.Column("risk_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(500)),
        sa.Column("matched_rule_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite")),
        sa.Column("context_json", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_policy_decision_subject", "policy_decision_log", ["subject"])
    op.create_index("ix_policy_decision_decision", "policy_decision_log", ["decision"])
    op.create_index("ix_policy_decision_risk", "policy_decision_log", ["risk_level"])

    op.create_table(
        "approval_item",
        _id_column(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("agent_code", sa.String(80)),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("resource", sa.String(160), nullable=False),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="low"),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("decision", sa.String(30)),
        sa.Column("decision_reason", sa.String(500)),
        sa.Column("request_json", sa.Text()),
        sa.Column("decided_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite")),
        sa.Column("decided_at", sa.DateTime()),
        *_timestamps(),
    )
    op.create_index("ix_approval_item_status", "approval_item", ["status"])
    op.create_index("ix_approval_item_risk", "approval_item", ["risk_level"])
    op.create_index("ix_approval_item_agent", "approval_item", ["agent_code"])

    op.create_table(
        "tool_call_log",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("tool_code", sa.String(120), nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("resource", sa.String(160), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="low"),
        sa.Column("decision", sa.String(30), nullable=False, server_default="allow"),
        sa.Column("input_summary", sa.Text()),
        sa.Column("output_summary", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("policy_decision_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite")),
        sa.Column("approval_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite")),
        *_timestamps(),
    )
    op.create_index("ix_tool_call_agent", "tool_call_log", ["agent_code"])
    op.create_index("ix_tool_call_status", "tool_call_log", ["status"])
    op.create_index("ix_tool_call_risk", "tool_call_log", ["risk_level"])

    op.create_table(
        "agent_memory",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("memory_type", sa.String(30), nullable=False, server_default="long_term"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("source_ref", sa.String(160)),
        *_timestamps(),
    )
    op.create_index("ix_agent_memory_agent", "agent_memory", ["agent_code"])
    op.create_index("ix_agent_memory_type", "agent_memory", ["memory_type"])

    op.create_table(
        "agent_knowledge_source",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_uri", sa.String(500), nullable=False),
        sa.Column("whitelist", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("config_json", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_agent_knowledge_source_agent", "agent_knowledge_source", ["agent_code"])
    op.create_index("ix_agent_knowledge_source_enabled", "agent_knowledge_source", ["enabled"])

    op.create_table(
        "agent_knowledge_doc",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("source_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite")),
        sa.Column("source_type", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(160)),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="low"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_index("ix_agent_knowledge_doc_agent", "agent_knowledge_doc", ["agent_code"])
    op.create_index("ix_agent_knowledge_doc_status", "agent_knowledge_doc", ["status"])

    op.create_table(
        "agent_knowledge_chunk",
        _id_column(),
        sa.Column("doc_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text()),
        sa.Column("embed_model", sa.String(64)),
        *_timestamps(),
    )
    op.create_index("ix_agent_knowledge_chunk_agent", "agent_knowledge_chunk", ["agent_code"])
    op.create_index("ix_agent_knowledge_chunk_doc", "agent_knowledge_chunk", ["doc_id"])

    op.create_table(
        "agent_job",
        _id_column(),
        sa.Column("job_code", sa.String(120), nullable=False),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("agent_code", sa.String(80)),
        sa.Column("schedule", sa.String(120), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(30), nullable=False, server_default="enabled"),
        sa.Column("last_run_at", sa.DateTime()),
        sa.Column("config_json", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_agent_job_code", "agent_job", ["job_code"], unique=True)
    op.create_index("ix_agent_job_agent", "agent_job", ["agent_code"])

    op.create_table(
        "agent_job_run",
        _id_column(),
        sa.Column("job_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("result_json", sa.Text()),
        sa.Column("error", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_agent_job_run_job", "agent_job_run", ["job_id"])
    op.create_index("ix_agent_job_run_status", "agent_job_run", ["status"])

    op.create_table(
        "agent_reflection",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("task_ref", sa.String(160)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("lesson", sa.Text()),
        sa.Column("risk_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reward_score", sa.Float(), nullable=False, server_default="0"),
        *_timestamps(),
    )
    op.create_index("ix_agent_reflection_agent", "agent_reflection", ["agent_code"])
    op.create_index("ix_agent_reflection_risk", "agent_reflection", ["risk_score"])

    op.create_table(
        "agent_reward_event",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(500)),
        sa.Column("impact_json", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_agent_reward_agent", "agent_reward_event", ["agent_code"])
    op.create_index("ix_agent_reward_type", "agent_reward_event", ["event_type"])

    op.create_table(
        "agent_artifact_version",
        _id_column(),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("artifact_type", sa.String(50), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("snapshot", sa.Text()),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        *_timestamps(),
    )
    op.create_index("ix_agent_artifact_agent", "agent_artifact_version", ["agent_code"])
    op.create_index("ix_agent_artifact_type", "agent_artifact_version", ["artifact_type"])
    op.create_index("ix_agent_artifact_status", "agent_artifact_version", ["status"])

    op.create_table(
        "agent_alert",
        _id_column(),
        sa.Column("alert_type", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(30), nullable=False, server_default="info"),
        sa.Column("status", sa.String(30), nullable=False, server_default="open"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("detail_json", sa.Text()),
        sa.Column("resolved_by", sa.BigInteger().with_variant(sa.Integer(), "sqlite")),
        sa.Column("resolved_at", sa.DateTime()),
        *_timestamps(),
    )
    op.create_index("ix_agent_alert_status", "agent_alert", ["status"])
    op.create_index("ix_agent_alert_severity", "agent_alert", ["severity"])

    op.create_table(
        "agent_metric_snapshot",
        _id_column(),
        sa.Column("metric_key", sa.String(120), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("dimension_json", sa.Text()),
        sa.Column("window_start", sa.DateTime()),
        sa.Column("window_end", sa.DateTime()),
        *_timestamps(),
    )
    op.create_index("ix_agent_metric_key", "agent_metric_snapshot", ["metric_key"])
    op.create_index("ix_agent_metric_window", "agent_metric_snapshot", ["window_start", "window_end"])


def downgrade() -> None:
    """删除 Agent 治理平台相关表。"""
    for table in (
        "agent_metric_snapshot",
        "agent_alert",
        "agent_artifact_version",
        "agent_reward_event",
        "agent_reflection",
        "agent_job_run",
        "agent_job",
        "agent_knowledge_chunk",
        "agent_knowledge_doc",
        "agent_knowledge_source",
        "agent_memory",
        "tool_call_log",
        "approval_item",
        "policy_decision_log",
        "policy_rule",
        "agent_tool_permission",
        "agent_skill_binding",
        "agent_profile",
    ):
        op.drop_table(table)
