"""062 migration keeps existing audit rows and accommodates bounded action names."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, text


def _migration():
    path = Path(__file__).parents[2] / "alembic/versions/062_audit_log_action_length.py"
    spec = spec_from_file_location("audit_log_action_length_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _build_audit_table(engine):
    metadata = MetaData()
    Table(
        "audit_log",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("action", String(40), nullable=False, comment="操作类型: login/user/rule/ai/project/agent"),
        Column("create_time", DateTime),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE INDEX ix_audit_log_action_time ON audit_log (action, create_time)"))
        connection.execute(text("INSERT INTO audit_log (id,action) VALUES (1,'login')"))


def test_upgrade_preserves_rows_and_accepts_namespaced_operation_actions():
    migration = _migration()
    engine = create_engine("sqlite://")
    _build_audit_table(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("audit_log")}
        assert columns["action"]["type"].length == 63
        assert {index["name"] for index in inspect(connection).get_indexes("audit_log")} == {
            "ix_audit_log_action_time"
        }
        actions = (
            "admin_copilot.ops.security_block_configure",
            "admin_copilot.ops.ssh_authorized_key_action",
        )
        for row_id, action in enumerate(actions, start=2):
            connection.execute(
                text("INSERT INTO audit_log (id,action) VALUES (:id,:action)"),
                {"id": row_id, "action": action},
            )
        assert connection.execute(text("SELECT action FROM audit_log WHERE id=1")).scalar_one() == "login"
        stored_actions = tuple(
            connection.execute(text("SELECT action FROM audit_log WHERE id>1 ORDER BY id")).scalars()
        )
        assert stored_actions == actions

    engine.dispose()


def test_downgrade_refuses_to_truncate_action_values():
    migration = _migration()
    engine = create_engine("sqlite://")
    _build_audit_table(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        connection.execute(
            text("INSERT INTO audit_log (id,action) VALUES (2,:action)"),
            {"action": "admin_copilot.ops.ssh_authorized_key_action"},
        )
        with pytest.raises(RuntimeError, match="拒绝缩短 audit_log.action"):
            migration.downgrade()
        assert inspect(connection).get_columns("audit_log")[1]["type"].length == 63
        assert connection.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one() == 2

    engine.dispose()
