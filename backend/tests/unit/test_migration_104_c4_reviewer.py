"""C4 reviewer delegation starts empty and never changes prior billing data."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_c4_reviewer_grant_migration_starts_empty_and_preserves_history():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/104_c4_reviewer_grant.py"
    spec = importlib.util.spec_from_file_location("c4_reviewer_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "103_personal_agent" and len(module.revision) <= 32
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE billing_history (id INTEGER PRIMARY KEY, status TEXT)"))
        conn.execute(text("INSERT INTO billing_history VALUES (9, 'unknown')"))
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        assert conn.execute(text("SELECT status FROM billing_history WHERE id=9")).scalar_one() == "unknown"
        assert conn.execute(text("SELECT count(*) FROM billing_reconciliation_reviewer_grant")).scalar_one() == 0
        assert any(set(x["column_names"]) == {"user_id"} for x in inspect(conn).get_unique_constraints(
            "billing_reconciliation_reviewer_grant"
        ))
        conn.execute(text("INSERT INTO billing_reconciliation_reviewer_grant(user_id, active) VALUES (2, 1)"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("INSERT INTO billing_reconciliation_reviewer_grant(user_id, active) VALUES (2, 1)"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("UPDATE billing_reconciliation_reviewer_grant SET active=2"))
    engine.dispose()
