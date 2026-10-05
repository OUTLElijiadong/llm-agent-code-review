"""allow namespaced production operation names in the audit action column

Revision ID: 062_audit_log_action_length
Revises: 061_overview_time_indexes
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "062_audit_log_action_length"
down_revision = "061_overview_time_indexes"
branch_labels = None
depends_on = None

_COMMENT = "操作类型，含 namespaced 运维动作"
_OLD_COMMENT = "操作类型: login/user/rule/ai/project/agent"


def _alter_action(length: int) -> None:
    bind = op.get_bind()
    alter = op.batch_alter_table if bind.dialect.name == "sqlite" else None
    if alter is not None:
        with alter("audit_log") as batch:
            batch.alter_column(
                "action",
                existing_type=sa.String(length=40 if length == 63 else 63),
                type_=sa.String(length=length),
                existing_nullable=False,
                existing_comment=_OLD_COMMENT if length == 63 else _COMMENT,
                comment=_COMMENT if length == 63 else _OLD_COMMENT,
            )
        return

    op.alter_column(
        "audit_log",
        "action",
        existing_type=sa.String(length=40 if length == 63 else 63),
        type_=sa.String(length=length),
        existing_nullable=False,
        existing_comment=_OLD_COMMENT if length == 63 else _COMMENT,
        comment=_COMMENT if length == 63 else _OLD_COMMENT,
    )


def upgrade() -> None:
    _alter_action(63)


def downgrade() -> None:
    bind = op.get_bind()
    length_function = "LENGTH" if bind.dialect.name == "sqlite" else "CHAR_LENGTH"
    count = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM audit_log WHERE {length_function}(action) > 40")
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"拒绝缩短 audit_log.action：仍有 {count} 条长度超过 40 的审计动作；先制定无损迁移方案"
        )
    _alter_action(40)
