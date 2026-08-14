"""032 策略记忆字段迁移回归。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa


def _migration_module():
    path = Path(__file__).parents[2] / "alembic" / "versions" / "032_strategy_memory.py"
    spec = importlib.util.spec_from_file_location("migration_032", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_032_upgrade_and_downgrade_sqlite(monkeypatch) -> None:
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE agent_memory ("
                "id INTEGER PRIMARY KEY, agent_code VARCHAR(80) NOT NULL, "
                "memory_type VARCHAR(30) NOT NULL, title VARCHAR(200) NOT NULL, "
                "content TEXT NOT NULL, weight FLOAT NOT NULL, status VARCHAR(30) NOT NULL, "
                "source_ref VARCHAR(160), create_time DATETIME NOT NULL, update_time DATETIME NOT NULL)"
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        monkeypatch.setattr(migration.op, "add_column", lambda table, column: connection.execute(
            sa.text(f"ALTER TABLE {table} ADD COLUMN {column.name} {column.type.compile(connection.dialect)}")
        ))
        created_indexes: list[str] = []
        monkeypatch.setattr(migration.op, "create_index", lambda name, *args, **kwargs: created_indexes.append(name))
        dropped_columns: list[str] = []
        monkeypatch.setattr(migration.op, "drop_index", lambda *args, **kwargs: None)
        monkeypatch.setattr(migration.op, "drop_column", lambda table, name: dropped_columns.append(name))

        migration.upgrade()
        columns = {item[1] for item in connection.execute(sa.text("PRAGMA table_info(agent_memory)"))}
        assert {
            "owner_user_id", "project_id", "share_scope", "fingerprint", "strategy_key",
            "outcome", "failure_kind", "success_count", "failure_count", "confidence",
            "evidence_json", "last_seen_at",
        } <= columns
        assert "uq_agent_memory_strategy_key" in created_indexes

        migration.downgrade()
        assert "strategy_key" in dropped_columns

    engine.dispose()
