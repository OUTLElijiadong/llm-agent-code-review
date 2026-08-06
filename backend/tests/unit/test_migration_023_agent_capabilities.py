"""023 MCP/沙箱迁移的 SQLite 执行与 MySQL DDL 类型验证。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/023_agent_capabilities_mcp_sandbox.py"
    spec = importlib.util.spec_from_file_location("migration_023", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_023_upgrades_and_downgrades_sqlite(monkeypatch) -> None:
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        monkeypatch.setattr(module.op, "create_table", lambda *args, **kwargs: args[0])
        # 使用 Alembic Operations 的真实连接代理重跑 upgrade/downgrade。
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        module.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {"mcp_server", "mcp_tool", "agent_mcp_binding", "agent_capability_alias"} <= tables
        connection.execute(
            text(
                "INSERT INTO mcp_server (code, name) VALUES ('docs', 'Docs')"
            )
        )
        created = connection.execute(text("SELECT create_time FROM mcp_server")).scalar_one()
        assert created is not None
        module.downgrade()
        assert "mcp_server" not in inspect(connection).get_table_names()


def test_migration_023_uses_mysql_longtext_for_large_payloads() -> None:
    module = _migration_module()
    compiled = str(module.LONG_TEXT.compile(dialect=mysql.dialect())).upper()
    assert compiled == "LONGTEXT"


def test_migration_023_mysql_mcp_ddl_compiles_with_longtext() -> None:
    module = _migration_module()
    metadata = sa.MetaData()
    table = sa.Table(
        "mcp_tool",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("server_id", sa.BigInteger(), nullable=False),
        sa.Column("input_schema_json", module.LONG_TEXT, nullable=False),
        sa.Column("create_time", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()
    assert "BIGINT NOT NULL AUTO_INCREMENT" in ddl
    assert "INPUT_SCHEMA_JSON LONGTEXT NOT NULL" in " ".join(ddl.split())
    normalized = " ".join(ddl.split())
    assert "DEFAULT NOW()" in normalized or "DEFAULT CURRENT_TIMESTAMP" in normalized
