"""Migration 121 preserves old audit history and constrains external evidence."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_121_keeps_legacy_events_and_enforces_paired_zero_evidence():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/121_billing_model_evidence.py"
    spec = importlib.util.spec_from_file_location("migration_121_billing_model_evidence", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "120_discussion_turn_watermark"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE billing_reservation_reconciliation_event "
                                "(id INTEGER PRIMARY KEY, decision TEXT, resolution TEXT)"))
        connection.execute(text("INSERT INTO billing_reservation_reconciliation_event "
                                "(id,decision,resolution) VALUES (1,'proposed','settle')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {col["name"]: col for col in inspect(connection).get_columns(
            "billing_reservation_reconciliation_event")}
        assert columns["model_search_calls_evidence"]["nullable"]
        assert columns["model_fetch_calls_evidence"]["nullable"]
        legacy = connection.execute(text("SELECT model_search_calls_evidence, model_fetch_calls_evidence "
                                         "FROM billing_reservation_reconciliation_event WHERE id=1")).one()
        assert legacy == (None, None)
        connection.execute(text("INSERT INTO billing_reservation_reconciliation_event "
                                "(id,decision,resolution,model_search_calls_evidence,model_fetch_calls_evidence) "
                                "VALUES (2,'approved','settle',0,0)"))
        for search, fetch, resolution in [(0, None, "settle"), (1, 0, "settle"), (0, 0, "release_not_sent")]:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("INSERT INTO billing_reservation_reconciliation_event "
                    "(decision,resolution,model_search_calls_evidence,model_fetch_calls_evidence) "
                    "VALUES ('proposed',:resolution,:search,:fetch)"),
                    {"resolution": resolution, "search": search, "fetch": fetch})
        with pytest.raises(RuntimeError, match="不能自动降级"):
            migration.downgrade()
    engine.dispose()
