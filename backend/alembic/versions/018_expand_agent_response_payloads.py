"""Expand Responses Agent payload columns beyond MySQL TEXT limits.

Revision ID: 018
Revises: 017
"""

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels = None
depends_on = None

_MYSQL_TEXT_MAX_BYTES = 65_535
_EXPANDED_COLUMNS = (
    ("agent_response_run", "checkpoint_json", False),
    ("agent_tool_execution", "arguments_json", False),
    ("agent_tool_execution", "result_json", True),
    ("approval_item", "request_json", True),
    ("admin_chat_message", "content", True),
    ("admin_chat_message", "payload_json", False),
    ("ops_execution", "params_json", False),
    ("ops_execution", "result_json", True),
)


def _is_mysql() -> bool:
    return op.get_bind().dialect.name == "mysql"


def upgrade() -> None:
    # SQLite TEXT is already unbounded. MySQL TEXT stops at 64 KiB, which is
    # smaller than an admin run containing the complete tool schema.
    if not _is_mysql():
        return
    for table_name, column_name, nullable in _EXPANDED_COLUMNS:
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.Text(),
            type_=mysql.LONGTEXT(),
            existing_nullable=nullable,
        )


def _assert_text_downgrade_fits() -> None:
    bind = op.get_bind()
    oversized = []
    for table_name, column_name, _nullable in _EXPANDED_COLUMNS:
        # Names come from the constant above, not user-controlled input.
        size = bind.execute(
            sa.text(
                f"SELECT COALESCE(MAX(OCTET_LENGTH(`{column_name}`)), 0) "
                f"FROM `{table_name}`"
            )
        ).scalar_one()
        if int(size or 0) > _MYSQL_TEXT_MAX_BYTES:
            oversized.append(f"{table_name}.{column_name}={int(size)} bytes")
    if oversized:
        raise RuntimeError(
            "Refusing LONGTEXT to TEXT downgrade because payloads exceed 65535 bytes: "
            + ", ".join(oversized)
        )


def downgrade() -> None:
    if not _is_mysql():
        return
    _assert_text_downgrade_fits()
    for table_name, column_name, nullable in reversed(_EXPANDED_COLUMNS):
        op.alter_column(
            table_name,
            column_name,
            existing_type=mysql.LONGTEXT(),
            type_=sa.Text(),
            existing_nullable=nullable,
        )
