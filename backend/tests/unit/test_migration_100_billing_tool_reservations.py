import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError


def _migration():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/100_billing_tool_reservations.py"
    spec = importlib.util.spec_from_file_location("migration_100_billing_tool_reservations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_100_replaces_reservation_check_constraints_on_sqlite_and_preserves_legacy_rows():
    module = _migration()
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE billing_reservation (
                id INTEGER PRIMARY KEY,
                request_key VARCHAR(160) NOT NULL,
                model_name VARCHAR(80) NOT NULL,
                pricing_version VARCHAR(64),
                input_rate_numerator BIGINT,
                input_rate_denominator BIGINT,
                output_rate_numerator BIGINT,
                output_rate_denominator BIGINT,
                provider_identity_version VARCHAR(32),
                created_at DATETIME NOT NULL,
                CONSTRAINT ck_billing_reservation_rate_snapshot_pair CHECK (
                    (pricing_version IS NULL AND input_rate_numerator IS NULL
                     AND input_rate_denominator IS NULL AND output_rate_numerator IS NULL
                     AND output_rate_denominator IS NULL)
                    OR (pricing_version IS NOT NULL AND pricing_version <> ''
                        AND input_rate_numerator IS NOT NULL AND input_rate_denominator IS NOT NULL
                        AND output_rate_numerator IS NOT NULL AND output_rate_denominator IS NOT NULL
                        AND input_rate_numerator > 0 AND input_rate_denominator > 0
                        AND output_rate_numerator > 0 AND output_rate_denominator > 0)
                )
            )
        """))
        connection.execute(text("""
            CREATE TABLE billing_reservation_reconciliation_event (
                id INTEGER PRIMARY KEY,
                reservation_id BIGINT NOT NULL,
                actor_id BIGINT NOT NULL,
                decision VARCHAR(16) NOT NULL,
                note VARCHAR(500) NOT NULL,
                created_at DATETIME NOT NULL
            )
        """))
        # Match the actual 067 migration/ORM: these ledger-detail tables store
        # reservation IDs but do not declare FK constraints to the parent table.
        connection.execute(text("""
            CREATE TABLE billing_reservation_part (
                id INTEGER PRIMARY KEY,
                reservation_id BIGINT NOT NULL,
                wallet_id BIGINT NOT NULL,
                batch_id BIGINT NOT NULL,
                reserved_milli BIGINT NOT NULL,
                charged_milli BIGINT
            )
        """))
        connection.execute(text("""
            CREATE TABLE billing_reservation_limit (
                id INTEGER PRIMARY KEY,
                reservation_id BIGINT NOT NULL,
                limit_id BIGINT NOT NULL,
                reserved_milli BIGINT NOT NULL,
                UNIQUE(reservation_id, limit_id)
            )
        """))
        connection.execute(text(
            "INSERT INTO billing_reservation "
            "(id, request_key, model_name, created_at) VALUES (1, 'legacy-1', 'deepseek-flash', CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO billing_reservation_part "
            "(id, reservation_id, wallet_id, batch_id, reserved_milli) VALUES (1, 1, 3, 4, 500)"
        ))
        connection.execute(text(
            "INSERT INTO billing_reservation_limit "
            "(id, reservation_id, limit_id, reserved_milli) VALUES (1, 1, 5, 500)"
        ))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        columns = {item["name"] for item in inspect(connection).get_columns("billing_reservation")}
        assert {
            "billable_kind", "tool_name", "tool_cost_cny_micros_per_success",
            "tool_price_milli_per_success", "tool_successful_calls", "provider_attempt_started_at",
        } <= columns
        checks = {item["name"] for item in inspect(connection).get_check_constraints("billing_reservation")}
        assert {
            "ck_billing_reservation_rate_snapshot_pair",
            "ck_billing_reservation_billable_kind_snapshot",
            "ck_billing_reservation_tool_success_count",
        } <= checks
        assert connection.execute(text(
            "SELECT billable_kind, tool_name, pricing_version FROM billing_reservation WHERE id=1"
        )).one() == ("model", None, None)
        assert connection.execute(text(
            "SELECT reservation_id, reserved_milli FROM billing_reservation_part WHERE id=1"
        )).one() == (1, 500)
        assert connection.execute(text(
            "SELECT reservation_id, reserved_milli FROM billing_reservation_limit WHERE id=1"
        )).one() == (1, 500)

        with pytest.raises(IntegrityError):
            connection.execute(text(
                "UPDATE billing_reservation SET billable_kind='search', tool_name='fetch' WHERE id=1"
            ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "UPDATE billing_reservation SET billable_kind='search', tool_name='search', "
                "pricing_version='tool-v1', provider_identity_version='deepseek-official-v1' WHERE id=1"
            ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "UPDATE billing_reservation SET tool_successful_calls=2 WHERE id=1"
            ))

        event_checks = {
            item["name"] for item in inspect(connection).get_check_constraints(
                "billing_reservation_reconciliation_event"
            )
        }
        assert "ck_billing_res_recon_tool_success_count" in event_checks

    engine.dispose()


def test_100_refuses_financial_snapshot_downgrade():
    module = _migration()
    with pytest.raises(RuntimeError, match="不得删除"):
        module.downgrade()
