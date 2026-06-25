"""add agent_name to evolution_proposal, create agent_skill_record table, add agent_label to ai_call_log

Revision ID: 005
Revises: 004
Create Date: 2026-06-25

本次迁移为 AgentSkill 自进化与总调度升级提供数据库支撑:
1. evolution_proposal 表新增 agent_name 字段(默认 evolution 兼容旧数据),标识提案产出 Agent
2. 新增 agent_skill_record 表,记录每次 Skill 调用(agent_name/skill_name/trigger_type/effect/duration)
3. ai_call_log 表新增 agent_label 字段(默认 NULL 兼容历史数据),供各 Agent Skill.reflect_from_logs 按归属 Agent 反思调用统计
4. 新增索引:ix_evolution_proposal_agent_name / ix_agent_skill_record_agent_created / ix_agent_skill_record_skill_effect / ix_ai_call_log_agent_label
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    """创建兼容 MySQL/SQLite 的自增主键列。

    Returns:
        sa.Column: id 主键列。
    """
    return sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


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
    """升级:evolution_proposal 加 agent_name + 新增 agent_skill_record 表 + ai_call_log 加 agent_label。

    Steps:
        1. evolution_proposal 表新增 agent_name 字段(server_default='evolution' 兼容旧数据)
        2. 新增 ix_evolution_proposal_agent_name 索引
        3. 创建 agent_skill_record 表(Skill 调用记录)
        4. 新增 agent_skill_record 两个索引
        5. ai_call_log 表新增 agent_label 字段(默认 NULL 兼容历史数据)
        6. 新增 ix_ai_call_log_agent_label 索引
    """
    # === evolution_proposal 表新增 agent_name 字段 ===
    op.add_column(
        "evolution_proposal",
        sa.Column(
            "agent_name",
            sa.String(50),
            nullable=False,
            server_default="evolution",
            comment="提案产出 Agent 名称,默认 evolution 兼容旧数据",
        ),
    )
    op.create_index(
        "ix_evolution_proposal_agent_name",
        "evolution_proposal",
        ["agent_name"],
    )

    # === ai_call_log 表新增 agent_label 字段(供 Skill.reflect_from_logs 按归属 Agent 反思) ===
    op.add_column(
        "ai_call_log",
        sa.Column(
            "agent_label",
            sa.String(50),
            nullable=True,
            comment="调用 Agent 名称,默认 NULL 兼容历史数据",
        ),
    )
    op.create_index(
        "ix_ai_call_log_agent_label",
        "ai_call_log",
        ["agent_label"],
    )

    # === 新增 agent_skill_record 表 ===
    op.create_table(
        "agent_skill_record",
        _id_column(),
        sa.Column("agent_name", sa.String(50), nullable=False, comment="Agent 名称"),
        sa.Column("skill_name", sa.String(100), nullable=False, comment="Skill 名称"),
        sa.Column(
            "trigger_type",
            sa.String(20),
            nullable=False,
            comment="触发类型: manual/scheduled/event/proactive",
        ),
        sa.Column(
            "trigger_source",
            sa.String(100),
            nullable=False,
            server_default="",
            comment="触发来源描述",
        ),
        sa.Column("input_params", sa.Text(), nullable=True, comment="输入参数 JSON"),
        sa.Column("output_summary", sa.Text(), nullable=True, comment="输出摘要(限 500 字)"),
        sa.Column(
            "effect",
            sa.String(20),
            nullable=False,
            server_default="success",
            comment="效果标签: success/failed/no_op/proposal_created",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="执行耗时(毫秒)",
        ),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger(),
            nullable=True,
            comment="触发用户 ID(manual 模式)",
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_agent_skill_record_agent_created",
        "agent_skill_record",
        ["agent_name", "create_time"],
    )
    op.create_index(
        "ix_agent_skill_record_skill_effect",
        "agent_skill_record",
        ["skill_name", "effect"],
    )


def downgrade() -> None:
    """回滚:删除 agent_skill_record 表、evolution_proposal.agent_name 与 ai_call_log.agent_label 字段。

    Steps:
        1. 删除 agent_skill_record 表的两个索引
        2. 删除 agent_skill_record 表
        3. 删除 ix_ai_call_log_agent_label 索引
        4. 删除 ai_call_log.agent_label 字段
        5. 删除 ix_evolution_proposal_agent_name 索引
        6. 删除 evolution_proposal.agent_name 字段
    """
    op.drop_index(
        "ix_agent_skill_record_skill_effect",
        table_name="agent_skill_record",
    )
    op.drop_index(
        "ix_agent_skill_record_agent_created",
        table_name="agent_skill_record",
    )
    op.drop_table("agent_skill_record")

    op.drop_index(
        "ix_ai_call_log_agent_label",
        table_name="ai_call_log",
    )
    op.drop_column("ai_call_log", "agent_label")

    op.drop_index(
        "ix_evolution_proposal_agent_name",
        table_name="evolution_proposal",
    )
    op.drop_column("evolution_proposal", "agent_name")
