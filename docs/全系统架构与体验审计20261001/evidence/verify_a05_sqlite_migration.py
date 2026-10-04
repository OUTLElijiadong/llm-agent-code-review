"""对 060 的数据回填逻辑做隔离 SQLite 迁移烟测；不连接生产或 MySQL。"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "backend/alembic/versions/060_agent_response_transcript_message_digest.py"
SPEC = importlib.util.spec_from_file_location("migration_060", MIGRATION)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

engine = sa.create_engine("sqlite:///:memory:")
with engine.begin() as connection:
    connection.exec_driver_sql(
        "CREATE TABLE agent_response_transcript_message "
        "(id INTEGER PRIMARY KEY, message_json TEXT NOT NULL)"
    )
    connection.exec_driver_sql(
        "INSERT INTO agent_response_transcript_message (id, message_json) VALUES (?, ?)",
        [
            (row_id, f'{{"role":"user","content":"第{row_id}条历史证据🧩"}}')
            for row_id in range(1, 504)
        ],
    )
    operations = Operations(MigrationContext.configure(connection))

    def alter_column(table_name: str, column_name: str, **kwargs: object) -> None:
        with operations.batch_alter_table(table_name) as batch:
            batch.alter_column(column_name, **kwargs)

    MODULE.op = SimpleNamespace(
        add_column=operations.add_column,
        alter_column=alter_column,
        get_bind=lambda: connection,
    )
    MODULE.upgrade()
    rows = connection.exec_driver_sql(
        "SELECT id, message_json, message_sha256 "
        "FROM agent_response_transcript_message ORDER BY id"
    ).all()
    columns = {
        column["name"]: column
        for column in sa.inspect(connection).get_columns("agent_response_transcript_message")
    }
    assert len(rows) == 503
    assert all(row[2] == hashlib.sha256(row[1].encode("utf-8")).hexdigest() for row in rows)
    assert columns["message_sha256"]["nullable"] is False
    print("SQLite migration backfill: 503/503 digests match; digest column is NOT NULL")
