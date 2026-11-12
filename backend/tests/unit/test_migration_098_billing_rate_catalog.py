import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.services import billing_rate_catalog_service
from app.services.billing_ledger_service import RATE_CATALOG_VERSION


def _migration():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/098_billing_rate_catalog.py"
    spec = importlib.util.spec_from_file_location("migration_098_billing_rate_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_098_seeds_historical_prices_and_keeps_catalog_rows_immutable():
    module = _migration()
    assert module.revision == "098_billing_rate_catalog"
    assert len(module.revision) <= 32
    assert module.down_revision == "097_ent_team_project"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        catalogs = inspect(connection).get_table_names()
        assert {"billing_rate_catalog", "billing_rate_catalog_item", "billing_rate_catalog_event"}.issubset(catalogs)
        baseline = connection.execute(text(
            "SELECT version_key, status, approved_effective_at, fx_cny_per_usd_scaled, "
            "source_url, source_checked_at, fx_source_url, fx_source_checked_at, source_sha256, "
            "credit_pack_price_fen, credit_pack_credits "
            "FROM billing_rate_catalog"
        )).one()
        assert baseline[:2] == (RATE_CATALOG_VERSION, "approved")
        assert baseline[2].startswith("1970-01-01 00:00:00")
        assert baseline[3:] == (
            67168, "https://api-docs.deepseek.com/quick_start/pricing/",
            "2026-11-07 05:30:00.000000", "https://www.boc.cn/sourcedb/whpj/index.html",
            "2026-11-07 05:30:00.000000", baseline[8], 400, 1000,
        )
        items = connection.execute(text(
            "SELECT model_name, input_rate_numerator, input_rate_denominator, output_rate_numerator, "
            "output_rate_denominator, provider_input_usd_micros_per_million, "
            "provider_output_usd_micros_per_million FROM billing_rate_catalog_item ORDER BY model_name"
        )).all()
        assert items == [
            ("deepseek-flash", 1, 1, 4, 1, 300000, 1200000),
            ("deepseek-v4-flash", 1, 1, 4, 1, 300000, 1200000),
            ("deepseek-v4-flash-vision-exp", 1, 1, 4, 1, 300000, 1200000),
        ]
        assert baseline[8] == billing_rate_catalog_service._source_facts_digest(
            source_url=baseline[4], source_checked_at=datetime.fromisoformat(baseline[5]),
            fx_source_url=baseline[6], fx_source_checked_at=datetime.fromisoformat(baseline[7]),
            fx=baseline[3], price_fen=baseline[9], credits=baseline[10],
            items=[dict(item._mapping) for item in items],
        )
        assert connection.execute(text(
            "SELECT action FROM billing_rate_catalog_event ORDER BY id"
        )).scalars().all() == ["created", "approved"]

        with pytest.raises(Exception, match="immutable"):
            connection.execute(text(
                "UPDATE billing_rate_catalog_item SET output_rate_numerator=99 WHERE model_name='deepseek-flash'"
            ))
        with pytest.raises(Exception, match="immutable"):
            connection.execute(text("DELETE FROM billing_rate_catalog_event WHERE id=1"))
        with pytest.raises(Exception, match="snapshot is immutable"):
            connection.execute(text("UPDATE billing_rate_catalog SET source_url='https://example.com' WHERE id=1"))
        with pytest.raises(Exception, match="snapshot is immutable"):
            connection.execute(text("UPDATE billing_rate_catalog SET fx_source_url='https://example.com' WHERE id=1"))
        with pytest.raises(Exception, match="rows are immutable"):
            connection.execute(text("DELETE FROM billing_rate_catalog WHERE id=1"))
    engine.dispose()


def test_098_refuses_to_drop_financial_price_history():
    module = _migration()
    with pytest.raises(RuntimeError, match="不得删除计费价格版本"):
        module.downgrade()
