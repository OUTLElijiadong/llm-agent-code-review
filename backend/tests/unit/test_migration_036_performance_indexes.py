"""036 性能索引迁移回归。"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/036_performance_indexes.py"
    spec = importlib.util.spec_from_file_location("migration_036_performance_indexes", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_INDEXES = {
    "sandbox_environment": {"ix_sandbox_env_status_update", "ix_sandbox_env_project_purpose_status"},
    "agent_mesh_message": {"ix_mesh_msg_owner_target_status"},
    "agent_team_task": {"ix_team_task_team_status"},
}


def _schema(engine) -> None:
    """只建立 036 索引依赖的列，模拟 035 之后的最小表结构。"""
    metadata = sa.MetaData()
    sa.Table(
        "sandbox_environment",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.BigInteger, nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("update_time", sa.DateTime, nullable=False),
    )
    sa.Table(
        "agent_mesh_message",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("send_to", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
    )
    sa.Table(
        "agent_team_task",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("team_id", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
    )
    sa.Table(
        "agent_response_run",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("surface", sa.String(24), nullable=False),
        sa.Column("session_key", sa.String(128), nullable=False),
    )
    metadata.create_all(engine)


def _run_with_operations(module, conn, fn) -> None:
    context = MigrationContext.configure(conn)
    operations = Operations(context)
    original_op = module.op
    module.op = operations
    try:
        fn()
    finally:
        module.op = original_op


def test_migration_036_metadata() -> None:
    module = _migration_module()
    assert module.revision == "036"
    assert module.down_revision == "035"


def test_migration_036_upgrade_and_downgrade_on_sqlite() -> None:
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _schema(engine)

    def table_indexes(conn, table: str) -> set[str]:
        return {item["name"] for item in inspect(conn).get_indexes(table)}

    with engine.begin() as conn:
        assert table_indexes(conn, "sandbox_environment") == set()

        # 幂等 upgrade：重复执行不会重复建索引。
        _run_with_operations(module, conn, module.upgrade)
        _run_with_operations(module, conn, module.upgrade)
        for table, expected in _INDEXES.items():
            assert expected <= table_indexes(conn, table)
        count = sum(
            row[1] == "ix_sandbox_env_status_update"
            for row in conn.execute(sa.text('PRAGMA index_list("sandbox_environment")'))
        )
        assert count == 1

        _run_with_operations(module, conn, module.downgrade)
        for table, expected in _INDEXES.items():
            assert expected & table_indexes(conn, table) == set()

    engine.dispose()


def test_migration_036_renders_mysql_upgrade_and_downgrade_sql() -> None:
    module = _migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
    )
    operations = Operations(context)
    original_op = module.op
    module.op = operations
    try:
        module.upgrade()
        module.downgrade()
    finally:
        module.op = original_op

    sql = output.getvalue()
    expected_count = sum(len(index_names) for index_names in _INDEXES.values())
    assert sql.count("CREATE INDEX") == expected_count
    assert sql.count("DROP INDEX") == expected_count
    for table, index_names in _INDEXES.items():
        for index_name in index_names:
            assert index_name in sql
    # agent_mesh_message 的目标地址持久化列名为 send_to。
    assert "user_id, send_to, status" in sql
