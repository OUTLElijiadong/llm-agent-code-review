"""add cvss/compliance_mapping/remediation/static_rule_hits to review_issue, add raw_size to code_file

Revision ID: 006
Revises: 005
Create Date: 2026-06-25

本次迁移为代码审计 Agent 集成与漏洞识别增强(全量方案)提供数据库支撑:
1. review_issue 表新增 5 个字段:
   - cvss_score: CVSS v3.1 基础分(0.0-10.0)
   - cvss_vector: CVSS 向量字符串(如 AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)
   - compliance_mapping: 合规标准条款映射 JSON(iso27001/gdpr/pci_dss/hipaa)
   - remediation: 修复建议详细文本
   - static_rule_hits: 静态规则命中次数(用于双引擎统计)
2. code_file 表新增 1 个字段:
   - raw_size: 原始字节数(用于项目总大小校验,与 size_bytes 区别在于含 binary 真实大小)
3. 新增索引:ix_review_issue_task_severity_cwe(按 task+severity+cwe 聚合漏洞统计)
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级:为 review_issue 与 code_file 表补充漏洞全量元数据字段

    Steps:
        1. review_issue 表新增 cvss_score/cvss_vector/compliance_mapping/remediation/static_rule_hits 5 字段
        2. code_file 表新增 raw_size 字段
        3. 新增 ix_review_issue_task_severity_cwe 复合索引
    """
    # === review_issue 表新增字段 ===
    op.add_column(
        "review_issue",
        sa.Column("cvss_score", sa.Float(), nullable=True, comment="CVSS v3.1 基础分 0.0-10.0"),
    )
    op.add_column(
        "review_issue",
        sa.Column(
            "cvss_vector",
            sa.String(64),
            nullable=True,
            comment="CVSS 向量,如 AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        ),
    )
    op.add_column(
        "review_issue",
        sa.Column(
            "compliance_mapping",
            sa.JSON(),
            nullable=True,
            comment='合规标准条款映射 {"iso27001":["A.14.2.1"],"gdpr":["Art.32"],"pci_dss":["Req-6.2.4"],"hipaa":["§164.312(b)"]}',
        ),
    )
    op.add_column(
        "review_issue",
        sa.Column("remediation", sa.Text(), nullable=True, comment="修复建议详细文本"),
    )
    op.add_column(
        "review_issue",
        sa.Column(
            "static_rule_hits",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="静态规则命中次数(双引擎统计)",
        ),
    )

    # 新增复合索引:便于按 task + severity + cwe 聚合漏洞统计(用于报告 metrics)
    op.create_index(
        "ix_review_issue_task_severity_cwe",
        "review_issue",
        ["task_id", "severity", "cwe"],
    )

    # === code_file 表新增字段 ===
    op.add_column(
        "code_file",
        sa.Column(
            "raw_size",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="原始字节数(含 binary 真实大小,用于项目总大小校验)",
        ),
    )


def downgrade() -> None:
    """回滚:移除 review_issue 与 code_file 表的全量元数据字段

    Steps:
        1. 移除 code_file 表的 raw_size 字段
        2. 移除 ix_review_issue_task_severity_cwe 索引
        3. 移除 review_issue 表的 5 个字段
    """
    # === 回滚 code_file 表 ===
    op.drop_column("code_file", "raw_size")

    # === 回滚 review_issue 表 ===
    op.drop_index("ix_review_issue_task_severity_cwe", table_name="review_issue")
    op.drop_column("review_issue", "static_rule_hits")
    op.drop_column("review_issue", "remediation")
    op.drop_column("review_issue", "compliance_mapping")
    op.drop_column("review_issue", "cvss_vector")
    op.drop_column("review_issue", "cvss_score")
