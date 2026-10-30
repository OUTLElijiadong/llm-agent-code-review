"""restore review_rule language/severity columns missing from the frozen baseline

Revision ID: 065_review_rule_lang_severity
Revises: 064_remote_pentest_chain

review_rule 由冻结基线 deploy/mysql/init.sql 创建，基线里没有 language/severity，而 ORM
(app/models/review_rule.py)早已声明它们，rule_service 按语言取规则也直接查询 language。
老库当年靠手工 ALTER 补过列，所以生产不受影响；但从 init.sql + seed.sql 起步的全新库走完
001~064 仍缺这两列，首个规则查询就报 1054 Unknown column。

本迁移按 ORM 定义补列，已存在的列(包括手工补过、定义不同的)原样保留，MySQL 与 SQLite 均幂等。
新增列带 server_default，已有行(含 seed.sql 写入的内置规则)随之得到 language='*'、severity='中'。
"""

import sqlalchemy as sa

from alembic import op

revision = "065_review_rule_lang_severity"
down_revision = "064_remote_pentest_chain"
branch_labels = None
depends_on = None

_TABLE = "review_rule"


def _missing_columns() -> list[sa.Column]:
    columns = [
        sa.Column("language", sa.String(30), nullable=True, server_default="*", comment="适用编程语言,*=通用"),
        sa.Column("severity", sa.String(10), nullable=True, server_default="中", comment="严重|高|中|低"),
    ]
    if op.get_context().as_sql:
        # 离线生成 SQL 时没有连接可探查现有结构，按全新库输出完整 DDL。
        return columns
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    return [column for column in columns if column.name not in existing]


def upgrade() -> None:
    for column in _missing_columns():
        op.add_column(_TABLE, column)


def downgrade() -> None:
    # 有意不删列：生产库的这两列是手工补的，迁移无法区分"本迁移创建"与"早已存在"，
    # 删除会抹掉内置规则的语言/严重度并让 ORM 查询重新报 1054。重新升级本就幂等。
    pass
