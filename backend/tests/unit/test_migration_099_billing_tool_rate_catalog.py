import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text


def _migration(revision: str):
    backend = Path(__file__).resolve().parents[2]
    path = backend / f"alembic/versions/{revision}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_099_adds_disabled_tool_rates_and_extends_catalog_immutability():
    previous = _migration("098_billing_rate_catalog")
    module = _migration("099_billing_tool_rate_catalog")
    assert module.revision == "099_billing_tool_rate_catalog"
    assert module.down_revision == "098_billing_rate_catalog"

    engine = create_engine("sqlite:///:memory:")
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        previous.op = Operations(context)
        previous.upgrade()
        module.op = Operations(context)
        module.upgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("billing_rate_catalog")}
        assert {
            "tool_cny_source_url", "tool_cny_source_checked_at",
            "tool_usd_source_url", "tool_usd_source_checked_at",
            "tool_search_cost_cny_micros_per_success", "tool_search_price_milli_per_success",
            "tool_fetch_cost_cny_micros_per_success", "tool_fetch_price_milli_per_success",
        }.issubset(columns)
        baseline = connection.execute(text(
            "SELECT tool_cny_source_url, tool_cny_source_checked_at, "
            "tool_usd_source_url, tool_usd_source_checked_at, "
            "tool_search_cost_cny_micros_per_success, tool_search_price_milli_per_success, "
            "tool_fetch_cost_cny_micros_per_success, tool_fetch_price_milli_per_success "
            "FROM billing_rate_catalog"
        )).one()
        assert baseline == (None, None, None, None, 0, 0, 0, 0)

        with pytest.raises(Exception, match="snapshot is immutable"):
            connection.execute(text(
                "UPDATE billing_rate_catalog SET "
                "tool_cny_source_url='https://api-docs.deepseek.com/zh-cn/guides/search/', "
                "tool_cny_source_checked_at='2026-11-07 05:30:00', "
                "tool_usd_source_url='https://api-docs.deepseek.com/quick_start/pricing/', "
                "tool_usd_source_checked_at='2026-11-07 05:30:00', "
                "tool_search_cost_cny_micros_per_success=5000, "
                "tool_search_price_milli_per_success=1786 "
                "WHERE id=1"
            ))
        with pytest.raises(Exception):
            connection.execute(text(
                "UPDATE billing_rate_catalog SET "
                "tool_search_cost_cny_micros_per_success=5000 WHERE id=1"
            ))
        with pytest.raises(Exception, match="tool pricing check failed"):
            connection.execute(text(
                "INSERT INTO billing_rate_catalog (version_key, status, effective_at, source_url, "
                "source_checked_at, fx_source_url, fx_source_checked_at, source_sha256, "
                "fx_cny_per_usd_scaled, credit_pack_price_fen, credit_pack_credits, change_note, "
                "created_at, tool_search_cost_cny_micros_per_success) VALUES ("
                "'c000000000000000000000000000000000000000000000000000000000000000', 'draft', "
                "'2026-11-10 00:00:00', 'https://api-docs.deepseek.com/quick_start/pricing/', "
                "'2026-11-07 05:30:00', 'https://www.boc.cn/sourcedb/whpj/index.html', "
                "'2026-11-07 05:30:00', 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', "
                "67168, 400, 1000, 'invalid half-configured tool rate', '2026-11-07 06:00:00', 5000)"
            ))
        with pytest.raises(Exception, match="billing_rate_catalog_item rows are immutable"):
            connection.execute(text(
                "UPDATE billing_rate_catalog_item SET output_rate_numerator=88 WHERE model_name='deepseek-flash'"
            ))
        with pytest.raises(Exception, match="不得删除计费费率目录工具历史"):
            module.downgrade()
    engine.dispose()
