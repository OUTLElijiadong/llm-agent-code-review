"""105 preserves old catalogs and protects the added price evidence."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _migration(name):
    path = Path(__file__).resolve().parents[2] / f"alembic/versions/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_105_keeps_historical_source_null_and_added_fields_immutable():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        ops = Operations(MigrationContext.configure(connection))
        for name in ("098_billing_rate_catalog", "099_billing_tool_rate_catalog", "105_model_cny_cost_sources"):
            migration = _migration(name)
            migration.op = ops
            migration.upgrade()
        assert connection.execute(text(
            "SELECT model_cny_source_url, model_cny_source_checked_at FROM billing_rate_catalog"
        )).one() == (None, None)
        assert all(row == (None, None) for row in connection.execute(text(
            "SELECT provider_input_cny_micros_per_million, provider_output_cny_micros_per_million "
            "FROM billing_rate_catalog_item"
        )).all())
        with pytest.raises(Exception, match="model CNY source is immutable"):
            connection.execute(text(
                "UPDATE billing_rate_catalog SET model_cny_source_url='https://example.com', "
                "model_cny_source_checked_at=CURRENT_TIMESTAMP"
            ))
        with pytest.raises(Exception, match="billing_rate_catalog_item rows are immutable"):
            connection.execute(text(
                "UPDATE billing_rate_catalog_item SET provider_input_cny_micros_per_million=2000000, "
                "provider_output_cny_micros_per_million=8000000"
            ))
    engine.dispose()
