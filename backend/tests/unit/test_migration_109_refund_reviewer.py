import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def test_refund_reviewer_migration_preserves_orders_and_grants_nobody():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/109_refund_reviewer_grant.py"
    spec = importlib.util.spec_from_file_location("refund_reviewer_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "108_personal_agent_publication" and len(module.revision) <= 32
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE billing_refund (id INTEGER PRIMARY KEY, status TEXT)"))
        conn.execute(text("INSERT INTO billing_refund VALUES (1, 'approved')"))
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        assert conn.execute(text("SELECT status FROM billing_refund WHERE id=1")).scalar_one() == "approved"
        assert conn.execute(text(
            "SELECT reconciliation_query_attempt FROM billing_refund WHERE id=1"
        )).scalar_one() is None
        assert conn.execute(text("SELECT refund_creation_attempt FROM billing_refund WHERE id=1")).scalar_one() is None
        assert conn.execute(text("SELECT COUNT(*) FROM billing_refund_reviewer_grant")).scalar_one() == 0
        conn.execute(text("INSERT INTO billing_refund_reviewer_grant(user_id, active) VALUES (2, 1)"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("INSERT INTO billing_refund_reviewer_grant(user_id, active) VALUES (2, 1)"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("UPDATE billing_refund_reviewer_grant SET active=2"))
    engine.dispose()
