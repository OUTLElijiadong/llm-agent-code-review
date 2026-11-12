"""Independent rate authority migration preserves evidence and starts with no grants."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def test_rate_review_migration_preserves_history_and_immutable_cancellation_evidence():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/107_rate_review_authority.py"
    spec = importlib.util.spec_from_file_location("rate_review_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "106_personal_agent_run" and len(module.revision) <= 32
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE billing_rate_catalog (id INTEGER PRIMARY KEY, status TEXT)"))
        conn.execute(text("INSERT INTO billing_rate_catalog VALUES (1, 'approved')"))
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        assert conn.execute(text("SELECT status FROM billing_rate_catalog WHERE id=1")).scalar_one() == "approved"
        assert conn.execute(text("SELECT COUNT(*) FROM billing_rate_reviewer_grant")).scalar_one() == 0
        conn.execute(text("INSERT INTO billing_rate_reviewer_grant(user_id, active) VALUES (2, 1)"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("INSERT INTO billing_rate_reviewer_grant(user_id, active) VALUES (2, 1)"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("UPDATE billing_rate_reviewer_grant SET active=2"))
        conn.execute(text("INSERT INTO billing_rate_cancel_request(catalog_id, active_catalog_id, status, "
                          "proposed_by, proposal_note, proposed_at) "
                          "VALUES (1, 1, 'pending', 2, '说明', CURRENT_TIMESTAMP)"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("UPDATE billing_rate_cancel_request SET proposal_note='篡改'"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("DELETE FROM billing_rate_cancel_request"))
        conn.execute(text("UPDATE billing_rate_cancel_request SET status='approved', active_catalog_id=NULL, "
                          "decided_by=3, decision_note='批准', decided_at=CURRENT_TIMESTAMP"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("UPDATE billing_rate_cancel_request SET decision_note='篡改'"))
    engine.dispose()
