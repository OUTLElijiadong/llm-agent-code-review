"""Migration 122 creates a constrained, non-destructive platform cost ledger."""

import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _migration():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/122_platform_model_cost_ledger.py"
    spec = importlib.util.spec_from_file_location("migration_122_platform_model_cost_ledger", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return backend, migration


def _row_values(request_key="valid"):
    return {
        "request_key": request_key,
        "actor_user_id": 7,
        "run_id": "admin-run",
        "model_name": "deepseek-v4-pro",
        "pricing_version": "deepseek-cny-peak-cache-miss-2026-11-17-v1",
        "pricing_source_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "pricing_source_checked_at": "2026-11-17 00:00:00",
        "provider_identity_version": "deepseek-platform-cost-v1",
        "provider_name": "deepseek",
        "endpoint_host": "api.deepseek.com",
        "credential_source": "system",
        "input_cost_cny_micros_per_million": 9_000_000,
        "output_cost_cny_micros_per_million": 27_000_000,
        "max_input_tokens": 20_000,
        "max_output_tokens": 1_000,
        "reserved_cost_cny_micros": 207_000,
        "actual_input_tokens": 10,
        "actual_output_tokens": 3,
        "actual_cost_cny_micros": 171,
        "status": "settled",
        "provider_attempt_started_at": "2026-11-17 00:00:01",
        "created_at": "2026-11-17 00:00:00",
        "settled_at": "2026-11-17 00:00:02",
    }


def test_122_is_single_head_after_121():
    backend, migration = _migration()
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [migration.revision]
    assert migration.down_revision == "121_billing_model_evidence"


def test_122_constraints_reject_negative_values_and_nonempty_downgrade():
    _backend, migration = _migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        columns = {column["name"] for column in inspect(connection).get_columns("platform_model_cost_attempt")}
        assert "active_run_id" not in columns
        constraints = {
            item["name"]: item["sqltext"]
            for item in inspect(connection).get_check_constraints("platform_model_cost_attempt")
        }
        assert "actual_input_tokens >= 0" in constraints["ck_platform_model_cost_token_limit"]
        assert "actual_output_tokens >= 0" in constraints["ck_platform_model_cost_token_limit"]
        assert "actual_cost_cny_micros >= 0" in constraints["ck_platform_model_cost_token_limit"]

        insert_sql = text(
            "INSERT INTO platform_model_cost_attempt (" + ", ".join(_row_values()) + ") VALUES ("
            + ", ".join(f":{key}" for key in _row_values()) + ")"
        )
        connection.execute(insert_sql, _row_values())
        for column, value in (
            ("actual_input_tokens", -1),
            ("actual_output_tokens", -1),
            ("actual_cost_cny_micros", -1),
        ):
            values = _row_values(f"negative-{column}")
            values[column] = value
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(insert_sql, values)

        with pytest.raises(RuntimeError, match="拒绝降级"):
            migration.downgrade()
        assert connection.execute(text("SELECT COUNT(*) FROM platform_model_cost_attempt")).scalar_one() == 1

        connection.execute(text("DELETE FROM platform_model_cost_attempt"))
        migration.downgrade()
        assert "platform_model_cost_attempt" not in inspect(connection).get_table_names()
        migration.upgrade()
        assert "platform_model_cost_attempt" in inspect(connection).get_table_names()
    engine.dispose()
