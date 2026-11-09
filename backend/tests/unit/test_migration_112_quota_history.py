"""Quota reconciliation must not guess or discard existing limit history."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def test_112_marks_historical_limits_for_reconciliation_and_keeps_values():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/112_quota_history_index.py"
    spec = importlib.util.spec_from_file_location("migration_112_quota_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "111_pentest_execution"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE billing_limit (id INTEGER PRIMARY KEY, used_milli INTEGER, "
                                "reserved_milli INTEGER)"))
        connection.execute(text("INSERT INTO billing_limit VALUES (1, 30000, 20000)"))
        connection.execute(text("CREATE TABLE billing_reservation (id INTEGER PRIMARY KEY, enterprise_id INTEGER, "
                                "usage_kind TEXT, created_at DATETIME)"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text(
            "SELECT used_milli, reserved_milli, accounting_version FROM billing_limit WHERE id=1"
        )).one() == (30_000, 20_000, 0)
        columns = connection.execute(text("PRAGMA index_info(ix_billing_reservation_ent_usage_created)")).all()
        assert [row[2] for row in columns] == ["enterprise_id", "usage_kind", "created_at", "id"]
    engine.dispose()
