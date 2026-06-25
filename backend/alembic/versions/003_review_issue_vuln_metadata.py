"""add review_issue vuln metadata and code_file binary support

Revision ID: 003
Revises: 002
Create Date: 2026-06-25

本次迁移为代码审查 Agent 集成与漏洞识别增强提供数据库支撑:
1. review_issue 表新增漏洞元数据字段(owasp/cwe/evidence/exploit_scenario/references_json/confidence/source)
2. code_file 表新增二进制文件支持字段(is_binary/original_blob)
3. 新增 ix_review_issue_task_cwe 索引(按 task_id + cwe 查询漏洞聚合)
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级:新增漏洞元数据字段与二进制文件支持字段

    Steps:
        1. review_issue 表新增 7 个字段(owasp/cwe/evidence/exploit_scenario/references_json/confidence/source)
        2. code_file 表新增 2 个字段(is_binary/original_blob)
        3. 新增 ix_review_issue_task_cwe 索引
    """
    # === review_issue 表新增字段 ===
    op.add_column(
        "review_issue",
        sa.Column("owasp", sa.String(32), nullable=True, comment="OWASP 编号,如 A03:2021-Injection"),
    )
    op.add_column(
        "review_issue",
        sa.Column("cwe", sa.String(32), nullable=True, comment="CWE 编号,如 CWE-89"),
    )
    op.add_column(
        "review_issue",
        sa.Column("evidence", sa.Text(), nullable=True, comment="漏洞证据代码片段(1-3 行)"),
    )
    op.add_column(
        "review_issue",
        sa.Column("exploit_scenario", sa.Text(), nullable=True, comment="攻击场景说明,30-200 字"),
    )
    op.add_column(
        "review_issue",
        sa.Column("references_json", sa.JSON(), nullable=True, comment="参考链接列表"),
    )
    op.add_column(
        "review_issue",
        sa.Column("confidence", sa.Float(), nullable=True, comment="置信度 0.0-1.0"),
    )
    op.add_column(
        "review_issue",
        sa.Column(
            "source",
            sa.String(16),
            nullable=True,
            server_default="llm",
            comment="发现来源:llm/static/regex",
        ),
    )

    # 新增索引:便于按 task + cwe 聚合漏洞统计
    op.create_index("ix_review_issue_task_cwe", "review_issue", ["task_id", "cwe"])

    # === code_file 表新增字段 ===
    op.add_column(
        "code_file",
        sa.Column(
            "is_binary",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="是否二进制文件:0否 1是",
        ),
    )
    op.add_column(
        "code_file",
        sa.Column(
            "original_blob",
            sa.LargeBinary(),
            nullable=True,
            comment="二进制文件原始字节(仅 is_binary=1 时使用)",
        ),
    )


def downgrade() -> None:
    """回滚:移除漏洞元数据字段与二进制文件支持字段

    Steps:
        1. 移除 code_file 表的 is_binary/original_blob 字段
        2. 移除 ix_review_issue_task_cwe 索引
        3. 移除 review_issue 表的 7 个字段
    """
    # === 回滚 code_file 表 ===
    op.drop_column("code_file", "original_blob")
    op.drop_column("code_file", "is_binary")

    # === 回滚 review_issue 表 ===
    op.drop_index("ix_review_issue_task_cwe", table_name="review_issue")
    op.drop_column("review_issue", "source")
    op.drop_column("review_issue", "confidence")
    op.drop_column("review_issue", "references_json")
    op.drop_column("review_issue", "exploit_scenario")
    op.drop_column("review_issue", "evidence")
    op.drop_column("review_issue", "cwe")
    op.drop_column("review_issue", "owasp")
