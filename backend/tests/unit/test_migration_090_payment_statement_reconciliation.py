import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_090_migration_creates_append_only_statement_and_review_tables():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/090_payment_statement_reconciliation.py"
    spec = importlib.util.spec_from_file_location("migration_090_payment_statement_reconciliation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "090_payment_statement_reconcile"
    assert len(module.revision) <= 32
    assert module.down_revision == "089_refund_reconcile_lease"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {
            "billing_statement_batch", "billing_statement_row", "billing_statement_review_event",
        }.issubset(tables)
        batch_uniques = inspect(connection).get_unique_constraints("billing_statement_batch")
        assert {tuple(item["column_names"]) for item in batch_uniques} == {
            ("channel", "bill_date", "bill_type", "source_origin", "file_sha256"),
        }
        row_columns = {item["name"] for item in inspect(connection).get_columns("billing_statement_row")}
        assert "raw_line" not in row_columns
        assert "provider_status" in row_columns and "fee_micros" in row_columns
        batch_columns = {item["name"] for item in inspect(connection).get_columns("billing_statement_batch")}
        assert {"received_missing_count", "fee_missing_count"}.issubset(batch_columns)
        review_status = next(item for item in inspect(connection).get_columns("billing_statement_row")
                             if item["name"] == "review_status")
        assert "unreviewed" in str(review_status["default"])
        review_indexes = {item["name"] for item in inspect(connection).get_indexes("billing_statement_review_event")}
        assert "ix_billing_statement_review_row" in review_indexes
    engine.dispose()


def test_091_migration_adds_local_provider_refund_id_snapshot():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/091_statement_refund_provider_id.py"
    spec = importlib.util.spec_from_file_location("migration_091_statement_refund_provider_id", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "091_statement_refund_provider"
    assert len(module.revision) <= 32
    assert module.down_revision == "090_payment_statement_reconcile"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE billing_statement_row (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE billing_statement_batch (id INTEGER PRIMARY KEY, source_origin VARCHAR(24) NOT NULL)"
        )
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        columns = {item["name"] for item in inspect(connection).get_columns("billing_statement_row")}
        assert "local_provider_refund_id" in columns
        batch_columns = {item["name"] for item in inspect(connection).get_columns("billing_statement_batch")}
        assert {"source_origin", "provider_hash_type", "provider_hash_value"}.issubset(batch_columns)
    engine.dispose()


def test_091_migration_refuses_to_drop_financial_evidence():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/091_statement_refund_provider_id.py"
    spec = importlib.util.spec_from_file_location("migration_091_no_downgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="财务审计"):
        module.downgrade()


def test_090_migration_refuses_to_drop_financial_evidence():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/090_payment_statement_reconciliation.py"
    spec = importlib.util.spec_from_file_location("migration_090_no_downgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="财务审计"):
        module.downgrade()
